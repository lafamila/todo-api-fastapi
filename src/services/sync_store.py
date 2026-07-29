"""동기화 상태 저장소 — `change_log` / `sync_state` / `sync_issues` / `local_identity`.

여기에는 **DB 접근과 직렬화만** 둔다. 충돌/중복 판정 같은 정책은 `sync_apply.py` 에 있다.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from typing import Any

try:
    from ..sync_schema import (
        SYNC_TABLES,
        UTC_COLUMNS,
        WALL_CLOCK_COLUMNS,
        table_columns,
    )
    from ..timeutil import iso_utc, parse_iso_utc, utcnow_naive
    from ..utils import generate_id
except ImportError:  # pragma: no cover
    from sync_schema import SYNC_TABLES, UTC_COLUMNS, WALL_CLOCK_COLUMNS, table_columns
    from timeutil import iso_utc, parse_iso_utc, utcnow_naive
    from utils import generate_id


# ---------------------------------------------------------------------------
# change_log
# ---------------------------------------------------------------------------


def max_change_seq(cursor) -> int:
    cursor.execute("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM change_log")
    return int(cursor.fetchone()["max_seq"])


def pending_change_count(cursor, since_seq: int) -> int:
    cursor.execute(
        "SELECT COUNT(*) AS pending FROM change_log WHERE seq > %s", (since_seq,)
    )
    return int(cursor.fetchone()["pending"])


def visible_project_ids(cursor, account_id: str) -> set[str]:
    """그 계정이 볼 수 있는 프로젝트 id (소유 또는 살아있는 멤버십 경유).

    단일 사용자에서는 전체와 같지만, 다중 사용자에서는 변경 피드가 남의 데이터를
    흘리지 않게 하는 유일한 방어선이다.
    """
    cursor.execute(
        """
        SELECT DISTINCT p.id
        FROM projects p
        LEFT JOIN project_members pm
               ON pm.project_id = p.id AND pm.user_id = %s AND pm.deleted_at IS NULL
        WHERE p.owner_id = %s OR pm.id IS NOT NULL
        """,
        (account_id, account_id),
    )
    return {row["id"] for row in cursor.fetchall()}


def _row_project_id(cursor, table: str, row: dict) -> str | None:
    """계정 스코프 판정에 쓰는 소속 프로젝트 id."""
    if table == "projects":
        return row.get("id")
    if table in ("memos", "project_members"):
        return row.get("project_id")
    if table == "memo_versions":
        cursor.execute("SELECT project_id FROM memos WHERE id = %s", (row.get("memo_id"),))
        parent = cursor.fetchone()
        return parent["project_id"] if parent else None
    return None


def serialize_row(table: str, row: dict, columns: tuple[str, ...] | None = None) -> dict:
    """DB 행 → 와이어 dict (화이트리스트 컬럼만, datetime 은 규칙대로 문자열화)."""
    allowed = columns if columns is not None else table_columns(table)
    payload: dict[str, Any] = {}
    for column in allowed:
        if column not in row:
            continue
        value = row[column]
        if isinstance(value, datetime):
            payload[column] = (
                iso_utc(value)
                if column in UTC_COLUMNS
                else value.isoformat(timespec="milliseconds")
            )
        elif column == "is_secret":
            payload[column] = bool(value)
        else:
            payload[column] = value
    return payload


def deserialize_row(table: str, payload: dict, columns: tuple[str, ...]) -> dict:
    """와이어 dict → DB 저장용 값. 모르는 컬럼은 조용히 버린다."""
    row: dict[str, Any] = {}
    for column in columns:
        if column not in payload:
            continue
        value = payload[column]
        if column in UTC_COLUMNS:
            row[column] = parse_iso_utc(value) if value else None
        elif column in WALL_CLOCK_COLUMNS:
            row[column] = _parse_naive(value)
        elif column == "is_secret":
            row[column] = 1 if value else 0
        else:
            row[column] = value
    return row


def _parse_naive(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1]
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=None)


def read_changes(
    cursor,
    since_seq: int,
    limit: int,
    account_id: str,
    columns_by_table: dict[str, tuple[str, ...]] | None = None,
) -> tuple[list[dict], int]:
    """`since_seq` 이후 변경을 계정 스코프로 걸러 반환한다.

    반환하는 `next_seq` 는 **훑어본 최대 seq** 다 (스코프에서 걸러진 행이 있어도
    커서가 진행해야 같은 구간을 무한히 다시 읽지 않는다).
    """
    cursor.execute(
        """
        SELECT seq, table_name, row_id, op, changed_at_utc
        FROM change_log
        WHERE seq > %s
        ORDER BY seq ASC
        LIMIT %s
        """,
        (since_seq, limit),
    )
    entries = cursor.fetchall()
    if not entries:
        return [], since_seq

    next_seq = max(int(entry["seq"]) for entry in entries)

    # 같은 행의 중복 변경은 마지막 것만 보낸다 (현재값을 어차피 다시 읽는다)
    latest: dict[tuple[str, str], dict] = {}
    for entry in entries:
        table = entry["table_name"]
        if table not in SYNC_TABLES:
            continue
        latest[(table, entry["row_id"])] = entry

    visible = visible_project_ids(cursor, account_id)
    changes: list[dict] = []
    for (table, row_id), entry in latest.items():
        columns = (columns_by_table or {}).get(table) or table_columns(table)
        cursor.execute(
            f"SELECT {', '.join(columns)} FROM `{table}` WHERE {SYNC_TABLES[table]['pk']} = %s",
            (row_id,),
        )
        row = cursor.fetchone()
        if row is None:
            # 삭제 전 scope를 change_log에 고정하지 않은 하드 삭제는 현재 계정 소속을
            # 증명할 수 없다. 업무 테이블은 tombstone을 쓰므로 이 예외 경로는 누출보다
            # 보수적으로 건너뛴다.
            continue

        project_id = _row_project_id(cursor, table, row)
        if project_id is not None and project_id not in visible:
            continue

        changes.append(
            {
                "seq": int(entry["seq"]),
                "table": table,
                "rowId": row_id,
                "op": entry["op"],
                "row": serialize_row(table, row, columns),
            }
        )

    changes.sort(key=lambda change: change["seq"])
    return changes, next_seq


def collect_local_changes(
    cursor,
    since_seq: int,
    limit: int,
    columns_by_table: dict[str, tuple[str, ...]] | None = None,
    peer: str | None = None,
) -> tuple[list[dict], int]:
    """push 용 — 계정 스코프 필터 없이 이 노드의 변경을 모은다 (단일 사용자 노드)."""
    cursor.execute(
        """
        SELECT seq, table_name, row_id, op
        FROM change_log
        WHERE seq > %s
        ORDER BY seq ASC
        LIMIT %s
        """,
        (since_seq, limit),
    )
    entries = cursor.fetchall()
    if not entries:
        return [], since_seq

    next_seq = max(int(entry["seq"]) for entry in entries)
    latest: dict[tuple[str, str], dict] = {}
    for entry in entries:
        if entry["table_name"] not in SYNC_TABLES:
            continue
        latest[(entry["table_name"], entry["row_id"])] = entry

    changes: list[dict] = []
    for (table, row_id), entry in latest.items():
        columns = (columns_by_table or {}).get(table) or table_columns(table)
        cursor.execute(
            f"SELECT {', '.join(columns)} FROM `{table}` WHERE {SYNC_TABLES[table]['pk']} = %s",
            (row_id,),
        )
        row = cursor.fetchone()
        change = {
            "seq": int(entry["seq"]),
            "table": table,
            "rowId": row_id,
            "op": "delete" if row is None else entry["op"],
            "row": None if row is None else serialize_row(table, row, columns),
        }
        if peer:
            row_state = get_row_sync_state(cursor, peer, table, row_id)
            change["baseUpdatedAtUtc"] = (
                iso_utc(row_state["last_seen_updated_at_utc"])
                if row_state and row_state.get("last_seen_updated_at_utc")
                else None
            )
            change["basePeerSeq"] = (
                int(row_state["last_seen_peer_seq"])
                if row_state and row_state.get("last_seen_peer_seq") is not None
                else None
            )
        changes.append(change)

    changes.sort(key=lambda change: change["seq"])
    return changes, next_seq


# ---------------------------------------------------------------------------
# 행별 동기화 기준 / 내구성 재시도 큐
# ---------------------------------------------------------------------------


def get_row_sync_state(cursor, peer: str, table: str, row_id: str) -> dict | None:
    cursor.execute(
        """
        SELECT last_seen_updated_at_utc, last_seen_peer_seq
        FROM sync_row_state
        WHERE peer = %s AND table_name = %s AND row_id = %s
        """,
        (peer, table, row_id),
    )
    return cursor.fetchone()


def get_row_sync_clock(cursor, peer: str, table: str, row_id: str) -> str | None:
    """상대와 마지막으로 합의된 행 시각.

    이 값은 프로세스 재시작 뒤에도 남아야 양쪽이 모두 수정된 경우와 단순한
    단방향 복제를 구분할 수 있다.
    """
    row = get_row_sync_state(cursor, peer, table, row_id)
    if row is None:
        return None
    return iso_utc(row["last_seen_updated_at_utc"])


def set_row_sync_clock(
    cursor,
    peer: str,
    table: str,
    row_id: str,
    updated_at_utc: datetime | str | None,
    peer_seq: int | None = None,
) -> None:
    """상대와 합의된 행 시각을 기록한다."""
    parsed = (
        parse_iso_utc(updated_at_utc)
        if isinstance(updated_at_utc, str)
        else updated_at_utc
    )
    if parsed is None:
        return
    cursor.execute(
        """
        INSERT INTO sync_row_state
            (peer, table_name, row_id, last_seen_updated_at_utc, last_seen_peer_seq)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            last_seen_updated_at_utc = VALUES(last_seen_updated_at_utc),
            last_seen_peer_seq = GREATEST(last_seen_peer_seq, VALUES(last_seen_peer_seq))
        """,
        (peer, table, row_id, parsed, int(peer_seq or 0)),
    )


def enqueue_sync_retry(
    cursor,
    peer: str,
    direction: str,
    change: dict,
    reason: str,
) -> None:
    """실패/지연된 한 행을 cursor와 같은 트랜잭션에 내구성 있게 보관한다."""
    if direction not in ("push", "pull"):
        raise ValueError(f"unsupported retry direction: {direction}")
    seq = change.get("seq")
    if seq is None:
        raise ValueError("retryable sync change requires seq")
    payload = json.dumps(
        change, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    cursor.execute(
        """
        SELECT payload_hash
        FROM sync_retry_queue
        WHERE peer = %s AND direction = %s AND seq = %s
        """,
        (peer, direction, int(seq)),
    )
    existing = cursor.fetchone()
    if existing is not None and existing["payload_hash"] != payload_hash:
        raise ValueError(
            f"sync retry {peer}/{direction}/{seq} has a different immutable payload"
        )
    now = utcnow_naive()
    cursor.execute(
        """
        INSERT INTO sync_retry_queue
            (peer, direction, seq, table_name, row_id, payload, payload_hash, reason,
             attempts, first_seen_at, last_attempt_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s)
        ON DUPLICATE KEY UPDATE
            reason = VALUES(reason),
            last_attempt_at = VALUES(last_attempt_at)
        """,
        (
            peer,
            direction,
            int(seq),
            change.get("table"),
            change.get("rowId"),
            payload,
            payload_hash,
            reason[:1000],
            now,
            now,
        ),
    )


def list_sync_retries(
    cursor, peer: str, direction: str, limit: int
) -> list[dict]:
    cursor.execute(
        """
        SELECT peer, direction, seq, table_name, row_id, payload, payload_hash, reason,
               attempts, first_seen_at, last_attempt_at
        FROM sync_retry_queue
        WHERE peer = %s AND direction = %s
        ORDER BY seq ASC
        LIMIT %s
        """,
        (peer, direction, limit),
    )
    rows = cursor.fetchall()
    for row in rows:
        payload = row.get("payload")
        if isinstance(payload, str):
            row["change"] = json.loads(payload)
        else:
            row["change"] = payload
    return rows


def mark_sync_retry_failed(
    cursor, peer: str, direction: str, seq: int, reason: str
) -> None:
    cursor.execute(
        """
        UPDATE sync_retry_queue
        SET attempts = attempts + 1, reason = %s, last_attempt_at = %s
        WHERE peer = %s AND direction = %s AND seq = %s
        """,
        (reason[:1000], utcnow_naive(), peer, direction, int(seq)),
    )


def mark_sync_retry_dead(
    cursor, peer: str, direction: str, seq: int, reason: str
) -> None:
    dead_direction = f"{direction}dead"
    cursor.execute(
        """
        UPDATE sync_retry_queue
        SET direction = %s, attempts = attempts + 1, reason = %s,
            last_attempt_at = %s
        WHERE peer = %s AND direction = %s AND seq = %s
        """,
        (
            dead_direction,
            reason[:1000],
            utcnow_naive(),
            peer,
            direction,
            int(seq),
        ),
    )


def delete_sync_retry(cursor, peer: str, direction: str, seq: int) -> None:
    cursor.execute(
        """
        DELETE FROM sync_retry_queue
        WHERE peer = %s AND direction = %s AND seq = %s
        """,
        (peer, direction, int(seq)),
    )


def pending_sync_retry_count(cursor, peer: str) -> int:
    cursor.execute(
        """
        SELECT COUNT(*) AS pending
        FROM sync_retry_queue
        WHERE peer = %s AND direction IN ('push', 'pull')
        """,
        (peer,),
    )
    return int(cursor.fetchone()["pending"])


# ---------------------------------------------------------------------------
# push terminal receipts
# ---------------------------------------------------------------------------


def sync_payload_hash(change: dict) -> str:
    canonical = json.dumps(
        change, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def get_sync_receipt(
    cursor, account_id: str, client_id: str, source_seq: int
) -> dict | None:
    cursor.execute(
        """
        SELECT payload_hash, outcome
        FROM sync_receipts
        WHERE account_id = %s AND client_id = %s AND source_seq = %s
        """,
        (account_id, client_id, int(source_seq)),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    outcome = row.get("outcome")
    if isinstance(outcome, str):
        outcome = json.loads(outcome)
    return {"payloadHash": row["payload_hash"], "outcome": outcome}


def store_sync_receipt(
    cursor,
    account_id: str,
    client_id: str,
    source_seq: int,
    payload_hash: str,
    outcome: dict,
) -> None:
    cursor.execute(
        """
        INSERT INTO sync_receipts
            (account_id, client_id, source_seq, payload_hash, outcome, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            account_id,
            client_id,
            int(source_seq),
            payload_hash,
            json.dumps(outcome, ensure_ascii=False),
            utcnow_naive(),
        ),
    )


