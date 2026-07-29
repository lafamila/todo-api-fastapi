from fastapi import APIRouter, Depends, Header, HTTPException

try:
    from ..auth_utils import get_current_user, require_admin
    from ..connectors import get_db_connection
    from ..models.base import (
        BulkDeleteMemosRequest,
        CreateMemoRequest,
        UpdateMemoRequest,
    )
    from ..services.merge import MergeError, run_merge
    from ..services.realtime import get_realtime_server
    from ..timeutil import iso_utc, localnow_naive, utcnow_naive
    from ..utils import can_manage_project, can_write_project, check_project_membership, generate_id
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user, require_admin
    from connectors import get_db_connection
    from models.base import (
        BulkDeleteMemosRequest,
        CreateMemoRequest,
        UpdateMemoRequest,
    )
    from services.merge import MergeError, run_merge
    from services.realtime import get_realtime_server
    from timeutil import iso_utc, localnow_naive, utcnow_naive
    from utils import can_manage_project, can_write_project, check_project_membership, generate_id


router = APIRouter(prefix="/api", tags=["memos"])

MEMO_COLUMNS = (
    "id, project_id, created_by, title, content, status, "
    "created_at, updated_at, updated_at_utc"
)


def _serialize_memo(memo: dict) -> dict:
    return {
        "id": memo["id"],
        "projectId": memo["project_id"],
        "createdBy": memo["created_by"],
        "title": memo["title"],
        "content": memo["content"],
        "status": memo["status"],
        "createdAt": memo["created_at"].isoformat(),
        "updatedAt": memo["updated_at"].isoformat(),
        "updatedAtUtc": iso_utc(memo.get("updated_at_utc")),
    }


def _serialize_version(version: dict) -> dict:
    return {
        "id": version["id"],
        "memoId": version["memo_id"],
        "content": version["content"],
        "version": version["version"],
        # 충돌/병합으로 보존된 버전임을 알려 주는 표시 — `충돌 · 로컬 (07-29 14:02)`
        "note": version.get("note"),
        "createdAt": version["created_at"].isoformat(),
        "updatedAtUtc": iso_utc(version.get("updated_at_utc")),
    }


def _require_live_project(cursor, project_id: str) -> dict:
    cursor.execute(
        "SELECT id FROM projects WHERE id = %s AND deleted_at IS NULL", (project_id,)
    )
    project = cursor.fetchone()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _require_memo_write_lease(
    memo_id: str, user_id: str, lease_token: str | None
) -> None:
    realtime_server = get_realtime_server()
    if realtime_server is None or not realtime_server.available:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "memo_realtime_unavailable",
                "message": "Memo editing is unavailable until realtime locking is ready.",
            },
        )
    if not realtime_server.validate_rest_lease(memo_id, user_id, lease_token):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "memo_lease_required",
                "message": "A current memo edit lease is required.",
            },
        )


@router.get("/projects/{project_id}/memos")
async def get_project_memos(project_id: str, user: dict = Depends(get_current_user)):
    """특정 프로젝트의 메모 목록 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_project(cursor, project_id)

            if not check_project_membership(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                f"""
                SELECT {MEMO_COLUMNS}
                FROM memos
                WHERE project_id = %s AND deleted_at IS NULL
                ORDER BY created_at DESC
            """,
                (project_id,),
            )
            return [_serialize_memo(memo) for memo in cursor.fetchall()]


@router.post("/memos", status_code=201)
async def create_memo(data: CreateMemoRequest, user: dict = Depends(get_current_user)):
    """메모 생성"""
    memo_id = generate_id()
    now = localnow_naive()
    now_utc = utcnow_naive()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_project(cursor, data.projectId)

            if not can_write_project(cursor, data.projectId, user):
                raise HTTPException(status_code=403, detail="Access denied")

            # 이 가드는 사람이 만드는 경로에만 있다. 동기화 경로(`/api/sync/push`)는
            # 이 가드를 우회하고 중복을 sync_issues 로 기록한다 — 막으면 오프라인
            # 생성분이 409 로 동기화를 영구 정지시킨다.
            cursor.execute(
                "SELECT id FROM memos WHERE project_id = %s AND title = %s AND deleted_at IS NULL LIMIT 1",
                (data.projectId, data.title),
            )
            existing_memo = cursor.fetchone()
            if existing_memo:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "동일한 제목의 메모가 이미 존재합니다.",
                        "existingMemoId": existing_memo["id"],
                    },
                )

            cursor.execute(
                """
                INSERT INTO memos
                    (id, project_id, created_by, title, content, status,
                     created_at, updated_at, updated_at_utc)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (memo_id, data.projectId, user["id"], data.title, "", 0, now, now, now_utc),
            )

    return {
        "id": memo_id,
        "projectId": data.projectId,
        "createdBy": user["id"],
        "title": data.title,
        "content": "",
        "status": 0,
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
        "updatedAtUtc": iso_utc(now_utc),
    }


