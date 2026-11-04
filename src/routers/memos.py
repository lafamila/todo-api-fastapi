import asyncio
import hashlib
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query

try:
    from ..auth_utils import get_current_user, require_admin
    from ..connectors import get_db_connection
    from ..models.base import (
        BulkDeleteMemosRequest,
        CreateMemoRequest,
        MoveMemoRequest,
        UpdateMemoRequest,
    )
    from ..services.merge import MergeError, run_merge
    from ..services.memo_search import search_memos, validate_search_query
    from ..services.realtime import get_realtime_server
    from ..timeutil import iso_utc, localnow_naive, utcnow_naive
    from ..utils import backlog_id, can_manage_project, can_write_project, check_project_membership, generate_id
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user, require_admin
    from connectors import get_db_connection
    from models.base import (
        BulkDeleteMemosRequest,
        CreateMemoRequest,
        MoveMemoRequest,
        UpdateMemoRequest,
    )
    from services.merge import MergeError, run_merge
    from services.memo_search import search_memos, validate_search_query
    from services.realtime import get_realtime_server
    from timeutil import iso_utc, localnow_naive, utcnow_naive
    from utils import backlog_id, can_manage_project, can_write_project, check_project_membership, generate_id


router = APIRouter(prefix="/api", tags=["memos"])

_SEARCH_CAPACITY = asyncio.BoundedSemaphore(2)


def _release_search_capacity(
    capacity: asyncio.BoundedSemaphore, task: asyncio.Task
) -> None:
    capacity.release()
    if not task.cancelled():
        # Mark a worker exception as observed even if the HTTP request was
        # cancelled while asyncio.shield kept the thread-backed task alive.
        task.exception()

MEMO_COLUMNS = (
    "id, project_id, created_by, kind, title, content, status, "
    "created_at, updated_at, updated_at_utc"
)