# ---------------------------------------------------------------------------
# sync_state
# ---------------------------------------------------------------------------


def get_sync_state(cursor, peer: str) -> dict:
    cursor.execute("SELECT * FROM sync_state WHERE peer = %s", (peer,))
    state = cursor.fetchone()
    if state is None:
        return {
            "peer": peer,
            "last_pushed_seq": 0,
            "last_pulled_seq": 0,
            "last_ok_at": None,
            "last_error": None,
            "paused": 0,
            "client_epoch": None,
        }
    return state


def ensure_sync_state(cursor, peer: str) -> dict:
    state = get_sync_state(cursor, peer)
    client_epoch = state.get("client_epoch") or generate_id()
    cursor.execute(
        """
        INSERT INTO sync_state
            (peer, last_pushed_seq, last_pulled_seq, paused, client_epoch, updated_at_utc)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            client_epoch = COALESCE(client_epoch, VALUES(client_epoch))
        """,
        (
            peer,
            state["last_pushed_seq"],
            state["last_pulled_seq"],
            state["paused"],
            client_epoch,
            utcnow_naive(),
        ),
    )
    return {**state, "client_epoch": client_epoch}


def get_client_epoch(cursor, peer: str) -> str:
    return str(ensure_sync_state(cursor, peer)["client_epoch"])


