import uuid

from fastapi import HTTPException

PROJECT_OWNER_ROLE = "owner"
PROJECT_EDITOR_ROLE = "editor"
PROJECT_VIEWER_ROLE = "viewer"
PROJECT_WRITE_ROLES = {PROJECT_OWNER_ROLE, PROJECT_EDITOR_ROLE}
PROJECT_ROLES = {PROJECT_OWNER_ROLE, PROJECT_EDITOR_ROLE, PROJECT_VIEWER_ROLE}


def generate_id():
    """UUID 기반 ID 생성"""
    return str(uuid.uuid4())


def backlog_id(project_id: str, user_id: str) -> str:
    """모든 노드가 같은 프로젝트+계정 백로그에 같은 UUID를 사용한다."""
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"todo-backlog:{project_id}:{user_id}")
    )


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


def check_explicit_project_membership(cursor, project_id: str, user_id: str) -> bool:
    """service-owner 우회 없이 실제 live membership만 확인한다."""
    cursor.execute(
        """
        SELECT pm.id
        FROM projects p
        LEFT JOIN projects parent ON parent.id = p.parent_id
        JOIN project_members pm
          ON pm.project_id = p.id AND pm.user_id = %s AND pm.deleted_at IS NULL
        WHERE p.id = %s AND p.deleted_at IS NULL AND p.is_group = 0
          AND (p.parent_id IS NULL OR
               (parent.id IS NOT NULL AND parent.deleted_at IS NULL))
        LIMIT 1
        """,
        (user_id, project_id),
    )
    return cursor.fetchone() is not None


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


def get_authorized_memo(
    cursor,
    memo_id: str,
    user: dict,
    *,
    require_write: bool = False,
    include_deleted: bool = False,
    _for_update: bool = False,
) -> dict | None:
    """메모 단위 권한을 판정하고 허용된 joined row를 반환한다.

    일반 메모는 기존 프로젝트 역할을 따른다. 개인 backlog는 live membership과
    정확한 ``created_by``가 모두 필요하며 service/project owner 우회를 허용하지 않는다.
    viewer도 자기 backlog에는 write 권한이 있다.
    """
    deleted_clause = "" if include_deleted else "AND m.deleted_at IS NULL"
    lock_clause = "FOR UPDATE" if _for_update else ""
    cursor.execute(
        f"""
        SELECT m.*, p.owner_id AS project_owner_id, p.parent_id, p.is_group,
               p.deleted_at AS project_deleted_at,
               parent.id AS parent_exists, parent.deleted_at AS parent_deleted_at,
               pm.id AS member_id, pm.role AS member_role
        FROM memos m
        JOIN projects p ON p.id = m.project_id
        LEFT JOIN projects parent ON parent.id = p.parent_id
        LEFT JOIN project_members pm
          ON pm.project_id = p.id AND pm.user_id = %s AND pm.deleted_at IS NULL
        WHERE m.id = %s {deleted_clause}
        LIMIT 1 {lock_clause}
        """,
        (user["id"], memo_id),
    )
    memo = cursor.fetchone()
    if (
        not memo
        or memo.get("project_deleted_at") is not None
        or bool(memo.get("is_group"))
        or (
            memo.get("parent_id") is not None
            and (
                memo.get("parent_exists") is None
                or memo.get("parent_deleted_at") is not None
            )
        )
    ):
        return None

    kind = memo.get("kind") or "memo"
    if kind not in {"memo", "backlog"}:
        return None
    if kind == "backlog":
        if memo.get("created_by") != user["id"] or memo.get("member_id") is None:
            return None
        return memo

    role = (
        PROJECT_OWNER_ROLE
        if is_service_owner(user)
        else memo.get("member_role")
    )
    if role is None:
        return None
    if require_write and role not in PROJECT_WRITE_ROLES:
        return None
    return memo


def can_read_memo(
    cursor, memo_id: str, user: dict, *, include_deleted: bool = False
) -> bool:
    return (
        get_authorized_memo(
            cursor, memo_id, user, include_deleted=include_deleted
        )
        is not None
    )


def can_write_memo(
    cursor, memo_id: str, user: dict, *, include_deleted: bool = False
) -> bool:
    return (
        get_authorized_memo(
            cursor,
            memo_id,
            user,
            require_write=True,
            include_deleted=include_deleted,
        )
        is not None
    )


def require_memo_access(
    cursor,
    memo_id: str,
    user: dict,
    *,
    write: bool = False,
    include_deleted: bool = False,
) -> dict:
    """메모가 없거나 숨겨진 hierarchy면 404, 권한이 없으면 403."""
    memo = get_authorized_memo(
        cursor,
        memo_id,
        user,
        require_write=write,
        include_deleted=include_deleted,
    )
    if memo is not None:
        return memo

    deleted_clause = "" if include_deleted else "AND m.deleted_at IS NULL"
    cursor.execute(
        f"""
        SELECT m.id, p.id AS project_exists, p.deleted_at AS project_deleted_at,
               p.is_group, p.parent_id, parent.id AS parent_exists,
               parent.deleted_at AS parent_deleted_at
        FROM memos m
        LEFT JOIN projects p ON p.id = m.project_id
        LEFT JOIN projects parent ON parent.id = p.parent_id
        WHERE m.id = %s {deleted_clause}
        LIMIT 1
        """,
        (memo_id,),
    )
    hidden = cursor.fetchone()
    if (
        not hidden
        or hidden.get("project_exists") is None
        or hidden.get("project_deleted_at") is not None
        or bool(hidden.get("is_group"))
        or (
            hidden.get("parent_id") is not None
            and (
                hidden.get("parent_exists") is None
                or hidden.get("parent_deleted_at") is not None
            )
        )
    ):
        raise HTTPException(status_code=404, detail="Memo not found")
    raise HTTPException(status_code=403, detail="Access denied")


def require_locked_memo_access(
    cursor,
    memo_id: str,
    user: dict,
    *,
    write: bool = False,
) -> dict:
    """첫 DB read에서 memo/project/parent/membership을 current-locking 조회한다.

    첨부 metadata 예약처럼 권한 확인과 쓰기 사이에 membership 회수가 끼어들면
    안 되는 짧은 transaction에서 사용한다. plain SELECT로 snapshot을 먼저 만들지
    않으며 object storage 네트워크 호출 동안에는 이 잠금을 유지하면 안 된다.
    """
    memo = get_authorized_memo(
        cursor,
        memo_id,
        user,
        require_write=write,
        _for_update=True,
    )
    if memo is not None:
        return memo
    # The first DB read above was a locking/current read, so this diagnostic
    # classification cannot reuse a stale REPEATABLE READ snapshot.
    return require_memo_access(cursor, memo_id, user, write=write)
