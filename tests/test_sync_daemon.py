"""데몬 preflight — 스키마 handshake 3케이스 / 시계 편차 / 신원 불일치.

세 검사 모두 **부분 적용 없이 중단** 해야 한다. 잘못된 신원이나 틀어진 시계로 LWW 를 돌리면
조용히 최신 내용이 버려지고, 손으로 되돌리기가 매우 어렵다.
"""

import unittest
from datetime import datetime, timedelta

from tests.scratch_db import (
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

SCRATCH_DB = use_scratch_database()

from src.connectors import DB_CONFIG, get_db_connection  # noqa: E402
from src.services import sync_daemon as sync_daemon_module  # noqa: E402
from src.services.sync_daemon import SyncBlocked, SyncDaemon  # noqa: E402
from src.services.sync_peer import SyncPeer, normalize_peer_root  # noqa: E402
from src.sync_schema import SCHEMA_VERSION, declared_tables  # noqa: E402
from src.timeutil import iso_utc, utcnow_naive  # noqa: E402

ACCOUNT = "remote-account-1"


REACHABLE = init_scratch_database()


def _handshake(**overrides) -> dict:
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "accountId": ACCOUNT,
        "permission": "owner",
        "serverTimeUtc": iso_utc(utcnow_naive()),
        "ownerIds": [ACCOUNT],
        "maxSeq": 0,
        "tables": declared_tables(),
        "identity": {"accountId": ACCOUNT, "email": "remote@example.com"},
    }
    payload.update(overrides)
    return payload


class SchemaHandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.daemon = SyncDaemon(peer=SyncPeer("http://peer.example", "k", "s"))

    def test_same_version_syncs_all_columns(self) -> None:
        self.assertEqual(self.daemon._check_schema(_handshake()), {})

    def test_local_ahead_blocks_by_default(self) -> None:
        with self.assertRaises(SyncBlocked) as caught:
            self.daemon._check_schema(_handshake(schemaVersion=SCHEMA_VERSION - 1))
        self.assertEqual(caught.exception.kind, "schema")
        self.assertIn("로컬 스키마가 앞서", caught.exception.message)

    def test_local_ahead_with_drift_allowed_syncs_shared_columns_only(self) -> None:
        peer_tables = declared_tables()
        peer_tables["memos"] = [
            column for column in peer_tables["memos"] if column != "status"
        ]
        original = sync_daemon_module.SYNC_ALLOW_SCHEMA_DRIFT
        sync_daemon_module.SYNC_ALLOW_SCHEMA_DRIFT = True
        try:
            columns = self.daemon._check_schema(
                _handshake(schemaVersion=SCHEMA_VERSION - 1, tables=peer_tables)
            )
        finally:
            sync_daemon_module.SYNC_ALLOW_SCHEMA_DRIFT = original

        self.assertNotIn("status", columns["memos"])
        self.assertIn("updated_at_utc", columns["memos"])
        self.assertIn("id", columns["memos"])

    def test_peer_ahead_always_blocks(self) -> None:
        original = sync_daemon_module.SYNC_ALLOW_SCHEMA_DRIFT
        sync_daemon_module.SYNC_ALLOW_SCHEMA_DRIFT = True
        try:
            with self.assertRaises(SyncBlocked) as caught:
                self.daemon._check_schema(_handshake(schemaVersion=SCHEMA_VERSION + 1))
        finally:
            sync_daemon_module.SYNC_ALLOW_SCHEMA_DRIFT = original
        self.assertIn("원격 스키마가 앞서", caught.exception.message)

    def test_peer_schema_version_is_recorded_for_status(self) -> None:
        self.daemon._check_schema(_handshake())
        self.assertEqual(self.daemon.runtime.peer_schema_version, SCHEMA_VERSION)


class ClockSkewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.daemon = SyncDaemon(peer=SyncPeer("http://peer.example", "k", "s"))

    def test_small_skew_passes(self) -> None:
        self.daemon._check_clock(_handshake())
        self.assertLess(self.daemon.runtime.clock_skew_seconds, 5)

    def test_large_skew_blocks(self) -> None:
        skewed = iso_utc(utcnow_naive() + timedelta(minutes=3))
        with self.assertRaises(SyncBlocked) as caught:
            self.daemon._check_clock(_handshake(serverTimeUtc=skewed))
        self.assertEqual(caught.exception.kind, "clock")
        self.assertGreater(caught.exception.detail["skewSeconds"], 5)


@unittest.skipUnless(REACHABLE, f"MySQL scratch DB unavailable ({DB_CONFIG['database']})")
class IdentityPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.daemon = SyncDaemon(peer=SyncPeer("http://peer.example", "k", "s"))
        truncate_scratch_tables()

    def _seed_owner(self, owner_id: str) -> None:
        moment = datetime(2026, 7, 29, 5, 0, 0)
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, owner_id, name, icon, status, is_secret,
                         created_at, updated_at, updated_at_utc)
                    VALUES ('p1', %s, 'Project', 'Beer', 0, 0, %s, %s, %s)
                    """,
                    (owner_id, moment, moment, moment),
                )

    def test_matching_owner_id_passes(self) -> None:
        self._seed_owner(ACCOUNT)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self.daemon._check_identity(cursor, _handshake())

    def test_empty_local_data_passes(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self.daemon._check_identity(cursor, _handshake())

    def test_mismatched_owner_id_blocks_and_records_issue(self) -> None:
        """시나리오 4 — owner id 를 일부러 어긋나게 하면 부분 적용 없이 중단된다."""
        self._seed_owner("stale-local-account")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                with self.assertRaises(SyncBlocked) as caught:
                    self.daemon._check_identity(cursor, _handshake())

        self.assertEqual(caught.exception.kind, "identity")
        self.assertIn("link-identity", caught.exception.message)
        self.assertEqual(caught.exception.detail["mismatched"], ["stale-local-account"])

        # 검사 커넥션은 raise 로 롤백되므로 이슈는 별도 트랜잭션에서 남긴다
        self.daemon._store_blocked(caught.exception)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT kind, resolved_at, detail FROM sync_issues WHERE kind = 'identity'"
                )
                issues = cursor.fetchall()
        self.assertEqual(len(issues), 1)
        self.assertIsNone(issues[0]["resolved_at"])
        self.assertIn("stale-local-account", issues[0]["detail"])


class PeerUrlTests(unittest.TestCase):
    def test_trailing_api_suffix_is_stripped(self) -> None:
        self.assertEqual(
            normalize_peer_root("https://todo.lafamila.xyz/api/"),
            "https://todo.lafamila.xyz",
        )

    def test_bare_root_is_kept(self) -> None:
        self.assertEqual(
            normalize_peer_root("https://todo.lafamila.xyz"), "https://todo.lafamila.xyz"
        )

    def test_peer_requires_url_and_credential(self) -> None:
        self.assertFalse(SyncPeer("https://todo.example", "", "").configured)
        self.assertTrue(SyncPeer("https://todo.example", "k", "s").configured)


if __name__ == "__main__":
    unittest.main()
