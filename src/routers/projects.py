from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

try:
    from ..auth_utils import get_current_user, require_admin
    from ..connectors import get_db_connection
    from ..models.base import (
        CreateProjectRequest,
        InviteMemberRequest,
        VerifyPasswordRequest,
    )
    from ..utils import can_manage_project, generate_id
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user, require_admin
    from connectors import get_db_connection
    from models.base import (
        CreateProjectRequest,
        InviteMemberRequest,
        VerifyPasswordRequest,
    )
    from utils import can_manage_project, generate_id


router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("/{project_id}/members", status_code=201)
async def invite_member(
    project_id: str, data: InviteMemberRequest, user: dict = Depends(require_admin)
):
    """프로젝트 멤버 초대"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, owner_id FROM projects WHERE id = %s", (project_id,)
            )
            project = cursor.fetchone()
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")
            if not can_manage_project(cursor, project_id, user):
                raise HTTPException(
                    status_code=403, detail="Only project owner can invite members"
                )

            cursor.execute(
                "SELECT id FROM project_members WHERE project_id = %s AND user_id = %s",
                (project_id, data.userId),
            )
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail="User is already a member")

            member_id = generate_id()
            now = datetime.now()
            cursor.execute(
                """
                INSERT INTO project_members
                    (id, project_id, user_id, username, display_name, email, role, invited_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
    """프로젝트 멤버 제거"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, owner_id FROM projects WHERE id = %s", (project_id,)
            )
            project = cursor.fetchone()
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")
            if not can_manage_project(cursor, project_id, user):
                raise HTTPException(
                    status_code=403, detail="Only project owner can remove members"
                )

            cursor.execute(
                "SELECT role FROM project_members WHERE project_id = %s AND user_id = %s",
                (project_id, member_user_id),
            )
            member = cursor.fetchone()
            if member and member["role"] == "owner":
                raise HTTPException(status_code=400, detail="Project owner cannot be removed")

            cursor.execute(
                "DELETE FROM project_members WHERE project_id = %s AND user_id = %s",
                (project_id, member_user_id),
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
            cursor.execute("SELECT id FROM projects WHERE id = %s", (project_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Project not found")

            if not user["is_admin"]:
                cursor.execute(
                    "SELECT id FROM project_members WHERE project_id = %s AND user_id = %s",
                    (project_id, user["id"]),
                )
                if not cursor.fetchone():
                    raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                """
                SELECT pm.id, pm.project_id, pm.user_id, pm.username,
                       pm.display_name, pm.email, pm.role, pm.invited_at
                FROM project_members pm
                WHERE pm.project_id = %s
                ORDER BY pm.invited_at
            """,
                (project_id,),
            )
            members = cursor.fetchall()
            return [
                {
                    "id": m["id"],
                    "projectId": m["project_id"],
                    "userId": m["user_id"],
                    "role": m["role"],
                    "invitedAt": m["invited_at"].isoformat(),
                    "username": m["username"] or m["email"] or m["user_id"],
                    "displayName": m["display_name"] or m["username"] or m["user_id"],
                    "isAdmin": m["role"] == "owner",
                }
                for m in members
            ]


@router.get("")
async def get_projects(user: dict = Depends(get_current_user)):
    """프로젝트 목록 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if user.get("permission") == "owner":
                cursor.execute(
                    """
                    SELECT DISTINCT p.id, p.name, p.icon, p.status, p.is_secret, p.owner_id, p.created_at, p.updated_at
                    FROM projects p
                    ORDER BY p.created_at ASC
                """
                )
            elif user["is_admin"]:
                cursor.execute(
                    """
                    SELECT DISTINCT p.id, p.name, p.icon, p.status, p.is_secret, p.owner_id, p.created_at, p.updated_at
                    FROM projects p
                    LEFT JOIN project_members pm ON p.id = pm.project_id AND pm.user_id = %s
                    WHERE p.owner_id = %s OR pm.user_id = %s
                    ORDER BY p.created_at ASC
                """,
                    (user["id"], user["id"], user["id"]),
                )
            else:
                cursor.execute(
                    """
                    SELECT p.id, p.name, p.icon, p.status, p.is_secret, p.owner_id, p.created_at, p.updated_at
                    FROM projects p
                    JOIN project_members pm ON p.id = pm.project_id
                    WHERE pm.user_id = %s
                    ORDER BY p.created_at ASC
                """,
                    (user["id"],),
                )

            projects = cursor.fetchall()

            return [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "icon": p["icon"],
                    "status": p["status"],
                    "isSecret": bool(p["is_secret"]),
                    "ownerId": p["owner_id"],
                    "createdAt": p["created_at"].isoformat(),
                    "updatedAt": p["updated_at"].isoformat(),
                }
                for p in projects
            ]


@router.post("", status_code=201)
async def create_project(
    data: CreateProjectRequest, user: dict = Depends(require_admin)
):
    """프로젝트 생성"""
    project_id = generate_id()
    member_id = generate_id()
    now = datetime.now()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO projects (id, owner_id, name, icon, status, is_secret, password, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )
            cursor.execute(
                """
                INSERT INTO project_members
                    (id, project_id, user_id, username, display_name, email, role, invited_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
    }


@router.post("/{project_id}/verify")
async def verify_project_password(
    project_id: str, data: VerifyPasswordRequest, user: dict = Depends(get_current_user)
):
    """프로젝트 비밀번호 검증"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT password FROM projects WHERE id = %s", (project_id,))
            project = cursor.fetchone()

            if not project:
                raise HTTPException(status_code=404, detail="Project not found")

            verified = project["password"] == data.password
            return {"verified": verified}
