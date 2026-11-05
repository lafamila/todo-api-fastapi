from fastapi import APIRouter, Depends, HTTPException

try:
    from ..auth_utils import get_current_user, require_admin
    from ..connectors import get_db_connection
    from ..models.base import (
        CreateProjectRequest,
        DeleteProjectRequest,
        InviteMemberRequest,
        UpdateProjectRequest,
        VerifyPasswordRequest,
    )
    from ..services.merge import MergeError, run_merge
    from ..services.realtime import get_realtime_server
    from ..timeutil import iso_utc, localnow_naive, utcnow_naive
    from ..utils import backlog_id, can_manage_project, generate_id
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user, require_admin
    from connectors import get_db_connection
    from models.base import (
        CreateProjectRequest,
        DeleteProjectRequest,
        InviteMemberRequest,
        UpdateProjectRequest,
        VerifyPasswordRequest,
    )
    from services.merge import MergeError, run_merge
    from services.realtime import get_realtime_server
    from timeutil import iso_utc, localnow_naive, utcnow_naive
    from utils import backlog_id, can_manage_project, generate_id


router = APIRouter(prefix="/api/projects", tags=["projects"])
PROJECT_COLUMNS = (
    "p.id, p.name, p.icon, p.status, p.is_secret, p.owner_id, p.parent_id, "
    "p.is_group, p.created_at, p.updated_at, p.updated_at_utc"
)


def _serialize_project(project: dict, user: dict | None = None) -> dict:
    role = project.get("role")
    if user and not project.get("navigation_only"):
        if user.get("permission") == "owner" or project.get("owner_id") == user.get("id"):
            role = "owner"
    return {
        "id": project["id"], "name": project["name"], "icon": project["icon"],
        "status": project["status"], "isSecret": bool(project["is_secret"]),
        "ownerId": project["owner_id"], "parentProjectId": project.get("parent_id"),
        "isGroup": bool(project.get("is_group")), "role": role,
        "canManage": role == "owner",
        "canDelete": bool(user and project.get("owner_id") == user.get("id")),
        "canWrite": role in {"owner", "editor"} and not bool(project.get("is_group")),
        "navigationOnly": bool(project.get("navigation_only")),
        "createdAt": project["created_at"].isoformat(),
        "updatedAt": project["updated_at"].isoformat(),
        "updatedAtUtc": iso_utc(project.get("updated_at_utc")),
    }


def _serialize_member(member: dict) -> dict:
    return {
        "id": member["id"], "projectId": member["project_id"], "userId": member["user_id"],
        "role": member["role"], "invitedAt": member["invited_at"].isoformat(),
        "username": member["username"] or member["email"] or member["user_id"],
        "displayName": member["display_name"] or member["username"] or member["user_id"],
        "isAdmin": member["role"] == "owner",
    }


