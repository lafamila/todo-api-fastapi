from fastapi import APIRouter, Depends, HTTPException

try:
    from ..auth_utils import get_current_user, require_admin
    from ..connectors import get_db_connection
    from ..models.base import (
        CreateProjectRequest,
        InviteMemberRequest,
        VerifyPasswordRequest,
    )
    from ..services.merge import MergeError, run_merge
    from ..timeutil import iso_utc, localnow_naive, utcnow_naive
    from ..utils import can_manage_project, generate_id
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user, require_admin
    from connectors import get_db_connection
    from models.base import (
        CreateProjectRequest,
        InviteMemberRequest,
        VerifyPasswordRequest,
    )
    from services.merge import MergeError, run_merge
    from timeutil import iso_utc, localnow_naive, utcnow_naive
    from utils import can_manage_project, generate_id


router = APIRouter(prefix="/api/projects", tags=["projects"])

PROJECT_COLUMNS = (
    "p.id, p.name, p.icon, p.status, p.is_secret, p.owner_id, "
    "p.created_at, p.updated_at, p.updated_at_utc"
)


def _serialize_project(project: dict) -> dict:
    return {
        "id": project["id"],
        "name": project["name"],
        "icon": project["icon"],
        "status": project["status"],
        "isSecret": bool(project["is_secret"]),
        "ownerId": project["owner_id"],
        "createdAt": project["created_at"].isoformat(),
        "updatedAt": project["updated_at"].isoformat(),
        "updatedAtUtc": iso_utc(project.get("updated_at_utc")),
    }


def _serialize_member(member: dict) -> dict:
    return {
        "id": member["id"],
        "projectId": member["project_id"],
        "userId": member["user_id"],
        "role": member["role"],
        "invitedAt": member["invited_at"].isoformat(),
        "username": member["username"] or member["email"] or member["user_id"],
        "displayName": member["display_name"] or member["username"] or member["user_id"],
        "isAdmin": member["role"] == "owner",
    }


def _require_live_project(cursor, project_id: str, columns: str = "id, owner_id") -> dict:
    cursor.execute(
        f"SELECT {columns} FROM projects WHERE id = %s AND deleted_at IS NULL",
        (project_id,),
    )
    project = cursor.fetchone()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/{project_id}/members", status_code=201)
async def invite_member(
    project_id: str, data: InviteMemberRequest, user: dict = Depends(require_admin)
):
    """프로젝트 멤버 초대"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_project(cursor, project_id)
            if not can_manage_project(cursor, project_id, user):
                raise HTTPException(
                    status_code=403, detail="Only project owner can invite members"
                )

            now = localnow_naive()
            now_utc = utcnow_naive()

            # soft delete 로 전환했으므로 UNIQUE(project_id, user_id) 에 tombstone 이
            # 남아 있을 수 있다. 그 행을 되살리는 것이 올바른 재초대다.
            cursor.execute(
                "SELECT id, deleted_at FROM project_members WHERE project_id = %s AND user_id = %s",
                (project_id, data.userId),
            )
            existing = cursor.fetchone()
            if existing and existing["deleted_at"] is None:
                raise HTTPException(status_code=409, detail="User is already a member")

            if existing:
                member_id = existing["id"]
                cursor.execute(
                    """
                    UPDATE project_members
                    SET deleted_at = NULL, username = %s, display_name = %s, email = %s,
                        role = %s, invited_at = %s, updated_at_utc = %s
                    WHERE id = %s
                    """,
                    (
                        data.username,
                        data.displayName,
                        data.email,
                        data.role,
                        now,
                        now_utc,
                        member_id,
                    ),
                )
            else:
                member_id = generate_id()
                cursor.execute(
                    """
                    INSERT INTO project_members
                        (id, project_id, user_id, username, display_name, email, role,
                         invited_at, updated_at_utc)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        member_id,
                        project_id,
                        data.userId,
                        data.username,
                        data.displayName,
                        data.email,
                        data.role,
                        now,
                        now_utc,
                    ),
                )

            return {
                "id": member_id,
                "projectId": project_id,
                "userId": data.userId,
                "role": data.role,
                "invitedAt": now.isoformat(),
                "username": data.username or data.email or data.userId,
                "displayName": data.displayName or data.username or data.userId,
                "isAdmin": data.role == "owner",
            }


@router.delete("/{project_id}/members/{member_user_id}")
async def remove_member(
    project_id: str, member_user_id: str, user: dict = Depends(require_admin)
):
    """프로젝트 멤버 제거 (소프트 삭제 — tombstone 이 상대 노드로 전파된다)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_project(cursor, project_id)
            if not can_manage_project(cursor, project_id, user):
                raise HTTPException(
                    status_code=403, detail="Only project owner can remove members"
                )

            cursor.execute(
                "SELECT role FROM project_members "
                "WHERE project_id = %s AND user_id = %s AND deleted_at IS NULL",
                (project_id, member_user_id),
            )
            member = cursor.fetchone()
            if member and member["role"] == "owner":
                raise HTTPException(status_code=400, detail="Project owner cannot be removed")

            now_utc = utcnow_naive()
            cursor.execute(
                "UPDATE project_members SET deleted_at = %s, updated_at_utc = %s "
                "WHERE project_id = %s AND user_id = %s AND deleted_at IS NULL",
                (now_utc, now_utc, project_id, member_user_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Member not found")
            return {"message": "Member removed successfully"}


@router.get("/{project_id}/members")
async def get_project_members(
    project_id: str, user: dict = Depends(get_current_user)
):
    """프로젝트 멤버 목록 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_project(cursor, project_id, columns="id")

            if not user["is_admin"]:
                cursor.execute(
                    "SELECT id FROM project_members "
                    "WHERE project_id = %s AND user_id = %s AND deleted_at IS NULL",
                    (project_id, user["id"]),
                )
                if not cursor.fetchone():
                    raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                """
                SELECT pm.id, pm.project_id, pm.user_id, pm.username,
                       pm.display_name, pm.email, pm.role, pm.invited_at
                FROM project_members pm
                WHERE pm.project_id = %s AND pm.deleted_at IS NULL
                ORDER BY pm.invited_at
            """,
                (project_id,),
            )
            return [_serialize_member(member) for member in cursor.fetchall()]


