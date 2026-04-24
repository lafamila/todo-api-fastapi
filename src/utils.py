import uuid


def generate_id():
    """UUID 기반 ID 생성"""
    return str(uuid.uuid4())


def check_project_membership(cursor, project_id: str, user: dict):
    if user["is_admin"]:
        cursor.execute(
            "SELECT id FROM projects WHERE id = %s AND owner_id = %s",
            (project_id, user["id"]),
        )
        if cursor.fetchone():
            return True

    cursor.execute(
        "SELECT id FROM project_members WHERE project_id = %s AND user_id = %s",
        (project_id, user["id"]),
    )
    if cursor.fetchone():
        return True

    return False