def _require_live_project(cursor, project_id: str, *, for_update: bool = False) -> dict:
    suffix = " FOR UPDATE" if for_update else ""
    cursor.execute(
        f"""
        SELECT p.id, p.owner_id, p.name, p.icon, p.status, p.is_secret, p.password,
               p.parent_id, p.is_group, p.created_at, p.updated_at, p.updated_at_utc
        FROM projects p LEFT JOIN projects parent ON parent.id = p.parent_id
        WHERE p.id = %s AND p.deleted_at IS NULL
          AND (p.parent_id IS NULL OR (parent.id IS NOT NULL AND parent.deleted_at IS NULL)){suffix}
        """, (project_id,),
    )
    project = cursor.fetchone()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _upsert_invited_member(cursor, project_id: str, data: InviteMemberRequest, now, now_utc) -> tuple[str, bool]:
    cursor.execute(
        "SELECT id, role, deleted_at FROM project_members WHERE project_id = %s AND user_id = %s",
        (project_id, data.userId),
    )
    existing = cursor.fetchone()
    if existing and existing["deleted_at"] is None:
        return existing["id"], False
    role = "owner" if existing and existing["role"] == "owner" else data.role
    if existing:
        member_id = existing["id"]
        cursor.execute(
            """UPDATE project_members SET deleted_at = NULL, username = %s, display_name = %s,
               email = %s, role = %s, invited_at = %s, updated_at_utc = %s WHERE id = %s""",
            (data.username, data.displayName, data.email, role, now, now_utc, member_id),
        )
    else:
        member_id = generate_id()
        cursor.execute(
            """INSERT INTO project_members
               (id, project_id, user_id, username, display_name, email, role, invited_at, updated_at_utc)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (member_id, project_id, data.userId, data.username, data.displayName, data.email,
             role, now, now_utc),
        )
    return member_id, True


@router.post("/{project_id}/members", status_code=201)
async def invite_member(project_id: str, data: InviteMemberRequest, user: dict = Depends(get_current_user)):
    if data.includeAllSubprojects and data.childProjectIds:
        raise HTTPException(status_code=422, detail="Choose all subprojects or explicit subprojects, not both")
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            project = _require_live_project(cursor, project_id, for_update=True)
            if not can_manage_project(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Only project owner can invite members")
            target_ids = [project_id]
            if project["parent_id"] is None:
                cursor.execute(
                    "SELECT id FROM projects WHERE parent_id = %s AND deleted_at IS NULL ORDER BY created_at, id",
                    (project_id,),
                )
                child_ids = [row["id"] for row in cursor.fetchall()]
                requested = child_ids if data.includeAllSubprojects else data.childProjectIds
                if set(requested) - set(child_ids):
                    raise HTTPException(status_code=422, detail="childProjectIds must be live direct subprojects")
                target_ids.extend(dict.fromkeys(requested))
            elif data.includeAllSubprojects or data.childProjectIds:
                raise HTTPException(status_code=422, detail="Subproject invitations cannot include sibling projects")

            now, now_utc = localnow_naive(), utcnow_naive()
            member_id, changed_ids = None, []
            for target_id in target_ids:
                current_id, changed = _upsert_invited_member(cursor, target_id, data, now, now_utc)
                if target_id == project_id:
                    member_id = current_id
                if changed:
                    changed_ids.append(target_id)
            if not changed_ids:
                raise HTTPException(status_code=409, detail="User is already a member of every selected project")
            result = {
                "id": member_id, "projectId": project_id, "userId": data.userId,
                "role": data.role, "invitedAt": now.isoformat(),
                "username": data.username or data.email or data.userId,
                "displayName": data.displayName or data.username or data.userId,
                "isAdmin": False, "invitedProjectIds": changed_ids,
            }
    realtime = get_realtime_server()
    if realtime is not None and realtime.available:
        await realtime.emit_projects_changed()
    return result


@router.delete("/{project_id}/members/{member_user_id}")
async def remove_member(project_id: str, member_user_id: str, user: dict = Depends(get_current_user)):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_project(cursor, project_id)
            if not can_manage_project(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Only project owner can remove members")
            cursor.execute(
                "SELECT role FROM project_members WHERE project_id = %s AND user_id = %s AND deleted_at IS NULL",
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
    realtime = get_realtime_server()
    if realtime is not None and realtime.available:
        await realtime.revoke_project_access([project_id])
        await realtime.emit_projects_changed()
    return {"message": "Member removed successfully"}


@router.get("/{project_id}/members")
async def get_project_members(project_id: str, user: dict = Depends(get_current_user)):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _require_live_project(cursor, project_id)
            if not can_manage_project(cursor, project_id, user):
                cursor.execute(
                    "SELECT id FROM project_members WHERE project_id = %s AND user_id = %s AND deleted_at IS NULL",
                    (project_id, user["id"]),
                )
                if not cursor.fetchone():
                    raise HTTPException(status_code=403, detail="Access denied")
            cursor.execute(
                """SELECT id, project_id, user_id, username, display_name, email, role, invited_at
                   FROM project_members WHERE project_id = %s AND deleted_at IS NULL ORDER BY invited_at""",
                (project_id,),
            )
            return [_serialize_member(member) for member in cursor.fetchall()]


@router.get("")
async def get_projects(user: dict = Depends(get_current_user)):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if user.get("permission") == "owner":
                cursor.execute(
                    f"""SELECT {PROJECT_COLUMNS}, 'owner' AS role, 0 AS navigation_only
                       FROM projects p LEFT JOIN projects parent ON parent.id = p.parent_id
                       WHERE p.deleted_at IS NULL
                         AND (p.parent_id IS NULL OR (parent.id IS NOT NULL AND parent.deleted_at IS NULL))
                       ORDER BY COALESCE(parent.created_at, p.created_at), p.parent_id IS NOT NULL, p.created_at"""
                )
            else:
                # parent는 PK, pm은 UNIQUE(project_id, user_id) 조인이므로 중복이 없다.
                # DISTINCT를 붙이면 MySQL 8에서 parent.created_at 정렬이 오류 3065가 된다.
                cursor.execute(
                    f"""SELECT {PROJECT_COLUMNS},
                         CASE WHEN p.owner_id = %s THEN 'owner' ELSE pm.role END AS role,
                         CASE WHEN p.owner_id = %s OR pm.id IS NOT NULL THEN 0 ELSE 1 END AS navigation_only
                       FROM projects p
                       LEFT JOIN projects parent ON parent.id = p.parent_id
                       LEFT JOIN project_members pm ON pm.project_id = p.id AND pm.user_id = %s AND pm.deleted_at IS NULL
                       WHERE p.deleted_at IS NULL
                         AND (p.parent_id IS NULL OR (parent.id IS NOT NULL AND parent.deleted_at IS NULL))
                         AND (p.owner_id = %s OR pm.id IS NOT NULL OR
                           (p.parent_id IS NULL AND EXISTS (
                             SELECT 1 FROM projects child JOIN project_members child_pm ON child_pm.project_id = child.id
                             WHERE child.parent_id = p.id AND child.deleted_at IS NULL
                               AND child_pm.user_id = %s AND child_pm.deleted_at IS NULL)))
                       ORDER BY COALESCE(parent.created_at, p.created_at), p.parent_id IS NOT NULL, p.created_at""",
                    (user["id"], user["id"], user["id"], user["id"], user["id"]),
                )
            return [_serialize_project(project, user) for project in cursor.fetchall()]


@router.post("", status_code=201)
async def create_project(data: CreateProjectRequest, user: dict = Depends(get_current_user)):
    if not data.name.strip():
        raise HTTPException(status_code=422, detail="Project name cannot be empty")
    project_id, member_id = generate_id(), generate_id()
    now, now_utc = localnow_naive(), utcnow_naive()
    if not data.parentProjectId and user.get("permission") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Admin access required")
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            parent, first_child, owner_id = None, False, user["id"]
            if data.parentProjectId:
                parent = _require_live_project(cursor, data.parentProjectId, for_update=True)
                if parent["parent_id"] is not None:
                    raise HTTPException(status_code=422, detail="Subprojects can only be one level deep")
                if parent["owner_id"] != user["id"]:
                    raise HTTPException(status_code=403, detail="Only the root project creator can create subprojects")
                owner_id = parent["owner_id"]
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM projects WHERE parent_id = %s AND deleted_at IS NULL",
                    (parent["id"],),
                )
                first_child = int(cursor.fetchone()["count"]) == 0 and not bool(parent["is_group"])

            cursor.execute(
                """INSERT INTO projects
                   (id, owner_id, parent_id, is_group, name, icon, status, is_secret, password,
                    created_at, updated_at, updated_at_utc)
                   VALUES (%s, %s, %s, 0, %s, %s, 0, %s, %s, %s, %s, %s)""",
                (project_id, owner_id, data.parentProjectId, data.name.strip(), data.icon,
                 data.isSecret, data.password, now, now, now_utc),
            )
            if first_child and parent:
                cursor.execute(
                    "UPDATE projects SET is_group = 1, updated_at = %s, updated_at_utc = %s WHERE id = %s",
                    (now, now_utc, parent["id"]),
                )
                cursor.execute(
                    "UPDATE memos SET project_id = %s, updated_at = updated_at, updated_at_utc = %s "
                    "WHERE project_id = %s",
                    (project_id, now_utc, parent["id"]),
                )
                cursor.execute("UPDATE articles SET project_id = %s WHERE project_id = %s", (project_id, parent["id"]))
                cursor.execute(
                    """SELECT user_id, username, display_name, email, role, invited_at
                       FROM project_members WHERE project_id = %s AND deleted_at IS NULL""",
                    (parent["id"],),
                )
                members = cursor.fetchall()
                copied_owner = False
                for member in members:
                    child_member_id = member_id if member["user_id"] == owner_id else generate_id()
                    copied_owner = copied_owner or member["user_id"] == owner_id
                    cursor.execute(
                        """INSERT INTO project_members
                           (id, project_id, user_id, username, display_name, email, role, invited_at, updated_at_utc)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (child_member_id, project_id, member["user_id"], member["username"],
                         member["display_name"], member["email"], member["role"],
                         member["invited_at"], now_utc),
                    )
                if not copied_owner:
                    cursor.execute(
                        """INSERT INTO project_members
                           (id, project_id, user_id, username, display_name, email, role, invited_at, updated_at_utc)
                           VALUES (%s, %s, %s, %s, %s, %s, 'owner', %s, %s)""",
                        (member_id, project_id, owner_id, user.get("username"),
                         user.get("display_name"), user.get("email"), now, now_utc),
                    )
            else:
                cursor.execute(
                    """INSERT INTO project_members
                       (id, project_id, user_id, username, display_name, email, role, invited_at, updated_at_utc)
                       VALUES (%s, %s, %s, %s, %s, %s, 'owner', %s, %s)""",
                    (member_id, project_id, owner_id, user.get("username"), user.get("display_name"),
                     user.get("email"), now, now_utc),
                )
            cursor.execute(
                "SELECT id FROM memos WHERE project_id = %s AND kind = 'backlog' "
                "AND created_by = %s AND deleted_at IS NULL LIMIT 1",
                (project_id, owner_id),
            )
            if not cursor.fetchone():
                cursor.execute(
                    """INSERT INTO memos
                       (id, project_id, created_by, kind, title, content, status,
                        created_at, updated_at, updated_at_utc)
                       VALUES (%s, %s, %s, 'backlog', '백로그', '', 0, %s, %s, %s)""",
                    (
                        backlog_id(project_id, owner_id),
                        project_id,
                        owner_id,
                        now,
                        now,
                        now_utc,
                    ),
                )
    result = _serialize_project({
        "id": project_id, "name": data.name.strip(), "icon": data.icon, "status": 0,
        "is_secret": data.isSecret, "owner_id": owner_id, "parent_id": data.parentProjectId,
        "is_group": False, "role": "owner", "navigation_only": False,
        "created_at": now, "updated_at": now, "updated_at_utc": now_utc,
    }, user)
    realtime = get_realtime_server()
    if realtime is not None and realtime.available:
        await realtime.emit_projects_changed()
    return result


