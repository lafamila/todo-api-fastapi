"""중복 병합 — 이름/제목 중복은 차단하지 않고 감지한 뒤 **명시적 확인 후** 병합한다.

자동 병합은 하지 않는다: 같은 이름이 항상 같은 것은 아니고, 되돌리기가 어렵다.

병합은 **원격(동기화 서버)에서 실행하고 로컬은 pull 로 받는다.** 양쪽에서 각자 병합하면
결과가 달라져 재충돌한다. 그래서 이 모듈의 쓰기는 `change_log` 에 정상적으로 남는다
(`sync_applying` 커넥션을 쓰지 않는다).
"""

from __future__ import annotations

try:
    from ..timeutil import kst_label, utcnow_naive
    from ..utils import generate_id
    from .sync_store import resolve_issues
except ImportError:  # pragma: no cover
    from timeutil import kst_label, utcnow_naive
    from utils import generate_id
    from services.sync_store import resolve_issues


class MergeError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def merge_note(loser_label: str, moment) -> str:
    return f"병합 · {loser_label} ({kst_label(moment)})"[:255]


def merge_memos(cursor, loser_id: str, winner_id: str) -> dict:
    """패자 메모를 생존자에 합친다.

    생존자 id 는 유지된다 (uuid 를 바꾸면 참조가 깨진다). 패자 내용은 생존자 버전으로
    편입되고, 패자 `memo_versions` 는 **재번호** 해서 이관한다 —
    `(memo_id, version)` 은 비유니크 인덱스라 제약 위반은 없지만 그대로 옮기면
    `GET /api/memos/{id}/versions/{version}` 이 모호해진다.
    """
    if loser_id == winner_id:
        raise MergeError(400, "동일한 메모를 병합할 수 없습니다.")

    loser = _load_memo(cursor, loser_id, "패자")
    winner = _load_memo(cursor, winner_id, "생존자")

    now = utcnow_naive()
    cursor.execute(
        "SELECT COALESCE(MAX(version), 0) AS max_version FROM memo_versions WHERE memo_id = %s",
        (winner_id,),
    )
    next_version = int(cursor.fetchone()["max_version"]) + 1

    moved_content_version = None
    if loser["content"]:
        cursor.execute(
            "SELECT id FROM memo_versions WHERE memo_id = %s AND content = %s LIMIT 1",
            (winner_id, loser["content"]),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                """
                INSERT INTO memo_versions
                    (id, memo_id, content, version, note, created_at, updated_at_utc)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    generate_id(),
                    winner_id,
                    loser["content"],
                    next_version,
                    merge_note(loser["title"], now),
                    now,
                    now,
                ),
            )
            moved_content_version = next_version
            next_version += 1

    # 패자 버전 이관 + 재번호
    cursor.execute(
        "SELECT id, version, note FROM memo_versions WHERE memo_id = %s ORDER BY version ASC, created_at ASC",
        (loser_id,),
    )
    moved_versions = 0
    for version_row in cursor.fetchall():
        note = version_row["note"] or merge_note(loser["title"], now)
        cursor.execute(
            "UPDATE memo_versions SET memo_id = %s, version = %s, note = %s, updated_at_utc = %s WHERE id = %s",
            (winner_id, next_version, note[:255], now, version_row["id"]),
        )
        next_version += 1
        moved_versions += 1

    cursor.execute(
        "UPDATE memos SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
        (now, now, loser_id),
    )

    resolved = resolve_issues(cursor, ref_table="memos", ref_id=loser_id)
    resolved += resolve_issues(cursor, ref_table="memos", ref_id=winner_id)

    return {
        "kind": "memo",
        "winnerId": winner_id,
        "loserId": loser_id,
        "winnerTitle": winner["title"],
        "loserTitle": loser["title"],
        "movedContentVersion": moved_content_version,
        "movedVersions": moved_versions,
        "latestWinnerVersion": next_version - 1,
        "resolvedIssues": resolved,
    }


def merge_projects(cursor, loser_id: str, winner_id: str) -> dict:
    """패자 프로젝트를 생존자에 합친다 — 메모 재부모화 → 멤버 합치기 → 패자 tombstone."""
    if loser_id == winner_id:
        raise MergeError(400, "동일한 프로젝트를 병합할 수 없습니다.")

    loser = _load_project(cursor, loser_id, "패자")
    winner = _load_project(cursor, winner_id, "생존자")
    now = utcnow_naive()

    cursor.execute(
        "SELECT id FROM memos WHERE project_id = %s",
        (loser_id,),
    )
    memo_ids = [row["id"] for row in cursor.fetchall()]
    for memo_id in memo_ids:
        cursor.execute(
            "UPDATE memos SET project_id = %s, updated_at_utc = %s WHERE id = %s",
            (winner_id, now, memo_id),
        )
    # articles 는 동기화 대상이 아니지만 project_id 참조가 어긋나면 안 된다
    cursor.execute(
        "UPDATE articles SET project_id = %s WHERE project_id = %s",
        (winner_id, loser_id),
    )

    cursor.execute(
        "SELECT * FROM project_members WHERE project_id = %s AND deleted_at IS NULL",
        (loser_id,),
    )
    loser_members = cursor.fetchall()
    merged_members = 0
    for member in loser_members:
        cursor.execute(
            "SELECT id, deleted_at FROM project_members WHERE project_id = %s AND user_id = %s",
            (winner_id, member["user_id"]),
        )
        target = cursor.fetchone()
        if target is None:
            cursor.execute(
                """
                INSERT INTO project_members
                    (id, project_id, user_id, username, display_name, email, role, invited_at, updated_at_utc)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    generate_id(),
                    winner_id,
                    member["user_id"],
                    member["username"],
                    member["display_name"],
                    member["email"],
                    member["role"],
                    member["invited_at"],
                    now,
                ),
            )
            merged_members += 1
        elif target["deleted_at"] is not None:
            # 생존자 쪽에 tombstone 이 있으면 되살린다 (UNIQUE(project_id,user_id) 때문)
            cursor.execute(
                """
                UPDATE project_members
                SET deleted_at = NULL, role = %s, username = %s, display_name = %s,
                    email = %s, updated_at_utc = %s
                WHERE id = %s
                """,
                (
                    member["role"],
                    member["username"],
                    member["display_name"],
                    member["email"],
                    now,
                    target["id"],
                ),
            )
            merged_members += 1

        cursor.execute(
            "UPDATE project_members SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
            (now, now, member["id"]),
        )

    cursor.execute(
        "UPDATE projects SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
        (now, now, loser_id),
    )

    resolved = resolve_issues(cursor, ref_table="projects", ref_id=loser_id)
    resolved += resolve_issues(cursor, ref_table="projects", ref_id=winner_id)

    return {
        "kind": "project",
        "winnerId": winner_id,
        "loserId": loser_id,
        "winnerName": winner["name"],
        "loserName": loser["name"],
        "movedMemos": len(memo_ids),
        "mergedMembers": merged_members,
        "resolvedIssues": resolved,
    }


