"""동기화 적용 정책 — LWW 충돌, 삭제 vs 편집, 중복 감지, 의존성 순서.

핵심 규칙 (root plan 확정):
    - `updated_at_utc` 가 **늦은 쪽 승**. **동시각이면 서버(원격) 값 승**.
    - 진 쪽 메모 본문은 `memo_versions` 에 `note` 와 함께 **보존**한다 — 유실이 없다.
    - 삭제는 전부 soft delete 이므로 삭제 vs 편집도 같은 LWW 로 결정된다.
    - 적용 순서는 `projects → memos → memo_versions → project_members`.
      FK 부모가 아직 없는 행은 **그 행만** 다음 라운드로 미룬다.
    - 제목/이름 중복은 **차단하지 않고** `sync_issues` 로 기록한다. 막으면 오프라인
      생성분이 409 로 동기화를 영구 정지시킨다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

try:
    from ..connectors import change_log_enabled
    from ..sync_schema import (
        CONTENT_COLUMN_BY_TABLE,
        SYNC_TABLE_ORDER,
        SYNC_TABLES,
        normalize_name,
        table_columns,
    )
    from ..timeutil import kst_label, utcnow_naive
    from ..utils import generate_id
    from .sync_store import (
        deserialize_row,
        get_sync_receipt,
        record_issue,
        store_sync_receipt,
        sync_payload_hash,
    )
except ImportError:  # pragma: no cover
    from connectors import change_log_enabled
    from sync_schema import (
        CONTENT_COLUMN_BY_TABLE,
        SYNC_TABLE_ORDER,
        SYNC_TABLES,
        normalize_name,
        table_columns,
    )
    from timeutil import kst_label, utcnow_naive
    from utils import generate_id
    from services.sync_store import (
        deserialize_row,
        get_sync_receipt,
        record_issue,
        store_sync_receipt,
        sync_payload_hash,
    )


logger = logging.getLogger(__name__)

SIDE_CLIENT = "client"
SIDE_SERVER = "server"

# 절대 라벨 — 동기화 **역할** 기준이라 양쪽 노드가 같은 문자열을 본다.
# 노트북 = 동기화 클라이언트 = `로컬`, NAS = 동기화 서버 = `원격`.
_SIDE_LABEL = {SIDE_CLIENT: "로컬", SIDE_SERVER: "원격"}


def _other_side(side: str) -> str:
    return SIDE_SERVER if side == SIDE_CLIENT else SIDE_CLIENT


def conflict_note(side: str, moment: datetime | None) -> str:
    """`충돌 · 로컬 (07-29 14:02)` 형태의 보존 버전 표시."""
    return f"충돌 · {_SIDE_LABEL.get(side, side)} ({kst_label(moment)})"


@dataclass
class ApplyOutcome:
    applied: int = 0
    skipped: int = 0
    deferred: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    issue_ids: list[str] = field(default_factory=list)
    # 실제로 반영된 행 — pull 후 로컬 재발행 대상을 고르는 데 쓴다
    applied_refs: list[dict] = field(default_factory=list)
    # 입력 seq별 결과. 호출자는 이 목록으로 cursor를 안전하게 ack하거나 재시도 큐에 넣는다.
    results: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "skipped": self.skipped,
            "deferred": self.deferred,
            "rejected": self.rejected,
            "conflicts": self.conflicts,
            "duplicates": self.duplicates,
            "issueIds": self.issue_ids,
            "results": self.results,
        }


def apply_changes(
    cursor,
    changes: list[dict],
    incoming_side: str,
    columns_by_table: dict[str, tuple[str, ...]] | None = None,
    record_issues: bool = True,
    receipt_account_id: str | None = None,
    receipt_client_id: str | None = None,
) -> ApplyOutcome:
    """변경 목록을 의존성 순서로 적용한다.

    `incoming_side` 는 들어온 변경이 **어느 역할** 에서 왔는지다:
        서버가 push 를 적용할 때  → `client`
        클라이언트가 pull 을 적용할 때 → `server`
    무승부는 항상 서버 값이 이긴다.
    """
    outcome = ApplyOutcome()
    use_receipts = bool(receipt_account_id and receipt_client_id)
    grouped: dict[str, list[dict]] = {table: [] for table in SYNC_TABLE_ORDER}
    for change in changes:
        table = change.get("table")
        if table not in SYNC_TABLES:
            rejected = {
                "seq": change.get("seq"),
                "table": table,
                "rowId": change.get("rowId"),
                "reason": "unknown_table",
            }
            outcome.rejected.append(rejected)
            outcome.results.append({**rejected, "status": "rejected"})
            continue
        grouped[table].append(change)

    savepoint_index = 0
    for table in SYNC_TABLE_ORDER:
        for change in grouped[table]:
            savepoint_index += 1
            savepoint = f"sync_apply_{savepoint_index}"
            row_outcome = ApplyOutcome()
            cursor.execute(f"SAVEPOINT {savepoint}")
            payload_hash = None
            source_seq = change.get("seq")
            try:
                if use_receipts and source_seq is not None:
                    payload_hash = sync_payload_hash(change)
                    receipt = get_sync_receipt(
                        cursor,
                        receipt_account_id,
                        receipt_client_id,
                        int(source_seq),
                    )
                    if receipt is not None:
                        if receipt["payloadHash"] != payload_hash:
                            raise ReceiptPayloadMismatch(
                                f"source seq {source_seq} was replayed with a different payload"
                            )
                        cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
                        _merge_receipt_outcome(outcome, receipt["outcome"])
                        continue
                _apply_one(
                    cursor,
                    table,
                    change,
                    incoming_side,
                    columns_by_table,
                    row_outcome,
                    record_issues,
                )
            except Exception as exc:  # noqa: BLE001 - 한 행의 실패가 배치를 죽이지 않게
                cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                if isinstance(exc, ReceiptPayloadMismatch):
                    cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
                    raise
                logger.exception(
                    "sync apply failed: %s %s", table, change.get("rowId")
                )
                rejected = {
                    "seq": change.get("seq"),
                    "table": table,
                    "rowId": change.get("rowId"),
                    "reason": str(exc),
                }
                outcome.rejected.append(rejected)
                result = {**rejected, "status": "rejected"}
                outcome.results.append(result)
                if use_receipts and source_seq is not None and payload_hash is not None:
                    if not isinstance(exc, ReceiptPayloadMismatch):
                        store_sync_receipt(
                            cursor,
                            receipt_account_id,
                            receipt_client_id,
                            int(source_seq),
                            payload_hash,
                            {
                                "applied": 0,
                                "skipped": 0,
                                "deferred": [],
                                "rejected": [rejected],
                                "conflicts": [],
                                "duplicates": [],
                                "issueIds": [],
                                "results": [result],
                                "appliedRefs": [],
                            },
                        )
                cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
                continue

            _merge_outcome(outcome, row_outcome)
            result = _row_result(cursor, table, change, row_outcome)
            outcome.results.append(result)
            if (
                use_receipts
                and source_seq is not None
                and payload_hash is not None
                and result["status"] != "deferred"
            ):
                store_sync_receipt(
                    cursor,
                    receipt_account_id,
                    receipt_client_id,
                    int(source_seq),
                    payload_hash,
                    {
                        **row_outcome.as_dict(),
                        "results": [result],
                        "appliedRefs": row_outcome.applied_refs,
                    },
                )
            cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
    return outcome


class ReceiptPayloadMismatch(ValueError):
    pass


def _merge_receipt_outcome(target: ApplyOutcome, payload: dict) -> None:
    target.applied += int(payload.get("applied") or 0)
    target.skipped += int(payload.get("skipped") or 0)
    target.deferred.extend(payload.get("deferred") or [])
    target.rejected.extend(payload.get("rejected") or [])
    target.conflicts.extend(payload.get("conflicts") or [])
    target.duplicates.extend(payload.get("duplicates") or [])
    target.issue_ids.extend(payload.get("issueIds") or [])
    target.applied_refs.extend(payload.get("appliedRefs") or [])
    target.results.extend(payload.get("results") or [])


def _merge_outcome(target: ApplyOutcome, source: ApplyOutcome) -> None:
    target.applied += source.applied
    target.skipped += source.skipped
    target.deferred.extend(source.deferred)
    target.rejected.extend(source.rejected)
    target.conflicts.extend(source.conflicts)
    target.duplicates.extend(source.duplicates)
    target.issue_ids.extend(source.issue_ids)
    target.applied_refs.extend(source.applied_refs)


def _row_result(cursor, table: str, change: dict, outcome: ApplyOutcome) -> dict:
    if outcome.rejected:
        status = "rejected"
        reason = outcome.rejected[0].get("reason")
    elif outcome.deferred:
        status = "deferred"
        reason = outcome.deferred[0].get("reason")
    elif outcome.applied:
        status = "applied"
        reason = None
    else:
        status = "skipped"
        reason = None

    row_id = change.get("rowId")
    effective_clock = None
    if row_id:
        spec = SYNC_TABLES[table]
        cursor.execute(
            f"SELECT updated_at_utc FROM `{table}` WHERE {spec['pk']} = %s",
            (row_id,),
        )
        current = cursor.fetchone()
        if current:
            effective_clock = _iso(current.get("updated_at_utc"))
    return {
        "seq": change.get("seq"),
        "table": table,
        "rowId": row_id,
        "status": status,
        "reason": reason,
        "conflict": bool(outcome.conflicts),
        "effectiveUpdatedAtUtc": effective_clock,
    }


def _apply_one(
    cursor,
    table: str,
    change: dict,
    incoming_side: str,
    columns_by_table: dict[str, tuple[str, ...]] | None,
    outcome: ApplyOutcome,
    record_issues: bool,
) -> None:
    spec = SYNC_TABLES[table]
    pk = spec["pk"]
    columns = (columns_by_table or {}).get(table) or table_columns(table)
    row_id = change.get("rowId")
    if not row_id:
        outcome.rejected.append({"table": table, "rowId": None, "reason": "missing_row_id"})
        return

    cursor.execute(f"SELECT * FROM `{table}` WHERE {pk} = %s", (row_id,))
    existing = cursor.fetchone()

    if change.get("op") == "delete" and change.get("row") is None:
        _apply_hard_delete(cursor, table, row_id, existing, outcome)
        return

    incoming = deserialize_row(table, change.get("row") or {}, columns)
    incoming[pk] = row_id
    incoming_clock = incoming.get("updated_at_utc")
    if not isinstance(incoming_clock, datetime):
        outcome.rejected.append({"table": table, "rowId": row_id, "reason": "missing_updated_at_utc"})
        return

    missing_parent = _missing_parent(cursor, table, incoming)
    if missing_parent is not None:
        # 오프라인에서 "프로젝트 생성 → 그 안에 메모 생성" 이 흔하다. 이 행만 미룬다.
        outcome.deferred.append(
            {"table": table, "rowId": row_id, "reason": f"missing_parent:{missing_parent}"}
        )
        return

    if existing is None:
        _insert_row(cursor, table, incoming, columns)
        outcome.applied += 1
        outcome.applied_refs.append({"table": table, "rowId": row_id})
        _detect_duplicates(cursor, table, row_id, incoming, outcome, record_issues)
        return

    existing_clock = existing.get("updated_at_utc")
    incoming_wins = _incoming_wins(incoming_clock, existing_clock, incoming_side)

    if incoming_clock == existing_clock and _rows_equivalent(table, existing, incoming, columns):
        # 완전히 같은 행 — 멱등 재적용. 충돌로 기록하지 않는다.
        outcome.skipped += 1
        return

    if _is_true_divergence(
        cursor,
        table,
        row_id,
        change,
        incoming_clock,
        existing_clock,
        incoming_side,
    ):
        loser_side = _other_side(incoming_side) if incoming_wins else incoming_side
        loser_row = existing if incoming_wins else incoming
        loser_clock = existing_clock if incoming_wins else incoming_clock

        preserved_version = _preserve_conflict_content(
            cursor, table, row_id, loser_row, loser_side, loser_clock
        )

        conflict = {
            "table": table,
            "rowId": row_id,
            "winner": "incoming" if incoming_wins else "existing",
            "winnerSide": incoming_side if incoming_wins else _other_side(incoming_side),
            "loserSide": loser_side,
            "baseUpdatedAtUtc": change.get("baseUpdatedAtUtc"),
            "incomingUpdatedAtUtc": _iso(incoming_clock),
            "existingUpdatedAtUtc": _iso(existing_clock),
            "preservedVersion": preserved_version,
        }
        outcome.conflicts.append(conflict)
        if record_issues:
            outcome.issue_ids.append(
                record_issue(
                    cursor,
                    "conflict",
                    ref_table=table,
                    ref_id=row_id,
                    detail=conflict,
                )
            )

    if not incoming_wins:
        outcome.skipped += 1
        return

    _update_row(cursor, table, incoming, columns, pk)
    outcome.applied += 1
    outcome.applied_refs.append({"table": table, "rowId": row_id})
    _detect_duplicates(cursor, table, row_id, incoming, outcome, record_issues)


def _iso(value: datetime | None) -> str | None:
    try:
        from ..timeutil import iso_utc
    except ImportError:  # pragma: no cover
        from timeutil import iso_utc
    return iso_utc(value)


def _incoming_wins(incoming: datetime, existing: datetime | None, incoming_side: str) -> bool:
    if existing is None:
        return True
    if incoming > existing:
        return True
    if incoming < existing:
        return False
    # 동시각 — 서버 값이 이긴다
    return incoming_side == SIDE_SERVER


def _is_true_divergence(
    cursor,
    table: str,
    row_id: str,
    change: dict,
    incoming_clock: datetime,
    existing_clock: datetime | None,
    incoming_side: str,
) -> bool:
    """마지막 합의 시각 이후 양쪽이 모두 바뀐 경우에만 충돌이다.

    기준 시각이 없는 구버전 피어는 예전처럼 보수적으로 충돌로 취급해 패자 내용을
    보존한다. 새 프로토콜에서는 durable `sync_row_state`에서 보낸 기준 시각으로
    단순한 단방향 복제를 조용히 적용한다.
    """
    base_peer_seq = change.get("basePeerSeq")
    if incoming_side == SIDE_CLIENT and base_peer_seq is not None:
        cursor.execute(
            """
            SELECT COALESCE(MAX(seq), 0) AS latest_seq
            FROM change_log
            WHERE table_name = %s AND row_id = %s
            """,
            (table, row_id),
        )
        latest_seq = int(cursor.fetchone()["latest_seq"])
        return latest_seq > int(base_peer_seq)

    base_value = change.get("baseUpdatedAtUtc")
    if not base_value:
        return True
    try:
        from ..timeutil import parse_iso_utc
    except ImportError:  # pragma: no cover
        from timeutil import parse_iso_utc
    base_clock = parse_iso_utc(base_value)
    if base_clock is None:
        return True
    return existing_clock != base_clock and incoming_clock != base_clock


def _rows_equivalent(table: str, existing: dict, incoming: dict, columns: tuple[str, ...]) -> bool:
    for column in columns:
        if column not in incoming:
            continue
        left = existing.get(column)
        right = incoming.get(column)
        if isinstance(left, int) and isinstance(right, bool):
            left, right = bool(left), right
        if left != right:
            return False
    return True


def _missing_parent(cursor, table: str, row: dict) -> str | None:
    for column, parent_table in SYNC_TABLES[table]["parents"]:
        parent_id = row.get(column)
        if not parent_id:
            continue
        cursor.execute(f"SELECT 1 AS ok FROM `{parent_table}` WHERE id = %s", (parent_id,))
        if cursor.fetchone() is None:
            return f"{parent_table}:{parent_id}"
    return None


def _insert_row(cursor, table: str, row: dict, columns: tuple[str, ...]) -> None:
    present = [column for column in columns if column in row]
    placeholders = ", ".join(["%s"] * len(present))
    cursor.execute(
        f"INSERT INTO `{table}` ({', '.join(present)}) VALUES ({placeholders})",
        [row[column] for column in present],
    )


def _update_row(cursor, table: str, row: dict, columns: tuple[str, ...], pk: str) -> None:
    present = [column for column in columns if column in row and column != pk]
    if not present:
        return
    assignments = ", ".join(f"{column} = %s" for column in present)
    cursor.execute(
        f"UPDATE `{table}` SET {assignments} WHERE {pk} = %s",
        [*[row[column] for column in present], row[pk]],
    )


def _apply_hard_delete(cursor, table: str, row_id: str, existing: dict | None, outcome: ApplyOutcome) -> None:
    """상대가 하드 삭제한 행. tombstone 이 있는 테이블은 soft delete 로 반영한다."""
    if existing is None:
        outcome.skipped += 1
        return
    if "deleted_at" in table_columns(table):
        if existing.get("deleted_at") is not None:
            outcome.skipped += 1
            return
        now = utcnow_naive()
        cursor.execute(
            f"UPDATE `{table}` SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
            (now, now, row_id),
        )
    else:
        cursor.execute(f"DELETE FROM `{table}` WHERE id = %s", (row_id,))
    outcome.applied += 1
    outcome.applied_refs.append({"table": table, "rowId": row_id})


def _preserve_conflict_content(
    cursor,
    table: str,
    row_id: str,
    loser_row: dict,
    loser_side: str,
    loser_clock: datetime | None,
) -> int | None:
    """진 쪽 본문을 `memo_versions` 에 보존한다. 이미 같은 내용이 있으면 건너뛴다.

    이 삽입은 **change_log 에 남긴다** — 그렇지 않으면 보존 버전이 이 노드에만 존재하고
    상대는 자기가 잃은 내용을 볼 수 없다.
    """
    content_column = CONTENT_COLUMN_BY_TABLE.get(table)
    if content_column is None:
        return None
    content = loser_row.get(content_column)
    if content is None:
        return None

    cursor.execute(
        "SELECT id FROM memo_versions WHERE memo_id = %s AND content = %s LIMIT 1",
        (row_id, content),
    )
    if cursor.fetchone() is not None:
        return None

    cursor.execute(
        "SELECT COALESCE(MAX(version), 0) AS max_version FROM memo_versions WHERE memo_id = %s",
        (row_id,),
    )
    next_version = int(cursor.fetchone()["max_version"]) + 1
    now = utcnow_naive()
    with change_log_enabled(cursor):
        cursor.execute(
            """
            INSERT INTO memo_versions (id, memo_id, content, version, note, created_at, updated_at_utc)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                generate_id(),
                row_id,
                content,
                next_version,
                conflict_note(loser_side, loser_clock),
                now,
                now,
            ),
        )
    return next_version


