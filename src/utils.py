import uuid

PROJECT_OWNER_ROLE = "owner"
PROJECT_EDITOR_ROLE = "editor"
PROJECT_VIEWER_ROLE = "viewer"
PROJECT_WRITE_ROLES = {PROJECT_OWNER_ROLE, PROJECT_EDITOR_ROLE}
PROJECT_ROLES = {PROJECT_OWNER_ROLE, PROJECT_EDITOR_ROLE, PROJECT_VIEWER_ROLE}


def generate_id():
    """UUID 기반 ID 생성"""
    return str(uuid.uuid4())


def is_service_owner(user: dict) -> bool:
    return user.get("permission") == "owner"


def check_project_membership(cursor, project_id: str, user: dict):
    if is_service_owner(user):
        return True

    cursor.execute(
        "SELECT id FROM project_members WHERE project_id = %s AND user_id = %s",
        (project_id, user["id"]),
    )
    if cursor.fetchone():
        return True

    return False


def get_project_role(cursor, project_id: str, user: dict) -> str | None:
    if is_service_owner(user):
        return PROJECT_OWNER_ROLE

    cursor.execute(
        "SELECT role FROM project_members WHERE project_id = %s AND user_id = %s",
        (project_id, user["id"]),
    )
    member = cursor.fetchone()
    if not member:
        return None
    return member["role"]


def can_write_project(cursor, project_id: str, user: dict) -> bool:
    role = get_project_role(cursor, project_id, user)
    return role in PROJECT_WRITE_ROLES


def can_manage_project(cursor, project_id: str, user: dict) -> bool:
    return get_project_role(cursor, project_id, user) == PROJECT_OWNER_ROLE