@router.patch("/{project_id}")
async def rename_project(
    project_id: str,
    data: UpdateProjectRequest,
    user: dict = Depends(get_current_user),
):
    """프로젝트 owner가 그룹/leaf 이름만 변경한다."""
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Project name cannot be empty")
    if len(name) > 255:
        raise HTTPException(status_code=422, detail="Project name must be 255 characters or fewer")

    now, now_utc = localnow_naive(), utcnow_naive()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            project = _require_live_project(cursor, project_id, for_update=True)
            if not can_manage_project(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Only project owner can rename it")
            cursor.execute(
                """
                SELECT id FROM projects
                WHERE parent_id <=> %s AND name = %s AND id <> %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (project["parent_id"], name, project_id),
            )
            duplicate = cursor.fetchone()
            if duplicate:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "동일한 이름의 프로젝트가 이미 존재합니다.",
                        "existingProjectId": duplicate["id"],
                    },
                )
            cursor.execute(
                "UPDATE projects SET name = %s, updated_at = %s, updated_at_utc = %s WHERE id = %s",
                (name, now, now_utc, project_id),
            )
            project.update(
                {
                    "name": name,
                    "updated_at": now,
                    "updated_at_utc": now_utc,
                    "role": "owner",
                    "navigation_only": False,
                }
            )
            result = _serialize_project(project, user)
    realtime = get_realtime_server()
    if realtime is not None and realtime.available:
        await realtime.emit_projects_changed()
    return result


@router.post("/{loser_id}/merge-into/{winner_id}")
async def merge_project_into(loser_id: str, winner_id: str, user: dict = Depends(require_admin)):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            for project_id in (loser_id, winner_id):
                _require_live_project(cursor, project_id)
                if not can_manage_project(cursor, project_id, user):
                    raise HTTPException(status_code=403, detail="Access denied")
    try:
        return await run_merge("project", loser_id, winner_id)
    except MergeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.delete("/{project_id}")
async def delete_project(project_id: str, data: DeleteProjectRequest, user: dict = Depends(get_current_user)):
    project_ids: list[str]
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            project = _require_live_project(cursor, project_id, for_update=True)
            if project["owner_id"] != user["id"]:
                raise HTTPException(status_code=403, detail="Only the project creator can delete it")
            if data.confirmationName != project["name"]:
                raise HTTPException(status_code=422, detail="Project name confirmation does not match")
            cursor.execute("SELECT id FROM projects WHERE parent_id = %s AND deleted_at IS NULL FOR UPDATE", (project_id,))
            project_ids = [project_id, *[row["id"] for row in cursor.fetchall()]]
            now_utc = utcnow_naive()
            placeholders = ", ".join(["%s"] * len(project_ids))
            cursor.execute(
                f"UPDATE memos SET deleted_at = %s, updated_at_utc = %s WHERE project_id IN ({placeholders}) AND deleted_at IS NULL",
                (now_utc, now_utc, *project_ids),
            )
            deleted_memos = cursor.rowcount
            cursor.execute(
                f"UPDATE project_members SET deleted_at = %s, updated_at_utc = %s WHERE project_id IN ({placeholders}) AND deleted_at IS NULL",
                (now_utc, now_utc, *project_ids),
            )
            deleted_members = cursor.rowcount
            cursor.execute(
                f"UPDATE projects SET deleted_at = %s, updated_at_utc = %s WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                (now_utc, now_utc, *project_ids),
            )
            result = {"message": "Project soft-deleted successfully", "deletedProjects": cursor.rowcount,
                      "deletedMemos": deleted_memos, "deletedMembers": deleted_members}
    realtime = get_realtime_server()
    if realtime is not None and realtime.available:
        await realtime.revoke_project_access(project_ids)
        await realtime.emit_projects_changed()
    return result


@router.post("/{project_id}/verify")
async def verify_project_password(project_id: str, data: VerifyPasswordRequest, user: dict = Depends(get_current_user)):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            project = _require_live_project(cursor, project_id)
            return {"verified": project["password"] == data.password}