def _detect_duplicates(
    cursor,
    table: str,
    row_id: str,
    row: dict,
    outcome: ApplyOutcome,
    record_issues: bool,
) -> None:
    """이름/제목 중복을 감지해 기록한다 (차단하지 않는다)."""
    spec = SYNC_TABLES[table]
    column = spec["duplicate_column"]
    kind = spec["duplicate_kind"]
    if not column or not kind:
        return
    if row.get("deleted_at") is not None:
        return

    name = normalize_name(row.get(column))
    if not name:
        return

    scope_column = spec["duplicate_scope"]
    if scope_column:
        cursor.execute(
            f"SELECT id, {column} AS label FROM `{table}` "
            f"WHERE {scope_column} = %s AND id <> %s AND deleted_at IS NULL",
            (row.get(scope_column), row_id),
        )
    else:
        cursor.execute(
            f"SELECT id, {column} AS label FROM `{table}` WHERE id <> %s AND deleted_at IS NULL",
            (row_id,),
        )

    for candidate in cursor.fetchall():
        if normalize_name(candidate["label"]) != name:
            continue
        duplicate = {
            "kind": kind,
            "table": table,
            "rowId": row_id,
            "peerRowId": candidate["id"],
            "name": name,
        }
        outcome.duplicates.append(duplicate)
        if record_issues:
            outcome.issue_ids.append(
                record_issue(
                    cursor,
                    kind,
                    ref_table=table,
                    ref_id=row_id,
                    peer_ref_id=candidate["id"],
                    detail=duplicate,
                )
            )