def _memo_revision(memo: dict) -> str:
    """Return an opaque, deterministic revision without adding DB state."""
    canonical = json.dumps(
        {
            "content": memo["content"],
            "id": memo["id"],
            "updatedAtUtc": iso_utc(memo.get("updated_at_utc")),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serialize_memo(memo: dict) -> dict:
    return {
        "id": memo["id"],
        "projectId": memo["project_id"],
        "createdBy": memo["created_by"],
        "kind": memo.get("kind") or "memo",
        "title": memo["title"],
        "content": memo["content"],
        "status": memo["status"],
        "createdAt": memo["created_at"].isoformat(),
        "updatedAt": memo["updated_at"].isoformat(),
        "updatedAtUtc": iso_utc(memo.get("updated_at_utc")),
        "revision": _memo_revision(memo),
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


def _serialize_version_summary(version: dict) -> dict:
    """목록에는 큰 본문을 싣지 않는다 — 본문은 버전 선택 시 상세 API로 읽는다."""
    return {
        "id": version["id"],
        "memoId": version["memo_id"],
        "version": version["version"],
        "note": version.get("note"),
        "createdAt": version["created_at"].isoformat(),
        "updatedAtUtc": iso_utc(version.get("updated_at_utc")),
    }


def _require_live_project(cursor, project_id: str, *, for_update: bool = False) -> dict:
    suffix = " FOR UPDATE" if for_update else ""
    cursor.execute(
        f"""
        SELECT p.id, p.owner_id, p.parent_id, p.is_group
        FROM projects p LEFT JOIN projects parent ON parent.id = p.parent_id
        WHERE p.id = %s AND p.deleted_at IS NULL
          AND (p.parent_id IS NULL OR (parent.id IS NOT NULL AND parent.deleted_at IS NULL))
        {suffix}
        """,
        (project_id,),
    )
    project = cursor.fetchone()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _require_leaf_project(cursor, project_id: str, *, for_update: bool = False) -> dict:
    project = _require_live_project(cursor, project_id, for_update=for_update)
    if project["is_group"]:
        raise HTTPException(status_code=409, detail="Group projects cannot contain memos")
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


def _search_project_memos_sync(
    project_id: str, query: str, limit: int, user: dict
) -> dict:
    """Load, authorize, and rank in a worker so large corpora never block ASGI."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_leaf_project(cursor, project_id)

            if not check_project_membership(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Access denied")

            # memos 자체가 최신 스냅샷이다. memo_versions 를 조인하지 않고 전체
            # 현재 코퍼스를 가져온 뒤 ranking을 수행해야 limit 이전 문서도 경쟁한다.
            cursor.execute(
                """
                SELECT id, title, content
                FROM memos
                WHERE project_id = %s AND kind = 'memo' AND deleted_at IS NULL
                """,
                (project_id,),
            )
            rows = cursor.fetchall()

    return search_memos(rows, query, limit)


@router.get("/projects/{project_id}/memos")
async def get_project_memos(project_id: str, user: dict = Depends(get_current_user)):
    """특정 프로젝트의 메모 목록 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_leaf_project(cursor, project_id)

            if not check_project_membership(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                f"""
                SELECT {MEMO_COLUMNS}
                FROM memos
                WHERE project_id = %s AND kind = 'memo' AND deleted_at IS NULL
                ORDER BY created_at DESC
            """,
                (project_id,),
            )
            return [_serialize_memo(memo) for memo in cursor.fetchall()]


@router.get("/projects/{project_id}/memos/search")
async def search_project_memos(
    project_id: str,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    """현재 프로젝트의 저장된 최신 메모를 관련도순으로 검색한다."""
    try:
        validate_search_query(q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if _SEARCH_CAPACITY.locked():
        raise HTTPException(
            status_code=429,
            detail="검색 요청이 많습니다. 잠시 후 다시 시도해 주세요.",
            headers={"Retry-After": "1"},
        )

    capacity = _SEARCH_CAPACITY
    await capacity.acquire()
    try:
        worker = asyncio.create_task(
            asyncio.to_thread(
                _search_project_memos_sync, project_id, q, limit, user
            )
        )
    except BaseException:
        capacity.release()
        raise
    worker.add_done_callback(
        lambda task: _release_search_capacity(capacity, task)
    )
    return await asyncio.shield(worker)


@router.post("/memos", status_code=201)
async def create_memo(data: CreateMemoRequest, user: dict = Depends(get_current_user)):
    """메모 생성"""
    memo_id = generate_id()
    now = localnow_naive()
    now_utc = utcnow_naive()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # root→group 전환과 serialize해 전환 직후 부모에 메모가 남는 race를 막는다.
            _require_leaf_project(cursor, data.projectId, for_update=True)

            if not can_write_project(cursor, data.projectId, user):
                raise HTTPException(status_code=403, detail="Access denied")

            # 이 가드는 사람이 만드는 경로에만 있다. 동기화 경로(`/api/sync/push`)는
            # 이 가드를 우회하고 중복을 sync_issues 로 기록한다 — 막으면 오프라인
            # 생성분이 409 로 동기화를 영구 정지시킨다.
            cursor.execute(
                "SELECT id FROM memos WHERE project_id = %s AND kind = 'memo' "
                "AND title = %s AND deleted_at IS NULL LIMIT 1",
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
                    (id, project_id, created_by, kind, title, content, status,
                     created_at, updated_at, updated_at_utc)
                VALUES (%s, %s, %s, 'memo', %s, %s, %s, %s, %s, %s)
            """,
                (memo_id, data.projectId, user["id"], data.title, "", 0, now, now, now_utc),
            )

    return _serialize_memo(
        {
            "id": memo_id,
            "project_id": data.projectId,
            "created_by": user["id"],
            "kind": "memo",
            "title": data.title,
            "content": "",
            "status": 0,
            "created_at": now,
            "updated_at": now,
            "updated_at_utc": now_utc,
        }
    )


@router.get("/projects/{project_id}/backlog")
async def get_project_backlog(project_id: str, user: dict = Depends(get_current_user)):
    """leaf 프로젝트의 공유 백로그를 조회한다. 아직 없으면 null이다."""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_leaf_project(cursor, project_id)
            if not check_project_membership(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Access denied")
            cursor.execute(
                f"SELECT {MEMO_COLUMNS} FROM memos "
                "WHERE project_id = %s AND kind = 'backlog' AND deleted_at IS NULL LIMIT 1",
                (project_id,),
            )
            memo = cursor.fetchone()
            return _serialize_memo(memo) if memo else None


@router.post("/projects/{project_id}/backlog", status_code=201)
async def create_project_backlog(project_id: str, user: dict = Depends(get_current_user)):
    """프로젝트 행 잠금 + 결정적 ID로 백로그를 get-or-create한다."""
    now, now_utc = localnow_naive(), utcnow_naive()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_leaf_project(cursor, project_id, for_update=True)
            if not can_write_project(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Access denied")
            cursor.execute(
                f"SELECT {MEMO_COLUMNS} FROM memos "
                "WHERE project_id = %s AND kind = 'backlog' AND deleted_at IS NULL LIMIT 1",
                (project_id,),
            )
            existing = cursor.fetchone()
            if existing:
                return _serialize_memo(existing)
            memo_id = backlog_id(project_id)
            # 부모에서 첫 자식으로 이동한 backlog는 ID가 부모 기반이어도 kind 조회로 재사용한다.
            cursor.execute("SELECT id FROM memos WHERE id = %s", (memo_id,))
            if cursor.fetchone():
                memo_id = generate_id()
            cursor.execute(
                """
                INSERT INTO memos
                    (id, project_id, created_by, kind, title, content, status,
                     created_at, updated_at, updated_at_utc)
                VALUES (%s, %s, %s, 'backlog', %s, '', 0, %s, %s, %s)
                """,
                (memo_id, project_id, user["id"], "백로그", now, now, now_utc),
            )
            return _serialize_memo({
                "id": memo_id, "project_id": project_id, "created_by": user["id"],
                "kind": "backlog", "title": "백로그", "content": "", "status": 0,
                "created_at": now, "updated_at": now, "updated_at_utc": now_utc,
            })


@router.post("/memos/{memo_id}/move")
async def move_memo(
    memo_id: str, data: MoveMemoRequest, user: dict = Depends(get_current_user)
):
    """root creator가 같은 그룹의 형제 subproject 사이에서 일반 메모를 이동한다."""
    now_utc = utcnow_naive()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, project_id, kind, title FROM memos WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (memo_id,),
            )
            memo = cursor.fetchone()
            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")
            if memo["kind"] == "backlog":
                raise HTTPException(status_code=422, detail="Backlogs cannot be moved manually")
            cursor.execute(
                """
                SELECT source.parent_id AS source_parent, target.parent_id AS target_parent,
                       parent.owner_id AS root_owner_id
                FROM projects source
                JOIN projects target ON target.id = %s AND target.deleted_at IS NULL
                JOIN projects parent ON parent.id = source.parent_id AND parent.deleted_at IS NULL
                WHERE source.id = %s AND source.deleted_at IS NULL
                """,
                (data.targetProjectId, memo["project_id"]),
            )
            lineage = cursor.fetchone()
            if (
                not lineage
                or not lineage["source_parent"]
                or lineage["source_parent"] != lineage["target_parent"]
            ):
                raise HTTPException(status_code=422, detail="Memos can only move between sibling subprojects")
            if lineage["root_owner_id"] != user["id"]:
                raise HTTPException(status_code=403, detail="Only the root project creator can move memos")
            if data.targetProjectId == memo["project_id"]:
                raise HTTPException(status_code=409, detail="Memo is already in the target project")
            cursor.execute(
                "SELECT id FROM memos WHERE project_id = %s AND kind = 'memo' "
                "AND title = %s AND id <> %s AND deleted_at IS NULL LIMIT 1",
                (data.targetProjectId, memo["title"], memo_id),
            )
            duplicate = cursor.fetchone()
            if duplicate:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "동일한 제목의 메모가 대상 서브프로젝트에 이미 존재합니다.",
                        "existingMemoId": duplicate["id"],
                    },
                )
            cursor.execute(
                "UPDATE memos SET project_id = %s, updated_at = updated_at, updated_at_utc = %s WHERE id = %s",
                (data.targetProjectId, now_utc, memo_id),
            )
            cursor.execute("UPDATE articles SET project_id = %s WHERE memo_id = %s", (data.targetProjectId, memo_id))
            cursor.execute(f"SELECT {MEMO_COLUMNS} FROM memos WHERE id = %s", (memo_id,))
            result = _serialize_memo(cursor.fetchone())
    realtime = get_realtime_server()
    if realtime is not None and realtime.available:
        await realtime.revalidate_memo_access(memo_id)
        await realtime.emit_projects_changed()
    return result


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

            _require_leaf_project(cursor, memo["project_id"])
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
                "SELECT * FROM memos WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (memo_id,),
            )
            memo = cursor.fetchone()

            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            _require_leaf_project(cursor, memo["project_id"])
            if not can_write_project(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            _require_memo_write_lease(memo_id, user["id"], lease_token)

            current_revision = _memo_revision(memo)
            if (
                data.baseRevision is not None
                and data.baseRevision != current_revision
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "memo_content_conflict",
                        "message": "Memo content changed after this edit was based on it.",
                        "current": _serialize_memo(memo),
                    },
                )

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
async def get_memo_versions(
    memo_id: str,
    include_content: bool = Query(default=True, alias="includeContent"),
    user: dict = Depends(get_current_user),
):
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

            _require_leaf_project(cursor, memo["project_id"])
            if not check_project_membership(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            content_column = ", content" if include_content else ""
            cursor.execute(
                f"""
                SELECT id, memo_id, version, note, created_at, updated_at_utc{content_column}
                FROM memo_versions
                WHERE memo_id = %s
                ORDER BY version DESC
            """,
                (memo_id,),
            )
            serializer = (
                _serialize_version if include_content else _serialize_version_summary
            )
            return [serializer(version) for version in cursor.fetchall()]


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

            _require_leaf_project(cursor, memo["project_id"])
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
                _require_leaf_project(cursor, memo["project_id"])
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

            _require_leaf_project(cursor, memo["project_id"])
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
                if not memo:
                    continue
                try:
                    _require_leaf_project(cursor, memo["project_id"])
                except HTTPException:
                    continue
                if not can_manage_project(cursor, memo["project_id"], user):
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
