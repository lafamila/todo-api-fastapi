import unittest
from argparse import Namespace
from unittest.mock import Mock

from src import sync_cli


ACCOUNT_ID = "account-1"


def handshake(**overrides) -> dict:
    value = {
        "schemaVersion": sync_cli.SCHEMA_VERSION,
        "accountId": ACCOUNT_ID,
        "permission": "owner",
        "subjectKind": "service_credential",
        "ownerIds": [ACCOUNT_ID],
        "maxSeq": 42,
        "tables": sync_cli.declared_tables(),
        "identity": {
            "accountId": ACCOUNT_ID,
            "email": "owner@example.com",
            "source": "local_identity",
        },
    }
    value.update(overrides)
    return value


def target_fingerprint(**overrides) -> dict:
    value = {
        "columns": {
            table: list(columns)
            for table, columns in sync_cli.declared_tables().items()
        },
        "ownerIds": [ACCOUNT_ID],
        "maxSeq": 42,
        "serverIdentity": {
            "serverUuid": "remote-server",
            "serverHost": "remote-db",
            "serverPort": 3306,
            "database": "todo",
        },
        "error": None,
    }
    value.update(overrides)
    return value


def blockers(**overrides) -> list[str]:
    values = {
        "handshake": handshake(),
        "peer_error": None,
        "owner_ids": [ACCOUNT_ID],
        "identity_cache": {"account_id": ACCOUNT_ID},
        "local_counts": {"projects": 1, "memos": 2},
        "target_fingerprint": target_fingerprint(),
        "local_server_identity": {
            "serverUuid": "local-server",
            "serverHost": "local-db",
            "serverPort": 3306,
            "database": "teddynote",
        },
    }
    values.update(overrides)
    return sync_cli._bootstrap_blockers(**values)


class BootstrapSafetyTests(unittest.TestCase):
    def test_complete_matching_preflight_has_no_blockers(self) -> None:
        self.assertEqual(blockers(), [])

    def test_handshake_failure_is_non_bypassable_and_force_option_is_removed(self) -> None:
        found = blockers(handshake=None, peer_error="401 invalid credential")
        self.assertTrue(any("handshake" in item for item in found))

        with self.assertRaises(SystemExit):
            sync_cli.build_parser().parse_args(
                [
                    "bootstrap",
                    "--target-host",
                    "remote",
                    "--target-database",
                    "todo",
                    "--force",
                ]
            )

    def test_exact_schema_version_and_declaration_are_required(self) -> None:
        old = handshake(schemaVersion=sync_cli.SCHEMA_VERSION - 1)
        found = blockers(handshake=old)
        self.assertTrue(any("schemaVersion" in item for item in found))

        changed_tables = sync_cli.declared_tables()
        changed_tables["memos"] = changed_tables["memos"][:-1]
        found = blockers(handshake=handshake(tables=changed_tables))
        self.assertTrue(any("정확히 일치" in item for item in found))

    def test_identity_cache_and_handshake_identity_must_match_account(self) -> None:
        found = blockers(identity_cache={"account_id": "different"})
        self.assertTrue(any("신원 캐시" in item for item in found))

        found = blockers(
            handshake=handshake(identity={"accountId": "different"})
        )
        self.assertTrue(any("identity.accountId" in item for item in found))

        found = blockers(owner_ids=["different"])
        self.assertTrue(any("owner id" in item for item in found))

    def test_utc_backfill_is_required(self) -> None:
        found = blockers(
            local_counts={
                "projects": 1,
                "projects (updated_at_utc 누락)": 1,
            }
        )
        self.assertTrue(any("백필" in item for item in found))

    def test_target_database_must_match_authenticated_handshake_fingerprint(self) -> None:
        found = blockers(
            target_fingerprint=target_fingerprint(maxSeq=41)
        )
        self.assertTrue(any("change_log" in item for item in found))

        found = blockers(
            target_fingerprint=target_fingerprint(ownerIds=["another-account"])
        )
        self.assertTrue(any("owner 목록" in item for item in found))

        same_server = {
            "serverUuid": "same-server",
            "serverHost": "mysql",
            "serverPort": 3306,
            "database": "teddynote",
        }
        found = blockers(
            local_server_identity=same_server,
            target_fingerprint=target_fingerprint(serverIdentity=same_server),
        )
        self.assertTrue(any("자기 자신" in item for item in found))

    def test_self_wipe_guard_uses_server_identity_not_client_host_alias(self) -> None:
        same_server = {
            "serverUuid": "same-server",
            "serverHost": "mysql-container",
            "serverPort": 3306,
            "database": "teddynote",
        }
        found = blockers(
            local_server_identity=same_server,
            target_fingerprint=target_fingerprint(serverIdentity=same_server),
        )
        self.assertTrue(any("자기 자신" in item for item in found))

    def test_target_database_requires_all_sync_columns(self) -> None:
        fingerprint = target_fingerprint()
        fingerprint["columns"]["memos"].remove("updated_at_utc")
        found = blockers(target_fingerprint=fingerprint)
        self.assertTrue(any("updated_at_utc" in item for item in found))

    def test_target_connection_failure_becomes_a_preflight_blocker(self) -> None:
        migrate = Mock()
        migrate.connect.side_effect = OSError("connection refused")
        args = Namespace(
            target_host="remote",
            target_port=3306,
            target_user="root",
            target_password="secret",
            target_database="todo",
        )

        fingerprint = sync_cli._inspect_bootstrap_target(migrate, args)

        self.assertIn("connection refused", fingerprint["error"])
        found = blockers(target_fingerprint=fingerprint)
        self.assertTrue(any("fingerprint" in item for item in found))


if __name__ == "__main__":
    unittest.main()
