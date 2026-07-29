"""Database-level smoke test for a real two-node sync cycle.

The peer is in-process, but both sides use independent MySQL databases and the
same store/apply functions used by the HTTP sync endpoints.  The test is
deliberately guarded: it will only ever select the two named scratch databases.
"""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta

from tests.scratch_db import (
    assert_scratch_database,
    init_scratch_database,
    truncate_scratch_tables,
)

from src.connectors import DB_CONFIG, get_db_connection
from src.services.sync_apply import SIDE_CLIENT, apply_changes
from src.services.sync_auth import distinct_owner_ids
from src.services.sync_daemon import SyncDaemon
from src.services.sync_peer import SyncPeerUnreachable
from src.services.sync_store import (
    max_change_seq,
    read_changes,
)
from src.sync_schema import SCHEMA_VERSION, declared_tables
from src.timeutil import iso_utc, utcnow_naive


NODE_A_DB = assert_scratch_database("teddynote_sync_node_a")
NODE_B_DB = assert_scratch_database("teddynote_sync_node_b")
ACCOUNT_ID = "two-node-smoke-account"
CLIENT_ID = "two-node-smoke-client"
PROJECT_ID = "two-node-smoke-project"
MEMO_ID = "two-node-smoke-memo"


@contextmanager
def _selected_database(name: str):
    """Temporarily point all connector calls at one guarded scratch database."""
    assert_scratch_database(name)
    previous_database = DB_CONFIG.get("database")
    previous_env = os.environ.get("DB_NAME")
    DB_CONFIG["database"] = name
    os.environ["DB_NAME"] = name
    try:
        yield
    finally:
        DB_CONFIG["database"] = previous_database
        if previous_env is None:
            os.environ.pop("DB_NAME", None)
        else:
            os.environ["DB_NAME"] = previous_env


class _DatabasePeer:
    """SyncPeer-compatible adapter whose endpoint implementation is node B."""

    configured = True
    root = "in-process://node-b"

    def __init__(self) -> None:
        self.delivery_client_ids: list[str] = []
        self.lose_next_push_response = False

    def handshake(self) -> dict:
        with _selected_database(NODE_B_DB):
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    return {
                        "schemaVersion": SCHEMA_VERSION,
                        "accountId": ACCOUNT_ID,
                        "permission": "owner",
                        "subjectKind": "service",
                        "serverTimeUtc": iso_utc(utcnow_naive()),
                        "ownerIds": distinct_owner_ids(cursor),
                        "maxSeq": max_change_seq(cursor),
                        "tables": declared_tables(),
                        "identity": {"accountId": ACCOUNT_ID},
                    }

    def changes(self, since: int, limit: int) -> dict:
        with _selected_database(NODE_B_DB):
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    changes, next_seq = read_changes(
                        cursor, since, limit, ACCOUNT_ID
                    )
                    return {
                        "schemaVersion": SCHEMA_VERSION,
                        "changes": changes,
                        "nextSeq": next_seq,
                        "maxSeq": max_change_seq(cursor),
                        "serverTimeUtc": iso_utc(utcnow_naive()),
                    }

    def push(self, client_id: str, changes: list[dict]) -> dict:
        self.delivery_client_ids.append(client_id)
        with _selected_database(NODE_B_DB):
            with get_db_connection(sync_applying=True) as conn:
                with conn.cursor() as cursor:
                    outcome = apply_changes(
                        cursor,
                        changes,
                        incoming_side=SIDE_CLIENT,
                        receipt_account_id=ACCOUNT_ID,
                        receipt_client_id=client_id,
                    )
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    remote_max_seq = max_change_seq(cursor)

        if self.lose_next_push_response:
            self.lose_next_push_response = False
            raise SyncPeerUnreachable("simulated lost response after node B commit")

        response = outcome.as_dict()
        response.update(
            {
                "schemaVersion": SCHEMA_VERSION,
                "maxSeq": remote_max_seq,
                "nextSeq": remote_max_seq,
                "serverTimeUtc": iso_utc(utcnow_naive()),
            }
        )
        return response


def _seed_node_a(moment: datetime) -> None:
    with _selected_database(NODE_A_DB):
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, owner_id, name, icon, status, is_secret,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, 'Two node smoke', 'Beer', 0, 0, %s, %s, %s)
                    """,
                    (PROJECT_ID, ACCOUNT_ID, moment, moment, moment),
                )
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, %s, 'Smoke memo', 'from A', 0, %s, %s, %s)
                    """,
                    (MEMO_ID, PROJECT_ID, ACCOUNT_ID, moment, moment, moment),
                )


