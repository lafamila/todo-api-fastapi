from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

try:
    from ..auth_utils import get_current_user, require_admin
    from ..connectors import get_db_connection
    from ..models.base import (
        BulkDeleteMemosRequest,
        CreateMemoRequest,
        UpdateMemoRequest,
    )
    from ..utils import can_manage_project, can_write_project, check_project_membership, generate_id
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user, require_admin
    from connectors import get_db_connection
    from models.base import (
        BulkDeleteMemosRequest,
        CreateMemoRequest,
        UpdateMemoRequest,
    )
    from utils import can_manage_project, can_write_project, check_project_membership, generate_id


router = APIRouter(prefix="/api", tags=["memos"])


@router.get("/projects/{project_id}/memos")
async def get_project_memos(project_id: str, user: dict = Depends(get_current_user)):
    """특정 프로젝트의 메모 목록 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM projects WHERE id = %s", (project_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Project not found")

            if not check_project_membership(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                """
                SELECT id, project_id, created_by, title, content, status, created_at, updated_at
                FROM memos
                WHERE project_id = %s AND deleted_at IS NULL
                ORDER BY created_at DESC
            """,
                (project_id,),
            )
            memos = cursor.fetchall()

            return [
                {
                    "id": m["id"],
                    "projectId": m["project_id"],
                    "createdBy": m["created_by"],
                    "title": m["title"],
                    "content": m["content"],
                    "status": m["status"],
                    "createdAt": m["created_at"].isoformat(),
                    "updatedAt": m["updated_at"].isoformat(),
                }
                for m in memos
            ]


@router.post("/memos", status_code=201)
async def create_memo(data: CreateMemoRequest, user: dict = Depends(get_current_user)):
    """메모 생성"""
    memo_id = generate_id()
    now = datetime.now()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM projects WHERE id = %s", (data.projectId,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Project not found")

            if not can_write_project(cursor, data.projectId, user):
                raise HTTPException(status_code=403, detail="Access denied")

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
                INSERT INTO memos (id, project_id, created_by, title, content, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (memo_id, data.projectId, user["id"], data.title, "", 0, now, now),
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
    }


@router.get("/memos/{memo_id}")
async def get_memo(memo_id: str, user: dict = Depends(get_current_user)):
    """메모 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, project_id, created_by, title, content, status, created_at, updated_at
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

            return {
                "id": memo["id"],
                "projectId": memo["project_id"],
                "createdBy": memo["created_by"],
                "title": memo["title"],
                "content": memo["content"],
                "status": memo["status"],
                "createdAt": memo["created_at"].isoformat(),
                "updatedAt": memo["updated_at"].isoformat(),
            }


@router.put("/memos/{memo_id}")
async def update_memo(
    memo_id: str, data: UpdateMemoRequest, user: dict = Depends(get_current_user)
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

            if memo["content"]:
                cursor.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) as max_version
                    FROM memo_versions
                    WHERE memo_id = %s
                """,
                    (memo_id,),
                )
                result = cursor.fetchone()
                next_version = result["max_version"] + 1

                version_id = generate_id()
                cursor.execute(
                    """
                    INSERT INTO memo_versions (id, memo_id, content, version, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                """,
                    (
                        version_id,
                        memo_id,
                        memo["content"],
                        next_version,
                        datetime.now(),
                    ),
                )

            cursor.execute(
                """
                UPDATE memos
                SET content = %s, updated_at = %s
                WHERE id = %s
            """,
                (data.content, datetime.now(), memo_id),
            )

            cursor.execute(
                """
                SELECT id, project_id, created_by, title, content, status, created_at, updated_at
                FROM memos
                WHERE id = %s AND deleted_at IS NULL
            """,
                (memo_id,),
            )
            updated_memo = cursor.fetchone()

            return {
                "id": updated_memo["id"],
                "projectId": updated_memo["project_id"],
                "createdBy": updated_memo["created_by"],
                "title": updated_memo["title"],
                "content": updated_memo["content"],
                "status": updated_memo["status"],
                "createdAt": updated_memo["created_at"].isoformat(),
                "updatedAt": updated_memo["updated_at"].isoformat(),
            }


@router.get("/memos/{memo_id}/versions")
async def get_memo_versions(memo_id: str, user: dict = Depends(get_current_user)):
    """메모의 버전 히스토리 조회"""
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
                SELECT id, memo_id, content, version, created_at
                FROM memo_versions
                WHERE memo_id = %s
                ORDER BY version DESC
            """,
                (memo_id,),
            )
            versions = cursor.fetchall()

            return [
                {
                    "id": v["id"],
                    "memoId": v["memo_id"],
                    "content": v["content"],
                    "version": v["version"],
                    "createdAt": v["created_at"].isoformat(),
                }
                for v in versions
            ]


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
                SELECT id, memo_id, content, version, created_at
                FROM memo_versions
                WHERE memo_id = %s AND version = %s
            """,
                (memo_id, version),
            )
            version_data = cursor.fetchone()

            if not version_data:
                raise HTTPException(status_code=404, detail="Version not found")

            return {
                "id": version_data["id"],
                "memoId": version_data["memo_id"],
                "content": version_data["content"],
                "version": version_data["version"],
                "createdAt": version_data["created_at"].isoformat(),
            }


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

            cursor.execute(
                "UPDATE memos SET deleted_at = %s WHERE id = %s",
                (datetime.now(), memo_id),
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
            now = datetime.now()
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
                    "UPDATE memos SET deleted_at = %s WHERE id = %s AND deleted_at IS NULL",
                    (now, memo_id),
                )
                deleted_count += cursor.rowcount

            return {
                "message": f"{deleted_count}개의 메모가 삭제되었습니다.",
                "deletedCount": deleted_count,
            }