def update_sync_state(cursor, peer: str, **fields) -> None:
    """`sync_state` 부분 갱신 (없으면 만든다)."""
    allowed = ("last_pushed_seq", "last_pulled_seq", "last_ok_at", "last_error", "paused")
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    ensure_sync_state(cursor, peer)
    assignments = ", ".join(f"{column} = %s" for column in updates)
    cursor.execute(
        f"UPDATE sync_state SET {assignments}, updated_at_utc = %s WHERE peer = %s",
        (*updates.values(), utcnow_naive(), peer),
    )


def set_paused(cursor, peer: str, paused: bool) -> None:
    update_sync_state(cursor, peer, paused=1 if paused else 0)


# ---------------------------------------------------------------------------
# sync_issues
# ---------------------------------------------------------------------------


def record_issue(
    cursor,
    kind: str,
    ref_table: str | None = None,
    ref_id: str | None = None,
    peer_ref_id: str | None = None,
    detail: dict | None = None,
    dedupe: bool = True,
) -> str:
    """미해결 이슈 기록. `dedupe` 면 같은 (kind, ref) 의 미해결 행을 갱신한다."""
    now = utcnow_naive()
    if dedupe:
        cursor.execute(
            """
            SELECT id FROM sync_issues
            WHERE kind = %s
              AND resolved_at IS NULL
              AND (ref_table <=> %s) AND (ref_id <=> %s) AND (peer_ref_id <=> %s)
            LIMIT 1
            """,
            (kind, ref_table, ref_id, peer_ref_id),
        )
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                "UPDATE sync_issues SET detail = %s, detected_at = %s WHERE id = %s",
                (json.dumps(detail or {}, ensure_ascii=False), now, existing["id"]),
            )
            return existing["id"]

    issue_id = generate_id()
    cursor.execute(
        """
        INSERT INTO sync_issues
            (id, kind, ref_table, ref_id, peer_ref_id, detail, detected_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            issue_id,
            kind,
            ref_table,
            ref_id,
            peer_ref_id,
            json.dumps(detail or {}, ensure_ascii=False),
            now,
        ),
    )
    return issue_id


def serialize_issue(row: dict) -> dict:
    detail = row.get("detail")
    if isinstance(detail, (str, bytes)):
        try:
            detail = json.loads(detail)
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = {"raw": str(detail)}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "refTable": row.get("ref_table"),
        "refId": row.get("ref_id"),
        "peerRefId": row.get("peer_ref_id"),
        "detail": detail or {},
        "detectedAt": iso_utc(row.get("detected_at")),
        "resolvedAt": iso_utc(row.get("resolved_at")),
        "resolved": row.get("resolved_at") is not None,
    }


def list_issues(
    cursor,
    kind: str | None = None,
    ref_table: str | None = None,
    ref_id: str | None = None,
    include_resolved: bool = False,
    limit: int = 200,
) -> list[dict]:
    conditions = []
    params: list[Any] = []
    if not include_resolved:
        conditions.append("resolved_at IS NULL")
    if kind:
        conditions.append("kind = %s")
        params.append(kind)
    if ref_table:
        conditions.append("ref_table = %s")
        params.append(ref_table)
    if ref_id:
        conditions.append("(ref_id = %s OR peer_ref_id = %s)")
        params.extend([ref_id, ref_id])
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    cursor.execute(
        f"""
        SELECT id, kind, ref_table, ref_id, peer_ref_id, detail, detected_at, resolved_at
        FROM sync_issues
        {where}
        ORDER BY (resolved_at IS NOT NULL) ASC, detected_at DESC
        LIMIT %s
        """,
        params,
    )
    return [serialize_issue(row) for row in cursor.fetchall()]


def unresolved_issue_counts(cursor) -> dict[str, int]:
    cursor.execute(
        """
        SELECT kind, COUNT(*) AS total
        FROM sync_issues
        WHERE resolved_at IS NULL
        GROUP BY kind
        """
    )
    return {row["kind"]: int(row["total"]) for row in cursor.fetchall()}


def resolve_issues(
    cursor,
    issue_ids: list[str] | None = None,
    kind: str | None = None,
    ref_table: str | None = None,
    ref_id: str | None = None,
) -> int:
    """이슈 해소 표시. 흐린 제목 표시는 미해결 행이 근거이므로 이 호출로 사라진다."""
    conditions = ["resolved_at IS NULL"]
    params: list[Any] = []
    if issue_ids:
        conditions.append(f"id IN ({', '.join(['%s'] * len(issue_ids))})")
        params.extend(issue_ids)
    if kind:
        conditions.append("kind = %s")
        params.append(kind)
    if ref_table:
        conditions.append("ref_table = %s")
        params.append(ref_table)
    if ref_id:
        conditions.append("(ref_id = %s OR peer_ref_id = %s)")
        params.extend([ref_id, ref_id])
    if len(conditions) == 1:
        return 0
    cursor.execute(
        f"UPDATE sync_issues SET resolved_at = %s WHERE {' AND '.join(conditions)}",
        (utcnow_naive(), *params),
    )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# local_identity — 오프라인 세션용 신원 캐시
# ---------------------------------------------------------------------------


def upsert_local_identity(cursor, user: dict, issuer: str | None) -> None:
    now = utcnow_naive()
    cursor.execute(
        """
        INSERT INTO local_identity
            (account_id, login_id, display_name, email, permission, issuer,
             verified_at_utc, updated_at_utc)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            login_id = VALUES(login_id),
            display_name = VALUES(display_name),
            email = VALUES(email),
            permission = VALUES(permission),
            issuer = VALUES(issuer),
            verified_at_utc = VALUES(verified_at_utc),
            updated_at_utc = VALUES(updated_at_utc)
        """,
        (
            user["id"],
            user.get("login_id") or user.get("username"),
            user.get("display_name") or user.get("name"),
            user.get("email"),
            user.get("permission", "visitor"),
            issuer,
            now,
            now,
        ),
    )


def get_local_identity(
    cursor, account_id: str | None = None, issuer: str | None = None
) -> dict | None:
    """정확한 계정/issuer 신원을 조회한다.

    account_id 없는 호출은 진단 CLI 하위 호환용으로만 남긴다. 세션 경로는 반드시
    account_id와 issuer를 함께 넘겨 "가장 최근 로그인" 계정으로 바뀌지 않게 한다.
    """
    if account_id and issuer:
        cursor.execute(
            "SELECT * FROM local_identity WHERE account_id = %s AND issuer = %s",
            (account_id, issuer),
        )
    elif account_id:
        cursor.execute("SELECT * FROM local_identity WHERE account_id = %s", (account_id,))
    else:
        cursor.execute("SELECT * FROM local_identity ORDER BY verified_at_utc DESC LIMIT 1")
    return cursor.fetchone()


def identity_to_user(identity: dict) -> dict:
    """`local_identity` 행 → `get_current_user` 가 기대하는 사용자 dict."""
    try:
        from ..token_verifier import ADMIN_PERMISSIONS, slugify
    except ImportError:  # pragma: no cover
        from token_verifier import ADMIN_PERMISSIONS, slugify

    account_id = identity["account_id"]
    login_id = identity.get("login_id") or account_id
    display_name = identity.get("display_name") or login_id
    permission = identity.get("permission") or "visitor"
    return {
        "id": account_id,
        "account_id": account_id,
        "login_id": login_id,
        "username": login_id,
        "name": display_name,
        "display_name": display_name,
        "email": identity.get("email"),
        "permission": permission,
        "slug": slugify(str(login_id)),
        "is_admin": permission in ADMIN_PERMISSIONS,
        "is_super_admin": permission == "superadmin",
        "is_active": permission != "visitor",
        "offline": True,
    }
