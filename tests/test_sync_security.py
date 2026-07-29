"""동기화 보안 경계와 startup schema 검증 회귀 테스트."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pymysql
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from tests.scratch_db import (
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

SCRATCH_DB = use_scratch_database()

from src.connectors import (  # noqa: E402
    DB_CONFIG,
    _try_ddl,
    get_db_connection,
)
from src.config import SYNC_BATCH_LIMIT  # noqa: E402
from src.routers import sync as sync_router_module  # noqa: E402
from src.routers.sync import (  # noqa: E402
    ResolveIssuesRequest,
    SyncChangeInput,
    SyncPushRequest,
    _authorize_sync_changes,
    _authorized_issue_ids,
    _ensure_sync_admin,
    _execute_peer_merge,
    _require_memo_write_access,
    _resolve_push_columns,
    _scoped_issues,
    sync_push,
)
from src.services import sync_auth as sync_auth_module  # noqa: E402
from src.services.http_json import JsonResponse  # noqa: E402
from src.services.sync_auth import (  # noqa: E402
    SERVICE_CREDENTIAL_KEY_HEADER,
    SERVICE_CREDENTIAL_SECRET_HEADER,
    ServiceCredentialAuthenticator,
    SyncAuthError,
    SyncAuthUnavailable,
    SyncPrincipal,
    distinct_owner_ids,
)
from src.services.sync_store import record_issue  # noqa: E402
from src.sync_schema import SCHEMA_VERSION, declared_tables  # noqa: E402
from src.utils import check_project_membership, get_project_role  # noqa: E402


REACHABLE = init_scratch_database()
MOMENT = datetime(2026, 7, 29, 5, 0, 0)
CLIENT_ID = "laptop:123e4567-e89b-42d3-a456-426614174000"


def _user(account_id: str, permission: str = "admin") -> dict:
    return {
        "id": account_id,
        "permission": permission,
        "is_admin": permission in {"owner", "superadmin", "admin"},
    }


def _principal(account_id: str = "account-a") -> SyncPrincipal:
    return SyncPrincipal(
        account_id=account_id,
        permission="owner",
        subject_kind="service_credential",
        subject_id="credential-1",
    )


class PushSchemaGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_mismatch_is_rejected_before_database_mutation(self) -> None:
        body = SyncPushRequest(
            clientId=CLIENT_ID,
            schemaVersion=SCHEMA_VERSION - 1,
            changes=[SyncChangeInput(table="memos", rowId="m1")],
        )
        with self.assertRaises(HTTPException) as caught:
            await sync_push(body, _principal())
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "sync_schema_mismatch")

    def test_explicit_older_schema_negotiates_declared_columns(self) -> None:
        tables = declared_tables()
        tables["memos"] = [
            column for column in tables["memos"] if column != "status"
        ]
        columns = _resolve_push_columns(
            SyncPushRequest(
                clientId=CLIENT_ID,
                schemaVersion=SCHEMA_VERSION - 1,
                allowSchemaDrift=True,
                tables=tables,
                changes=[SyncChangeInput(table="memos", rowId="m1")],
            )
        )
        self.assertNotIn("status", columns["memos"])
        self.assertIn("id", columns["memos"])
        self.assertIn("updated_at_utc", columns["memos"])

    def test_change_model_preserves_base_clock_metadata(self) -> None:
        change = SyncChangeInput(
            table="memos",
            rowId="m1",
            baseUpdatedAtUtc="2026-07-29T05:00:00.000Z",
            basePeerSeq=17,
        )
        self.assertEqual(
            change.model_dump()["baseUpdatedAtUtc"],
            "2026-07-29T05:00:00.000Z",
        )
        self.assertEqual(change.model_dump()["basePeerSeq"], 17)

    def test_push_batch_size_is_bounded_by_configured_limit(self) -> None:
        changes = [
            SyncChangeInput(table="memos", rowId=f"m-{index}")
            for index in range(SYNC_BATCH_LIMIT + 1)
        ]
        with self.assertRaises(ValidationError):
            SyncPushRequest(
                clientId=CLIENT_ID,
                schemaVersion=SCHEMA_VERSION,
                changes=changes,
            )

    def test_push_rejects_empty_batch(self) -> None:
        with self.assertRaises(ValidationError):
            SyncPushRequest(
                clientId=CLIENT_ID,
                schemaVersion=SCHEMA_VERSION,
                changes=[],
            )


class DdlGuardTests(unittest.TestCase):
    def test_known_duplicate_column_error_is_idempotent(self) -> None:
        class Cursor:
            def execute(self, _statement):
                raise pymysql.OperationalError(1060, "duplicate column")

        _try_ddl(Cursor(), "ALTER TABLE projects ADD COLUMN duplicate INT")

    def test_unknown_ddl_error_is_not_swallowed(self) -> None:
        class Cursor:
            def execute(self, _statement):
                raise pymysql.OperationalError(1146, "table missing")

        with self.assertRaises(pymysql.OperationalError):
            _try_ddl(Cursor(), "ALTER TABLE missing ADD COLUMN value INT")


class SyncCredentialBindingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _headers(key_id: str) -> dict[str, str]:
        return {
            SERVICE_CREDENTIAL_KEY_HEADER: key_id,
            SERVICE_CREDENTIAL_SECRET_HEADER: "presented-secret",
        }

    @staticmethod
    def _valid_auth_response(*_args, **_kwargs) -> JsonResponse:
        return JsonResponse(
            200,
            {
                "valid": True,
                "serviceKey": "todo",
                "scopes": ["sync"],
                "status": "active",
            },
        )

    def test_server_fails_closed_when_allowed_key_ids_are_empty(self) -> None:
        authenticator = ServiceCredentialAuthenticator()
        with (
            patch.object(sync_auth_module, "SYNC_ALLOWED_KEY_IDS", ()),
            patch.object(sync_auth_module, "serves_sync_peer_api", return_value=True),
            patch.object(
                sync_auth_module,
                "request_json",
                side_effect=AssertionError("auth must not be called for invalid server config"),
            ),
        ):
            with self.assertRaises(SyncAuthUnavailable):
                authenticator.verify(self._headers("configured-client-key"))
        self.assertFalse(authenticator._cache)

    def test_other_valid_sync_key_is_rejected_and_not_cached(self) -> None:
        authenticator = ServiceCredentialAuthenticator()
        with (
            patch.object(
                sync_auth_module, "SYNC_ALLOWED_KEY_IDS", ("allowed-laptop-key",)
            ),
            patch.object(sync_auth_module, "serves_sync_peer_api", return_value=True),
            patch.object(
                sync_auth_module,
                "AUTH_SERVICE_KEY_ID",
                "todo-server-verifier",
            ),
            patch.object(
                sync_auth_module,
                "AUTH_SERVICE_SECRET",
                "todo-server-secret",
            ),
            patch.object(
                sync_auth_module,
                "request_json",
                side_effect=self._valid_auth_response,
            ) as auth_verify,
        ):
            with self.assertRaises(SyncAuthError) as caught:
                authenticator.verify(self._headers("other-valid-sync-key"))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.reason, "credential_not_allowed")
        self.assertEqual(auth_verify.call_count, 1)
        self.assertFalse(authenticator._cache)

    async def test_peer_dependency_rejects_other_auth_valid_key_end_to_end(self) -> None:
        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class _Connection:
            def cursor(self):
                return _Cursor()

        @contextmanager
        def fake_db_connection(*_args, **_kwargs):
            yield _Connection()

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/sync/handshake",
                "query_string": b"",
                "headers": [
                    (
                        SERVICE_CREDENTIAL_KEY_HEADER.encode(),
                        b"other-valid-sync-key",
                    ),
                    (
                        SERVICE_CREDENTIAL_SECRET_HEADER.encode(),
                        b"presented-secret",
                    ),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 20022),
                "scheme": "http",
            }
        )
        shared_authenticator = sync_auth_module.get_service_credential_authenticator()
        shared_authenticator.clear_cache()
        try:
            with (
                patch.object(
                    sync_router_module, "serves_sync_peer_api", return_value=True
                ),
                patch.object(
                    sync_router_module,
                    "get_db_connection",
                    side_effect=fake_db_connection,
                ),
                patch.object(
                    sync_router_module,
                    "resolve_account_id",
                    return_value="account-a",
                ),
                patch.object(
                    sync_auth_module,
                    "SYNC_ALLOWED_KEY_IDS",
                    ("allowed-laptop-key",),
                ),
                patch.object(
                    sync_auth_module, "serves_sync_peer_api", return_value=True
                ),
                patch.object(
                    sync_auth_module,
                    "AUTH_SERVICE_KEY_ID",
                    "todo-server-verifier",
                ),
                patch.object(
                    sync_auth_module,
                    "AUTH_SERVICE_SECRET",
                    "todo-server-secret",
                ),
                patch.object(
                    sync_auth_module,
                    "request_json",
                    side_effect=self._valid_auth_response,
                ),
            ):
                with self.assertRaises(HTTPException) as caught:
                    await sync_router_module.require_sync_principal(request)
        finally:
            shared_authenticator.clear_cache()
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail["code"], "credential_not_allowed")
        self.assertFalse(shared_authenticator._cache)


@unittest.skipUnless(REACHABLE, f"MySQL scratch DB unavailable ({DB_CONFIG['database']})")
class SyncSecurityDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        truncate_scratch_tables()
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                for project_id, owner_id in (
                    ("p-owned", "account-a"),
                    ("p-shared", "account-b"),
                    ("p-viewer", "account-b"),
                    ("p-other", "account-c"),
                ):
                    cursor.execute(
                        """
                        INSERT INTO projects
                            (id, owner_id, name, icon, status, is_secret,
                             created_at, updated_at, updated_at_utc)
                        VALUES (%s, %s, %s, 'Beer', 0, 0, %s, %s, %s)
                        """,
                        (project_id, owner_id, project_id, MOMENT, MOMENT, MOMENT),
                    )
                cursor.execute(
                    """
                    INSERT INTO project_members
                        (id, project_id, user_id, role, invited_at, updated_at_utc)
                    VALUES
                        ('pm-editor', 'p-shared', 'account-a', 'editor', %s, %s),
                        ('pm-viewer', 'p-viewer', 'account-a', 'viewer', %s, %s),
                        ('pm-deleted', 'p-other', 'account-a', 'editor', %s, %s),
                        ('pm-collab', 'p-owned', 'collaborator-x', 'editor', %s, %s)
                    """,
                    (
                        MOMENT,
                        MOMENT,
                        MOMENT,
                        MOMENT,
                        MOMENT,
                        MOMENT,
                        MOMENT,
                        MOMENT,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE project_members
                    SET deleted_at = %s
                    WHERE id = 'pm-deleted'
                    """,
                    (MOMENT,),
                )
                for memo_id, project_id, creator in (
                    ("m-owned", "p-owned", "collaborator-x"),
                    ("m-shared", "p-shared", "account-b"),
                    ("m-viewer", "p-viewer", "account-b"),
                    ("m-other", "p-other", "account-c"),
                ):
                    cursor.execute(
                        """
                        INSERT INTO memos
                            (id, project_id, created_by, title, content, status,
                             created_at, updated_at, updated_at_utc)
                        VALUES (%s, %s, %s, %s, '', 0, %s, %s, %s)
                        """,
                        (
                            memo_id,
                            project_id,
                            creator,
                            memo_id,
                            MOMENT,
                            MOMENT,
                            MOMENT,
                        ),
                    )

    def _memo_change(self, memo_id: str, project_id: str) -> dict:
        return {
            "table": "memos",
            "rowId": memo_id,
            "op": "update",
            "row": {
                "id": memo_id,
                "project_id": project_id,
                "title": memo_id,
                "updated_at_utc": "2026-07-29T05:01:00.000Z",
            },
            "baseUpdatedAtUtc": "2026-07-29T05:00:00.000Z",
        }

    def test_deleted_membership_grants_no_access(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self.assertFalse(
                    check_project_membership(cursor, "p-other", _user("account-a"))
                )
                self.assertIsNone(
                    get_project_role(cursor, "p-other", _user("account-a"))
                )

    def test_identity_uses_only_project_owner_not_collaborators_or_creators(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self.assertEqual(
                    distinct_owner_ids(cursor),
                    ["account-a", "account-b", "account-c"],
                )

        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM projects WHERE id <> 'p-owned'")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self.assertEqual(distinct_owner_ids(cursor), ["account-a"])

    def test_owner_and_editor_changes_are_authorized(self) -> None:
        changes = [
            self._memo_change("m-owned", "p-owned"),
            self._memo_change("m-shared", "p-shared"),
            {
                "table": "project_members",
                "rowId": "pm-new",
                "op": "insert",
                "row": {
                    "id": "pm-new",
                    "project_id": "p-owned",
                    "user_id": "collaborator",
                    "role": "viewer",
                    "updated_at_utc": "2026-07-29T05:01:00.000Z",
                },
            },
        ]
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                _authorize_sync_changes(cursor, changes, "account-a")

    def test_viewer_and_cross_account_changes_are_rejected(self) -> None:
        for change in (
            self._memo_change("m-viewer", "p-viewer"),
            self._memo_change("m-other", "p-other"),
        ):
            with self.subTest(change=change["rowId"]):
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        with self.assertRaises(HTTPException) as caught:
                            _authorize_sync_changes(cursor, [change], "account-a")
                self.assertEqual(caught.exception.status_code, 403)

    def test_cross_account_hard_and_soft_delete_payloads_are_rejected(self) -> None:
        hard_delete = {
            "table": "memos",
            "rowId": "m-other",
            "op": "delete",
            "row": None,
        }
        soft_delete = self._memo_change("m-other", "p-other")
        soft_delete["op"] = "delete"
        soft_delete["row"]["deleted_at"] = "2026-07-29T05:01:00.000Z"
        for change in (hard_delete, soft_delete):
            with self.subTest(op=change["op"], row=change["row"] is not None):
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        with self.assertRaises(HTTPException) as caught:
                            _authorize_sync_changes(cursor, [change], "account-a")
                self.assertEqual(caught.exception.status_code, 403)

    def test_cross_account_merge_rejects_either_loser_or_winner_before_mutation(self) -> None:
        for loser_id, winner_id in (
            ("m-other", "m-owned"),
            ("m-owned", "m-other"),
        ):
            with self.subTest(kind="memo", loser=loser_id, winner=winner_id):
                with self.assertRaises(HTTPException) as caught:
                    _execute_peer_merge(
                        "memo", loser_id, winner_id, "account-a"
                    )
                self.assertEqual(caught.exception.status_code, 403)

        for loser_id, winner_id in (
            ("p-other", "p-owned"),
            ("p-owned", "p-other"),
        ):
            with self.subTest(kind="project", loser=loser_id, winner=winner_id):
                with self.assertRaises(HTTPException) as caught:
                    _execute_peer_merge(
                        "project", loser_id, winner_id, "account-a"
                    )
                self.assertEqual(caught.exception.status_code, 403)

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT deleted_at FROM memos WHERE id IN ('m-owned', 'm-other')"
                )
                self.assertTrue(
                    all(row["deleted_at"] is None for row in cursor.fetchall())
                )
                cursor.execute(
                    "SELECT deleted_at FROM projects WHERE id IN ('p-owned', 'p-other')"
                )
                self.assertTrue(
                    all(row["deleted_at"] is None for row in cursor.fetchall())
                )

    def test_project_owner_cannot_reassign_ownership_through_sync(self) -> None:
        change = {
            "table": "projects",
            "rowId": "p-owned",
            "op": "update",
            "row": {
                "id": "p-owned",
                "owner_id": "account-c",
                "updated_at_utc": "2026-07-29T05:01:00.000Z",
            },
        }
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                with self.assertRaises(HTTPException):
                    _authorize_sync_changes(cursor, [change], "account-a")

    def test_lock_requires_owner_or_editor_role(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                _require_memo_write_access(cursor, "m-owned", "account-a")
                _require_memo_write_access(cursor, "m-shared", "account-a")
                with self.assertRaises(HTTPException) as viewer:
                    _require_memo_write_access(cursor, "m-viewer", "account-a")
                with self.assertRaises(HTTPException) as foreign:
                    _require_memo_write_access(cursor, "m-other", "account-a")
        self.assertEqual(viewer.exception.status_code, 403)
        self.assertEqual(foreign.exception.status_code, 403)

    def test_issue_listing_and_resolution_are_account_scoped(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                own_id = record_issue(
                    cursor, "conflict", ref_table="memos", ref_id="m-owned"
                )
                foreign_id = record_issue(
                    cursor, "conflict", ref_table="memos", ref_id="m-other"
                )
                global_id = record_issue(cursor, "schema", detail={"version": 1})

        admin = _user("account-a", "admin")
        owner = _user("account-a", "owner")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                admin_issues = _scoped_issues(cursor, admin)
                owner_issues = _scoped_issues(cursor, owner)
                allowed = _authorized_issue_ids(
                    cursor,
                    ResolveIssuesRequest(issueIds=[own_id, foreign_id, global_id]),
                    admin,
                )
        self.assertEqual({issue["id"] for issue in admin_issues}, {own_id})
        self.assertEqual(
            {issue["id"] for issue in owner_issues},
            {own_id, global_id},
        )
        self.assertEqual(allowed, [own_id])

    def test_global_controls_require_admin_permission(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            _ensure_sync_admin(_user("account-a", "user"))
        self.assertEqual(caught.exception.status_code, 403)
        _ensure_sync_admin(_user("account-a", "admin"))

    def test_startup_created_required_local_metadata_tables(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT TABLE_NAME
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME IN ('sync_row_state', 'sync_retry_queue')
                    """,
                    (DB_CONFIG["database"],),
                )
                tables = {row["TABLE_NAME"] for row in cursor.fetchall()}
        self.assertEqual(tables, {"sync_row_state", "sync_retry_queue"})


if __name__ == "__main__":
    unittest.main()
