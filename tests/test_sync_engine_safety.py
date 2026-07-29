"""동기화 엔진의 데이터 유실 방지 회귀 테스트."""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.scratch_db import (
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

SCRATCH_DB = use_scratch_database()

from src.connectors import DB_CONFIG, get_db_connection  # noqa: E402
from src.services import sync_apply as sync_apply_module  # noqa: E402
from src.services.sync_apply import (  # noqa: E402
    SIDE_CLIENT,
    SIDE_SERVER,
    ReceiptPayloadMismatch,
    apply_changes,
)
from src.services.sync_daemon import SyncDaemon  # noqa: E402
from src.services.sync_peer import SyncPeerUnreachable  # noqa: E402
from src.services.sync_store import (  # noqa: E402
    enqueue_sync_retry,
    get_sync_state,
    list_sync_retries,
)
from src.timeutil import iso_utc  # noqa: E402

ACCOUNT = "sync-engine-account"
BASE = datetime(2026, 7, 29, 5, 0, 0)
REACHABLE = init_scratch_database()


class _PushPeer:
    configured = True
    root = "http://peer.example"

    def push(self, client_id: str, changes: list[dict]) -> dict:
        results = []
        for change in changes:
            rejected = change["rowId"] == "m-rejected"
            results.append(
                {
                    "seq": change["seq"],
                    "table": change["table"],
                    "rowId": change["rowId"],
                    "status": "rejected" if rejected else "applied",
                    "reason": "forced rejection" if rejected else None,
                    "conflict": False,
                    "effectiveUpdatedAtUtc": (
                        None if rejected else change["row"]["updated_at_utc"]
                    ),
                }
            )
        return {
            "applied": sum(result["status"] == "applied" for result in results),
            "skipped": 0,
            "deferred": [],
            "rejected": [
                result for result in results if result["status"] == "rejected"
            ],
            "conflicts": [],
            "duplicates": [],
            "results": results,
        }


class _PullPeer:
    configured = True
    root = "http://peer.example"

    def __init__(self, changes: list[dict]):
        self._changes = changes

    def changes(self, since: int, limit: int) -> dict:
        pending = [change for change in self._changes if int(change["seq"]) > since]
        return {
            "changes": pending[:limit],
            "nextSeq": max([since, *[int(change["seq"]) for change in pending[:limit]]]),
            "maxSeq": max([since, *[int(change["seq"]) for change in self._changes]]),
        }


class _LostResponseReceiptPeer:
    configured = True
    root = "http://peer.example"

    def __init__(self):
        self.lose_next_response = True

    def push(self, client_id: str, changes: list[dict]) -> dict:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                outcome = apply_changes(
                    cursor,
                    changes,
                    SIDE_CLIENT,
                    receipt_account_id=ACCOUNT,
                    receipt_client_id=client_id,
                )
        response = outcome.as_dict()
        if self.lose_next_response:
            self.lose_next_response = False
            raise SyncPeerUnreachable("response lost after server commit")
        return response


@unittest.skipUnless(REACHABLE, f"MySQL scratch DB unavailable ({DB_CONFIG['database']})")
class SyncEngineSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        truncate_scratch_tables()

    def _insert_project(self, project_id: str = "p1") -> None:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, owner_id, name, icon, status, is_secret,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, 'Project', 'Beer', 0, 0, %s, %s, %s)
                    """,
                    (project_id, ACCOUNT, BASE, BASE, BASE),
                )

    def _insert_memo(
        self,
        memo_id: str,
        content: str,
        moment: datetime,
        project_id: str = "p1",
    ) -> None:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, %s, 'Memo', %s, 0, %s, %s, %s)
                    """,
                    (memo_id, project_id, ACCOUNT, content, BASE, moment, moment),
                )

    def _memo_change(
        self,
        seq: int,
        memo_id: str,
        content: str,
        moment: datetime,
        project_id: str = "p1",
        base: datetime | None = None,
    ) -> dict:
        return {
            "seq": seq,
            "table": "memos",
            "rowId": memo_id,
            "op": "update",
            "baseUpdatedAtUtc": iso_utc(base) if base else None,
            "row": {
                "id": memo_id,
                "project_id": project_id,
                "created_by": ACCOUNT,
                "title": "Memo",
                "content": content,
                "status": 0,
                "created_at": BASE.isoformat(timespec="milliseconds"),
                "updated_at": moment.isoformat(timespec="milliseconds"),
                "updated_at_utc": iso_utc(moment),
                "deleted_at": None,
            },
        }

    def _memo(self, memo_id: str) -> dict | None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM memos WHERE id = %s", (memo_id,))
                return cursor.fetchone()

    def _versions(self, memo_id: str) -> list[dict]:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM memo_versions WHERE memo_id = %s ORDER BY version",
                    (memo_id,),
                )
                return cursor.fetchall()

    def test_row_failure_rolls_back_its_partial_write_and_later_row_applies(self) -> None:
        self._insert_project()
        original = sync_apply_module._detect_duplicates

        def fail_after_insert(cursor, table, row_id, row, outcome, record_issues):
            if row_id == "m-rejected":
                raise RuntimeError("forced failure after insert")
            return original(cursor, table, row_id, row, outcome, record_issues)

        changes = [
            self._memo_change(1, "m-rejected", "bad", BASE),
            self._memo_change(2, "m-good", "good", BASE),
        ]
        with patch.object(sync_apply_module, "_detect_duplicates", fail_after_insert):
            with get_db_connection(sync_applying=True) as conn:
                with conn.cursor() as cursor:
                    outcome = apply_changes(cursor, changes, SIDE_SERVER)

        self.assertIsNone(self._memo("m-rejected"))
        self.assertEqual(self._memo("m-good")["content"], "good")
        self.assertEqual(
            [(result["seq"], result["status"]) for result in outcome.results],
            [(1, "rejected"), (2, "applied")],
        )

    def test_mixed_pull_batch_advances_cursor_and_durably_retries_orphan(self) -> None:
        self._insert_project()
        changes = [
            self._memo_change(1, "m-orphan", "later", BASE, project_id="p-later"),
            self._memo_change(2, "m-good", "now", BASE),
        ]
        daemon = SyncDaemon(peer=_PullPeer(changes), client_id="peer-a")
        report = daemon._pull_rounds({})

        self.assertEqual(report["applied"], 1)
        self.assertEqual(len(report["deferred"]), 1)
        self.assertEqual(self._memo("m-good")["content"], "now")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self.assertEqual(get_sync_state(cursor, "peer-a")["last_pulled_seq"], 2)
                queued = list_sync_retries(cursor, "peer-a", "pull", 10)
        self.assertEqual([(row["seq"], row["row_id"]) for row in queued], [(1, "m-orphan")])

        self._insert_project("p-later")
        retry = daemon._retry_pull_queue({})
        self.assertEqual(retry["applied"], 1)
        self.assertEqual(self._memo("m-orphan")["content"], "later")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self.assertFalse(list_sync_retries(cursor, "peer-a", "pull", 10))

    def test_rejected_push_is_queued_before_cursor_ack(self) -> None:
        self._insert_project()
        self._insert_memo("m-rejected", "bad", BASE)
        self._insert_memo("m-good", "good", BASE)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO change_log (table_name, row_id, op, changed_at_utc)
                    VALUES ('memos', 'm-rejected', 'update', %s),
                           ('memos', 'm-good', 'update', %s)
                    """,
                    (BASE, BASE),
                )

        daemon = SyncDaemon(peer=_PushPeer(), client_id="peer-a")
        report = daemon._push_rounds({})
        self.assertEqual(report["applied"], 1)
        self.assertEqual(len(report["rejected"]), 1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                state = get_sync_state(cursor, "peer-a")
                queued = list_sync_retries(cursor, "peer-a", "pushdead", 10)
        self.assertEqual(state["last_pushed_seq"], 2)
        self.assertEqual([(row["seq"], row["row_id"]) for row in queued], [(1, "m-rejected")])

    def test_retry_payload_is_immutable_for_the_same_source_sequence(self) -> None:
        first = self._memo_change(1, "m1", "first", BASE)
        second = self._memo_change(1, "m1", "changed", BASE)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                enqueue_sync_retry(cursor, "peer-a", "push", first, "pending")
        with self.assertRaisesRegex(ValueError, "different immutable payload"):
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    enqueue_sync_retry(cursor, "peer-a", "push", second, "pending")

    def test_terminal_receipt_makes_lost_response_replay_idempotent(self) -> None:
        self._insert_project()
        self._insert_memo("m1", "server", BASE + timedelta(seconds=10))
        change = self._memo_change(
            1, "m1", "client", BASE + timedelta(seconds=20), base=BASE
        )
        kwargs = {
            "incoming_side": SIDE_CLIENT,
            "receipt_account_id": ACCOUNT,
            "receipt_client_id": "laptop:epoch-1",
        }
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                first = apply_changes(cursor, [change], **kwargs)
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                replay = apply_changes(cursor, [change], **kwargs)

        self.assertEqual(len(first.conflicts), 1)
        self.assertEqual(len(replay.conflicts), 1)
        self.assertEqual(len(self._versions("m1")), 1)
        self.assertEqual(self._memo("m1")["content"], "client")

    def test_lost_http_response_keeps_immutable_outbox_and_restart_replays_receipt(self) -> None:
        self._insert_project()
        self._insert_memo("m1", "client", BASE)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO change_log (table_name, row_id, op, changed_at_utc)
                    VALUES ('memos', 'm1', 'update', %s)
                    """,
                    (BASE,),
                )
        peer = _LostResponseReceiptPeer()
        first_daemon = SyncDaemon(peer=peer, client_id="peer-a")
        with self.assertRaises(SyncPeerUnreachable):
            first_daemon._push_rounds({})
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                queued = list_sync_retries(cursor, "peer-a", "push", 10)
                epoch_before = get_sync_state(cursor, "peer-a")["client_epoch"]
        self.assertEqual([(row["seq"], row["row_id"]) for row in queued], [(1, "m1")])

        restarted_daemon = SyncDaemon(peer=peer, client_id="peer-a")
        report = restarted_daemon._push_rounds({})
        self.assertEqual(report["skipped"], 1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self.assertFalse(list_sync_retries(cursor, "peer-a", "push", 10))
                self.assertEqual(
                    get_sync_state(cursor, "peer-a")["client_epoch"], epoch_before
                )

    def test_push_base_peer_sequence_distinguishes_one_way_from_concurrent_edit(self) -> None:
        self._insert_project()
        self._insert_memo("m-one-way", "server base", BASE)
        self._insert_memo("m-concurrent", "server base", BASE)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO change_log (table_name, row_id, op, changed_at_utc)
                    VALUES ('memos', 'm-one-way', 'insert', %s),
                           ('memos', 'm-concurrent', 'insert', %s)
                    """,
                    (BASE, BASE),
                )
                cursor.execute(
                    """
                    UPDATE memos
                    SET content = 'server concurrent', updated_at = %s,
                        updated_at_utc = %s
                    WHERE id = 'm-concurrent'
                    """,
                    (BASE + timedelta(seconds=10), BASE + timedelta(seconds=10)),
                )

        one_way = self._memo_change(
            11,
            "m-one-way",
            "client only",
            BASE + timedelta(seconds=20),
            base=BASE,
        )
        one_way["basePeerSeq"] = 1
        concurrent = self._memo_change(
            12,
            "m-concurrent",
            "client concurrent",
            BASE + timedelta(seconds=20),
            base=BASE,
        )
        concurrent["basePeerSeq"] = 2
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                outcome = apply_changes(
                    cursor, [one_way, concurrent], SIDE_CLIENT
                )
        self.assertEqual(
            [(item["rowId"], item["winner"]) for item in outcome.conflicts],
            [("m-concurrent", "incoming")],
        )
        self.assertEqual(self._memo("m-one-way")["content"], "client only")

    def test_receipt_rejects_same_sequence_with_different_payload_but_is_account_scoped(self) -> None:
        self._insert_project()
        original = self._memo_change(1, "m1", "first", BASE)
        changed = self._memo_change(1, "m1", "second", BASE + timedelta(seconds=1))
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                apply_changes(
                    cursor,
                    [original],
                    SIDE_CLIENT,
                    receipt_account_id="account-a",
                    receipt_client_id="laptop:epoch-1",
                )
        with self.assertRaises(ReceiptPayloadMismatch):
            with get_db_connection(sync_applying=True) as conn:
                with conn.cursor() as cursor:
                    apply_changes(
                        cursor,
                        [changed],
                        SIDE_CLIENT,
                        receipt_account_id="account-a",
                        receipt_client_id="laptop:epoch-1",
                    )
        self.assertEqual(self._memo("m1")["content"], "first")

        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                other_account = apply_changes(
                    cursor,
                    [changed],
                    SIDE_CLIENT,
                    receipt_account_id="account-b",
                    receipt_client_id="laptop:epoch-1",
                )
        self.assertEqual(other_account.results[0]["status"], "applied")
        self.assertEqual(self._memo("m1")["content"], "second")

    def test_one_way_remote_edit_does_not_create_conflict(self) -> None:
        self._insert_project()
        self._insert_memo("m1", "base", BASE)
        change = self._memo_change(
            1, "m1", "remote edit", BASE + timedelta(seconds=10), base=BASE
        )
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                outcome = apply_changes(cursor, [change], SIDE_SERVER)
        self.assertEqual(outcome.conflicts, [])
        self.assertFalse(self._versions("m1"))
        self.assertEqual(self._memo("m1")["content"], "remote edit")

    def test_one_way_local_edit_ignores_unchanged_remote_without_conflict(self) -> None:
        self._insert_project()
        self._insert_memo("m1", "local edit", BASE + timedelta(seconds=10))
        change = self._memo_change(1, "m1", "base", BASE, base=BASE)
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                outcome = apply_changes(cursor, [change], SIDE_SERVER)
        self.assertEqual(outcome.conflicts, [])
        self.assertEqual(outcome.skipped, 1)
        self.assertFalse(self._versions("m1"))
        self.assertEqual(self._memo("m1")["content"], "local edit")

    def test_true_concurrent_edit_preserves_loser(self) -> None:
        self._insert_project()
        self._insert_memo("m1", "local", BASE + timedelta(seconds=10))
        change = self._memo_change(
            1, "m1", "remote", BASE + timedelta(seconds=20), base=BASE
        )
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                outcome = apply_changes(cursor, [change], SIDE_SERVER)
        self.assertEqual(len(outcome.conflicts), 1)
        self.assertEqual([row["content"] for row in self._versions("m1")], ["local"])
        self.assertEqual(self._memo("m1")["content"], "remote")

    def test_empty_string_loser_content_is_preserved(self) -> None:
        self._insert_project()
        self._insert_memo("m1", "", BASE + timedelta(seconds=10))
        change = self._memo_change(
            1, "m1", "remote", BASE + timedelta(seconds=20), base=BASE
        )
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                outcome = apply_changes(cursor, [change], SIDE_SERVER)
        self.assertEqual(len(outcome.conflicts), 1)
        self.assertEqual([row["content"] for row in self._versions("m1")], [""])


if __name__ == "__main__":
    unittest.main()