async def run_merge(kind: str, loser_id: str, winner_id: str) -> dict:
    """병합을 **어디서 실행할지** 결정하고 실행한다.

    | 상태 | 동작 |
    |---|---|
    | 클라이언트 · 온라인 | 원격에 위임하고 즉시 pull 을 요청한다 |
    | 클라이언트 · 오프라인 | **409 로 잠근다** — 양쪽에서 각자 병합하면 결과가 달라져 재충돌한다 |
    | 서버 또는 개발 스택 | 로컬 실행 |
    """
    import asyncio

    try:
        from ..config import runs_sync_daemon
        from ..connectors import get_db_connection
        from .sync_peer import SyncPeerError, SyncPeerUnreachable, get_sync_peer
        from .sync_runtime import get_sync_runtime
    except ImportError:  # pragma: no cover
        from config import runs_sync_daemon
        from connectors import get_db_connection
        from services.sync_peer import SyncPeerError, SyncPeerUnreachable, get_sync_peer
        from services.sync_runtime import get_sync_runtime

    executor = merge_memos if kind == "memo" else merge_projects

    if runs_sync_daemon():
        runtime = get_sync_runtime()
        peer = get_sync_peer()
        if not runtime.online or not peer.configured:
            raise MergeError(
                409,
                "오프라인 상태에서는 병합을 실행할 수 없습니다. "
                "병합은 원격에서 실행하고 로컬은 pull 로 받습니다 — 연결된 뒤 다시 시도하세요.",
            )
        remote_call = peer.merge_memo if kind == "memo" else peer.merge_project
        try:
            result = await asyncio.to_thread(remote_call, loser_id, winner_id)
        except SyncPeerUnreachable as exc:
            runtime.mark_offline(str(exc))
            raise MergeError(409, f"원격에 닿지 못해 병합을 중단했습니다: {exc}") from exc
        except SyncPeerError as exc:
            detail = exc.detail
            message = detail.get("detail") if isinstance(detail, dict) else detail
            raise MergeError(exc.status or 502, str(message)) from exc
        runtime.request_cycle()
        return {**result, "executedOn": "remote", "pullRequested": True}

    def _execute() -> dict:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                return executor(cursor, loser_id, winner_id)

    result = await asyncio.to_thread(_execute)
    return {**result, "executedOn": "local", "pullRequested": False}


def _load_memo(cursor, memo_id: str, label: str) -> dict:
    cursor.execute(
        "SELECT id, project_id, title, content FROM memos WHERE id = %s AND deleted_at IS NULL",
        (memo_id,),
    )
    memo = cursor.fetchone()
    if memo is None:
        raise MergeError(404, f"{label} 메모를 찾을 수 없습니다: {memo_id}")
    return memo


def _load_project(cursor, project_id: str, label: str) -> dict:
    cursor.execute(
        "SELECT id, name FROM projects WHERE id = %s AND deleted_at IS NULL",
        (project_id,),
    )
    project = cursor.fetchone()
    if project is None:
        raise MergeError(404, f"{label} 프로젝트를 찾을 수 없습니다: {project_id}")
    return project