@router.get("")
async def get_projects(user: dict = Depends(get_current_user)):
    """프로젝트 목록 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if user.get("permission") == "owner":
                cursor.execute(
                    f"""
                    SELECT DISTINCT {PROJECT_COLUMNS}
                    FROM projects p
                    WHERE p.deleted_at IS NULL
                    ORDER BY p.created_at ASC
                """
                )
            elif user["is_admin"]:
                cursor.execute(
                    f"""
                    SELECT DISTINCT {PROJECT_COLUMNS}
                    FROM projects p
                    LEFT JOIN project_members pm
                           ON p.id = pm.project_id AND pm.user_id = %s AND pm.deleted_at IS NULL
                    WHERE p.deleted_at IS NULL AND (p.owner_id = %s OR pm.user_id = %s)
                    ORDER BY p.created_at ASC
                """,
                    (user["id"], user["id"], user["id"]),
                )
            else:
                cursor.execute(
                    f"""
                    SELECT {PROJECT_COLUMNS}
                    FROM projects p
                    JOIN project_members pm ON p.id = pm.project_id
                    WHERE pm.user_id = %s AND pm.deleted_at IS NULL AND p.deleted_at IS NULL
                    ORDER BY p.created_at ASC
                """,
                    (user["id"],),
                )

            return [_serialize_project(project) for project in cursor.fetchall()]


@router.post("", status_code=201)
async def create_project(
    data: CreateProjectRequest, user: dict = Depends(require_admin)
):
    """프로젝트 생성"""
    project_id = generate_id()
    member_id = generate_id()
    now = localnow_naive()
    now_utc = utcnow_naive()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO projects
                    (id, owner_id, name, icon, status, is_secret, password,
                     created_at, updated_at, updated_at_utc)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    project_id,
                    user["id"],
                    data.name,
                    data.icon,
                    0,
                    data.isSecret,
                    data.password,
                    now,
                    now,
                    now_utc,
                ),
            )
            cursor.execute(
                """
                INSERT INTO project_members
                    (id, project_id, user_id, username, display_name, email, role,
                     invited_at, updated_at_utc)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    member_id,
                    project_id,
                    user["id"],
                    user.get("username"),
                    user.get("display_name"),
                    user.get("email"),
                    "owner",
                    now,
                    now_utc,
                ),
            )

    return {
        "id": project_id,
        "name": data.name,
        "icon": data.icon,
        "status": 0,
        "isSecret": data.isSecret,
        "ownerId": user["id"],
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
        "updatedAtUtc": iso_utc(now_utc),
    }


@router.post("/{loser_id}/merge-into/{winner_id}")
async def merge_project_into(
    loser_id: str, winner_id: str, user: dict = Depends(require_admin)
):
    """중복 프로젝트 병합 — 패자 메모 재부모화 → 멤버 합치기 → 패자 tombstone.

    오프라인에서는 잠긴다 (409). 온라인 클라이언트는 원격에 위임하고 결과를 pull 로 받는다.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            for project_id in (loser_id, winner_id):
                _require_live_project(cursor, project_id, columns="id")
                if not can_manage_project(cursor, project_id, user):
                    raise HTTPException(status_code=403, detail="Access denied")

    try:
        return await run_merge("project", loser_id, winner_id)
    except MergeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.delete("/{project_id}")
async def delete_project(project_id: str, user: dict = Depends(require_admin)):
    """프로젝트 소프트 삭제 — 메모/멤버 tombstone 까지 함께 남긴다.

    하드 삭제(FK CASCADE)를 쓰면 tombstone 이 없어 상대 노드에서 다음 동기화에 되살아난다.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_project(cursor, project_id, columns="id")
            if not can_manage_project(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Access denied")

            now_utc = utcnow_naive()
            cursor.execute(
                "UPDATE memos SET deleted_at = %s, updated_at_utc = %s "
                "WHERE project_id = %s AND deleted_at IS NULL",
                (now_utc, now_utc, project_id),
            )
            deleted_memos = cursor.rowcount
            cursor.execute(
                "UPDATE project_members SET deleted_at = %s, updated_at_utc = %s "
                "WHERE project_id = %s AND deleted_at IS NULL",
                (now_utc, now_utc, project_id),
            )
            deleted_members = cursor.rowcount
            cursor.execute(
                "UPDATE projects SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
                (now_utc, now_utc, project_id),
            )
            return {
                "message": "Project soft-deleted successfully",
                "deletedMemos": deleted_memos,
                "deletedMembers": deleted_members,
            }


@router.post("/{project_id}/verify")
async def verify_project_password(
    project_id: str, data: VerifyPasswordRequest, user: dict = Depends(get_current_user)
):
    """프로젝트 비밀번호 검증"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT password FROM projects WHERE id = %s AND deleted_at IS NULL",
                (project_id,),
            )
            project = cursor.fetchone()

            if not project:
                raise HTTPException(status_code=404, detail="Project not found")

            verified = project["password"] == data.password
            return {"verified": verified}
