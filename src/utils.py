import uuid

PROJECT_OWNER_ROLE = "owner"
PROJECT_EDITOR_ROLE = "editor"
PROJECT_VIEWER_ROLE = "viewer"
PROJECT_WRITE_ROLES = {PROJECT_OWNER_ROLE, PROJECT_EDITOR_ROLE}
PROJECT_ROLES = {PROJECT_OWNER_ROLE, PROJECT_EDITOR_ROLE, PROJECT_VIEWER_ROLE}


def generate_id():
    """UUID 기반 ID 생성"""
    return str(uuid.uuid4())


def backlog_id(project_id: str) -> str:
    """모든 노드가 같은 프로젝트 백로그에 같은 UUID를 사용한다."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"todo-backlog:{project_id}"))


def is_service_owner(user: dict) -> bool:
    return user.get("permission") == "owner"


def check_project_membership(cursor, project_id: str, user: dict):
    cursor.execute(
        """
        SELECT p.id, pm.id AS member_id
        FROM projects p
        LEFT JOIN projects parent ON parent.id = p.parent_id
        LEFT JOIN project_members pm
          ON pm.project_id = p.id AND pm.user_id = %s AND pm.deleted_at IS NULL
        WHERE p.id = %s AND p.deleted_at IS NULL
          AND (p.parent_id IS NULL OR (parent.id IS NOT NULL AND parent.deleted_at IS NULL))
        """,
        (user["id"], project_id),
    )
    row = cursor.fetchone()
    if not row:
        return False
    if is_service_owner(user):
        return True
    return bool(row["member_id"])


def get_project_role(cursor, project_id: str, user: dict) -> str | None:
    cursor.execute(
        """
        SELECT p.id, pm.role
        FROM projects p
        LEFT JOIN projects parent ON parent.id = p.parent_id
        LEFT JOIN project_members pm
          ON pm.project_id = p.id AND pm.user_id = %s AND pm.deleted_at IS NULL
        WHERE p.id = %s AND p.deleted_at IS NULL
          AND (p.parent_id IS NULL OR (parent.id IS NOT NULL AND parent.deleted_at IS NULL))
        """,
        (user["id"], project_id),
    )
    member = cursor.fetchone()
    if not member:
        return None
    if is_service_owner(user):
        return PROJECT_OWNER_ROLE
    return member["role"]


def can_write_project(cursor, project_id: str, user: dict) -> bool:
    role = get_project_role(cursor, project_id, user)
    return role in PROJECT_WRITE_ROLES


def can_manage_project(cursor, project_id: str, user: dict) -> bool:
    return get_project_role(cursor, project_id, user) == PROJECT_OWNER_ROLE