def _update_memo(database: str, content: str, moment: datetime) -> None:
    with _selected_database(database):
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE memos
                    SET content = %s, updated_at = %s, updated_at_utc = %s
                    WHERE id = %s
                    """,
                    (content, moment, moment, MEMO_ID),
                )


def _scalar(database: str, query: str, params=()):
    with _selected_database(database):
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                row = cursor.fetchone()
                return next(iter(row.values()))


class TwoNodeSyncSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reachable = True
        for database in (NODE_A_DB, NODE_B_DB):
            with _selected_database(database):
                reachable = init_scratch_database() and reachable
        if not reachable:
            raise unittest.SkipTest(
                f"MySQL scratch databases unavailable ({NODE_A_DB}, {NODE_B_DB})"
            )

    def setUp(self) -> None:
        for database in (NODE_A_DB, NODE_B_DB):
            with _selected_database(database):
                truncate_scratch_tables()

    def test_push_pull_conflict_and_durable_cursors_converge(self) -> None:
        baseline = datetime(2026, 7, 29, 6, 0, 0)
        _seed_node_a(baseline)
        peer = _DatabasePeer()
        daemon = SyncDaemon(peer=peer, client_id=CLIENT_ID)

        with _selected_database(NODE_A_DB):
            initial = daemon._cycle_sync()
        self.assertTrue(initial["ok"])
        self.assertEqual(initial["accountId"], ACCOUNT_ID)
        self.assertEqual(initial["peerSchemaVersion"], SCHEMA_VERSION)
        self.assertEqual(initial["push"]["conflicts"], [])
        self.assertEqual(
            _scalar(NODE_B_DB, "SELECT content FROM memos WHERE id = %s", (MEMO_ID,)),
            "from A",
        )

        _update_memo(NODE_B_DB, "one-sided B edit", baseline + timedelta(minutes=1))
        with _selected_database(NODE_A_DB):
            pulled = daemon._cycle_sync()
        self.assertGreaterEqual(pulled["pull"]["applied"], 1)
        self.assertEqual(
            _scalar(NODE_A_DB, "SELECT content FROM memos WHERE id = %s", (MEMO_ID,)),
            "one-sided B edit",
        )

        _update_memo(NODE_A_DB, "concurrent loser from A", baseline + timedelta(minutes=2))
        _update_memo(NODE_B_DB, "concurrent winner from B", baseline + timedelta(minutes=3))
        with _selected_database(NODE_A_DB):
            conflicted = daemon._cycle_sync()
        self.assertEqual(len(conflicted["push"]["conflicts"]), 1)
        self.assertEqual(conflicted["push"]["conflicts"][0]["loserSide"], "client")
        self.assertEqual(
            _scalar(NODE_B_DB, "SELECT content FROM memos WHERE id = %s", (MEMO_ID,)),
            "concurrent winner from B",
        )
        self.assertEqual(
            _scalar(
                NODE_B_DB,
                "SELECT COUNT(*) AS count FROM memo_versions "
                "WHERE memo_id = %s AND content = 'concurrent loser from A'",
                (MEMO_ID,),
            ),
            1,
        )

        # Conflict-preservation versions intentionally enter change_log; one more
        # cycle exchanges those rows and leaves both durable queues/cursors idle.
        with _selected_database(NODE_A_DB):
            daemon._cycle_sync()
        self.assertEqual(
            _scalar(NODE_A_DB, "SELECT content FROM memos WHERE id = %s", (MEMO_ID,)),
            "concurrent winner from B",
        )
        for database in (NODE_A_DB, NODE_B_DB):
            self.assertEqual(
                _scalar(
                    database,
                    "SELECT COUNT(*) AS count FROM sync_retry_queue "
                    "WHERE direction IN ('push', 'pull')",
                ),
                0,
            )
        with _selected_database(NODE_A_DB):
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT last_pushed_seq, last_pulled_seq "
                        "FROM sync_state WHERE peer = %s",
                        (CLIENT_ID,),
                    )
                    state = cursor.fetchone()
                    local_max = max_change_seq(cursor)
            with _selected_database(NODE_B_DB):
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        remote_max = max_change_seq(cursor)
        self.assertEqual(int(state["last_pushed_seq"]), local_max)
        self.assertEqual(int(state["last_pulled_seq"]), remote_max)

    def test_restart_reuses_epoch_after_lost_push_response(self) -> None:
        _seed_node_a(datetime(2026, 7, 29, 7, 0, 0))
        peer = _DatabasePeer()
        peer.lose_next_push_response = True
        first_daemon = SyncDaemon(peer=peer, client_id=CLIENT_ID)

        with _selected_database(NODE_A_DB):
            first = first_daemon._cycle_sync
            with self.assertRaises(SyncPeerUnreachable):
                first()
        first_delivery_id = peer.delivery_client_ids[-1]
        self.assertEqual(
            _scalar(
                NODE_B_DB,
                "SELECT COUNT(*) AS count FROM projects WHERE id = %s",
                (PROJECT_ID,),
            ),
            1,
        )

        restarted_daemon = SyncDaemon(peer=peer, client_id=CLIENT_ID)
        with _selected_database(NODE_A_DB):
            recovered = restarted_daemon._cycle_sync()
        self.assertTrue(recovered["ok"])
        self.assertEqual(peer.delivery_client_ids[-1], first_delivery_id)
        self.assertEqual(
            _scalar(
                NODE_A_DB,
                "SELECT COUNT(*) AS count FROM sync_retry_queue "
                "WHERE direction IN ('push', 'pull')",
            ),
            0,
        )
        self.assertEqual(
            _scalar(
                NODE_B_DB,
                "SELECT COUNT(*) AS count FROM sync_receipts "
                "WHERE account_id = %s AND client_id = %s",
                (ACCOUNT_ID, first_delivery_id),
            ),
            2,
        )
        self.assertEqual(
            _scalar(NODE_B_DB, "SELECT COUNT(*) AS count FROM memo_versions"),
            0,
        )


if __name__ == "__main__":
    unittest.main()