@router.get("/memos/{memo_id}")
async def get_memo(memo_id: str, user: dict = Depends(get_current_user)):
    """메모 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {MEMO_COLUMNS}
                FROM memos
                WHERE id = %s AND deleted_at IS NULL
            """,
                (memo_id,),
            )
            memo = cursor.fetchone()

            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not check_project_membership(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            return _serialize_memo(memo)


@router.put("/memos/{memo_id}")
async def update_memo(
    memo_id: str,
    data: UpdateMemoRequest,
    user: dict = Depends(get_current_user),
    lease_token: str | None = Header(default=None, alias="X-Memo-Lease-Token"),
):
    """메모 업데이트 (버전 히스토리 저장)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM memos WHERE id = %s AND deleted_at IS NULL", (memo_id,)
            )
            memo = cursor.fetchone()

            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not can_write_project(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            _require_memo_write_lease(memo_id, user["id"], lease_token)

            now = localnow_naive()
            now_utc = utcnow_naive()

            if memo["content"]:
                cursor.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) as max_version
                    FROM memo_versions
                    WHERE memo_id = %s
                """,
                    (memo_id,),
                )
                next_version = cursor.fetchone()["max_version"] + 1

                cursor.execute(
                    """
                    INSERT INTO memo_versions
                        (id, memo_id, content, version, note, created_at, updated_at_utc)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        generate_id(),
                        memo_id,
                        memo["content"],
                        next_version,
                        None,
                        now,
                        now_utc,
                    ),
                )

            cursor.execute(
                """
                UPDATE memos
                SET content = %s, updated_at = %s, updated_at_utc = %s
                WHERE id = %s
            """,
                (data.content, now, now_utc, memo_id),
            )

            cursor.execute(
                f"""
                SELECT {MEMO_COLUMNS}
                FROM memos
                WHERE id = %s AND deleted_at IS NULL
            """,
                (memo_id,),
            )
            return _serialize_memo(cursor.fetchone())


@router.get("/memos/{memo_id}/versions")
async def get_memo_versions(memo_id: str, user: dict = Depends(get_current_user)):
    """메모의 버전 히스토리 조회 (충돌·병합 보존 버전은 `note` 로 구분된다)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, project_id FROM memos WHERE id = %s AND deleted_at IS NULL",
                (memo_id,),
            )
            memo = cursor.fetchone()
            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not check_project_membership(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                """
                SELECT id, memo_id, content, version, note, created_at, updated_at_utc
                FROM memo_versions
                WHERE memo_id = %s
                ORDER BY version DESC
            """,
                (memo_id,),
            )
            return [_serialize_version(version) for version in cursor.fetchall()]


@router.get("/memos/{memo_id}/versions/{version}")
async def get_memo_version(
    memo_id: str, version: int, user: dict = Depends(get_current_user)
):
    """특정 버전의 메모 내용 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, project_id FROM memos WHERE id = %s AND deleted_at IS NULL",
                (memo_id,),
            )
            memo = cursor.fetchone()
            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not check_project_membership(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                """
                SELECT id, memo_id, content, version, note, created_at, updated_at_utc
                FROM memo_versions
                WHERE memo_id = %s AND version = %s
                ORDER BY created_at DESC
                LIMIT 1
            """,
                (memo_id, version),
            )
            version_data = cursor.fetchone()

            if not version_data:
                raise HTTPException(status_code=404, detail="Version not found")

            return _serialize_version(version_data)


@router.post("/memos/{loser_id}/merge-into/{winner_id}")
async def merge_memo_into(
    loser_id: str, winner_id: str, user: dict = Depends(get_current_user)
):
    """중복 메모 병합 — 패자 내용을 생존자 버전으로 편입하고 패자를 tombstone 한다.

    오프라인에서는 잠긴다 (409). 온라인 클라이언트는 원격에 위임하고 결과를 pull 로 받는다.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            for memo_id in (loser_id, winner_id):
                cursor.execute(
                    "SELECT project_id FROM memos WHERE id = %s AND deleted_at IS NULL",
                    (memo_id,),
                )
                memo = cursor.fetchone()
                if not memo:
                    raise HTTPException(status_code=404, detail=f"Memo not found: {memo_id}")
                if not can_manage_project(cursor, memo["project_id"], user):
                    raise HTTPException(status_code=403, detail="Access denied")

    try:
        return await run_merge("memo", loser_id, winner_id)
    except MergeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.delete("/memos/{memo_id}")
async def delete_memo(memo_id: str, user: dict = Depends(get_current_user)):
    """메모 소프트 삭제"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.id, m.project_id, p.owner_id
                FROM memos m
                JOIN projects p ON m.project_id = p.id
                WHERE m.id = %s AND m.deleted_at IS NULL
            """,
                (memo_id,),
            )
            memo = cursor.fetchone()
            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not can_manage_project(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            now_utc = utcnow_naive()
            cursor.execute(
                "UPDATE memos SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
                (now_utc, now_utc, memo_id),
            )
            return {"message": "Memo soft-deleted successfully"}


@router.post("/memos/bulk-delete")
async def bulk_delete_memos(
    data: BulkDeleteMemosRequest, user: dict = Depends(require_admin)
):
    """메모 일괄 소프트 삭제"""
    if not data.memoIds:
        raise HTTPException(status_code=400, detail="memoIds cannot be empty")

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            now_utc = utcnow_naive()
            deleted_count = 0
            for memo_id in data.memoIds:
                cursor.execute(
                    """
                    SELECT project_id
                    FROM memos
                    WHERE id = %s AND deleted_at IS NULL
                    """,
                    (memo_id,),
                )
                memo = cursor.fetchone()
                if not memo or not can_manage_project(cursor, memo["project_id"], user):
                    continue
                cursor.execute(
                    "UPDATE memos SET deleted_at = %s, updated_at_utc = %s "
                    "WHERE id = %s AND deleted_at IS NULL",
                    (now_utc, now_utc, memo_id),
                )
                deleted_count += cursor.rowcount

            return {
                "message": f"{deleted_count}개의 메모가 삭제되었습니다.",
                "deletedCount": deleted_count,
            }
