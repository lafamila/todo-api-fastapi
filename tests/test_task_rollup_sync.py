"""Sync-v5 durable task-rollup receipt schema, privacy, and convergence tests."""

from __future__ import annotations

import json
import os
import unicodedata
import unittest
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import HTTPException

from tests.scratch_db import (
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

SCRATCH_DB = use_scratch_database("todo_sync_rollup_sync_test")

from src.connectors import DB_CONFIG, get_db_connection  # noqa: E402
from src.routers import sync as sync_router_module  # noqa: E402
from src.routers.sync import (  # noqa: E402
    SyncChangeInput,
    SyncPushRequest,
    _resolve_push_columns,
    sync_changes,
)
from src.services.sync_apply import SIDE_SERVER, apply_changes  # noqa: E402
from src.services import sync_daemon as sync_daemon_module  # noqa: E402
from src.services.sync_daemon import SyncBlocked, SyncDaemon  # noqa: E402
from src.services.sync_auth import SyncPrincipal  # noqa: E402
from src.services.sync_peer import SyncPeer  # noqa: E402
from src.services.sync_store import read_changes  # noqa: E402
from src.services.task_rollup_schema import (  # noqa: E402
    TaskRollupSchemaError,
    canonical_json_text,
    canonical_payload_hash,
    validate_completion_data_json,
)
from src.sync_schema import SCHEMA_VERSION, SYNC_TABLE_ORDER, SYNC_TABLES  # noqa: E402
from src.sync_cli import _bootstrap_blockers  # noqa: E402
from src.timeutil import iso_utc  # noqa: E402


ACTOR = "rollup-actor"
OTHER = "rollup-other"
OWNER = "rollup-service-owner"
PROJECT = "rollup-project"
MEMO = "rollup-memo"
OPERATION = "12345678-1234-4234-8234-123456789abc"
UNDO_OPERATION = "22345678-1234-4234-8234-123456789abc"
BASE = datetime(2026, 11, 24, 3, 0, 0, 123000)
NODE_A_DB = "todo_sync_rollup_node_a_test"
NODE_B_DB = "todo_sync_rollup_node_b_test"


@contextmanager
def _selected_database(name: str):
    previous_database = DB_CONFIG["database"]
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


def _occurrence(text: str = "Cafe\u0301") -> dict:
    occurrence = {
        "key": "",
        "memoId": MEMO,
        "memoTitle": "Tasks",
        "revision": "r" * 64,
        "lineIndex": 0,
        "byteStart": 0,
        "marker": "-- ",
        "checked": False,
        "text": text,
        "physicalPath": [text],
    }
    occurrence["key"] = canonical_payload_hash(
        [
            occurrence["memoId"],
            occurrence["revision"],
            occurrence["lineIndex"],
            occurrence["byteStart"],
            occurrence["marker"],
            occurrence["text"],
            occurrence["physicalPath"],
        ]
    )
    return occurrence


def _manifest(moment: datetime = BASE) -> list[dict]:
    return [
        {
            "memoId": MEMO,
            "revision": "s" * 64,
            "contentHash": "c" * 64,
            "updatedAtUtc": iso_utc(moment),
        }
    ]


def _result(*, status: str = "ready", can_undo: bool = True, text: str = "Cafe\u0301") -> dict:
    occurrence = _occurrence(text)
    return {
        "operationId": OPERATION,
        "projectId": PROJECT,
        "groupKey": "g" * 64,
        "group": {
            "key": "g" * 64,
            "kind": "task",
            "text": text,
            "checked": status == "undone",
            "actionable": status != "undone",
            "occurrences": [deepcopy(occurrence)],
            "children": [],
        },
        "changedMemoIds": [MEMO],
        "afterManifest": _manifest(BASE + (timedelta(seconds=1) if status == "undone" else timedelta(0))),
        "completedAtUtc": iso_utc(BASE),
        "expiresAtUtc": iso_utc(BASE + timedelta(hours=12)),
        "status": status,
        "canUndo": can_undo,
    }


def _completion_envelope(text: str = "Cafe\u0301") -> dict:
    occurrence = _occurrence(text)
    return {
        "result": _result(text=text),
        "matchedOccurrences": [deepcopy(occurrence)],
        "changedOccurrences": [deepcopy(occurrence)],
        "beforeRevisions": {MEMO: "b" * 64},
        "afterManifest": _manifest(),
    }


def _undo_envelope(memo_scope: str | None = None) -> dict:
    result = deepcopy(_completion_envelope()["result"])
    if memo_scope is not None:
        result["memoScope"] = memo_scope
    result["status"] = "undone"
    result["canUndo"] = False
    result["afterManifest"] = _manifest(BASE + timedelta(seconds=1))
    return {"result": result, "afterManifest": deepcopy(result["afterManifest"])}


def _wire_row(
    moment: datetime = BASE,
    *,
    actor: str = ACTOR,
    memo_scope: str | None = None,
) -> dict:
    envelope = _completion_envelope()
    if memo_scope is not None:
        envelope["result"]["memoScope"] = memo_scope
    return {
        "id": OPERATION,
        "project_id": PROJECT,
        "actor_id": actor,
        "group_key": "g" * 64,
        "payload_hash": "a" * 64,
        "data_json": canonical_json_text(envelope),
        "completed_at_utc": iso_utc(BASE),
        "completion_date_kst": "2026-11-24",
        "undo_operation_id": None,
        "undo_payload_hash": None,
        "undo_data_json": None,
        "updated_at_utc": iso_utc(moment),
        "deleted_at": None,
    }


class TaskRollupPayloadUnitTests(unittest.TestCase):
    def test_schema_v5_declares_exact_completion_columns_last(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 5)
        self.assertEqual(SYNC_TABLE_ORDER[-1], "task_rollup_completions")
        self.assertEqual(
            SYNC_TABLES["task_rollup_completions"]["columns"],
            (
                "id", "project_id", "actor_id", "group_key", "payload_hash",
                "data_json", "completed_at_utc", "completion_date_kst",
                "undo_operation_id", "undo_payload_hash", "undo_data_json",
                "updated_at_utc", "deleted_at",
            ),
        )

    def test_v4_peer_is_fail_closed_for_push_pull_and_bootstrap(self) -> None:
        with self.assertRaises(HTTPException) as push:
            _resolve_push_columns(
                SyncPushRequest(
                    clientId="peer:123e4567-e89b-42d3-a456-426614174000",
                    schemaVersion=4,
                    allowSchemaDrift=True,
                    tables={name: list(spec["columns"]) for name, spec in SYNC_TABLES.items()},
                    changes=[SyncChangeInput(table="memos", rowId="memo")],
                )
            )
        self.assertEqual(getattr(push.exception, "status_code", None), 409)

        original = sync_daemon_module.SYNC_ALLOW_SCHEMA_DRIFT
        sync_daemon_module.SYNC_ALLOW_SCHEMA_DRIFT = True
        try:
            with self.assertRaises(SyncBlocked):
                SyncDaemon()._check_schema(
                    {"schemaVersion": 4, "tables": {name: list(spec["columns"]) for name, spec in SYNC_TABLES.items()}}
                )
        finally:
            sync_daemon_module.SYNC_ALLOW_SCHEMA_DRIFT = original

        blockers = _bootstrap_blockers(
            handshake={
                "schemaVersion": 4,
                "tables": {name: list(spec["columns"]) for name, spec in SYNC_TABLES.items()},
                "accountId": ACTOR,
                "identity": {"accountId": ACTOR},
                "permission": "owner",
                "subjectKind": "service",
                "ownerIds": [ACTOR],
                "maxSeq": 0,
            },
            peer_error=None,
            owner_ids=[ACTOR],
            identity_cache={"account_id": ACTOR},
            local_counts={},
            target_fingerprint={
                "columns": {name: list(spec["columns"]) for name, spec in SYNC_TABLES.items()},
                "ownerIds": [ACTOR],
                "maxSeq": 0,
                "serverIdentity": {"database": "remote"},
                "error": None,
            },
            local_server_identity={"database": "local"},
            rollup_actor_ids=[ACTOR],
        )
        self.assertTrue(any("schemaVersion" in blocker for blocker in blockers))

    def test_sync_peer_declares_v5_on_change_feed_request(self) -> None:
        peer = SyncPeer("https://peer.example", "key", "secret")
        with patch.object(peer, "_call", return_value={}) as call:
            peer.changes(7, 25)
        method, path = call.call_args.args
        self.assertEqual(method, "GET")
        self.assertIn(f"schemaVersion={SCHEMA_VERSION}", path)
        self.assertIn("since=7", path)
        self.assertIn("limit=25", path)


class PullSchemaFeedGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_old_and_ahead_versions_reject_before_database_read(self) -> None:
        principal = SyncPrincipal(
            account_id=ACTOR,
            permission="owner",
            subject_kind="service_credential",
            subject_id="credential",
        )
        for version, expected_code in (
            (None, "sync_schema_version_required"),
            (4, "sync_private_media_schema_upgrade_required"),
            (SCHEMA_VERSION + 1, "sync_client_schema_ahead"),
        ):
            with self.subTest(version=version):
                with patch.object(
                    sync_router_module,
                    "get_db_connection",
                    side_effect=AssertionError("database must not be read"),
                ):
                    with self.assertRaises(HTTPException) as caught:
                        await sync_changes(
                            since=0,
                            limit=100,
                            schema_version=version,
                            principal=principal,
                        )
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(caught.exception.detail["code"], expected_code)

    def test_canonical_json_preserves_opaque_nfd_text(self) -> None:
        nfd = unicodedata.normalize("NFD", "Café")
        nfc = unicodedata.normalize("NFC", nfd)
        nfd_json = canonical_json_text(_completion_envelope(nfd))
        nfc_json = canonical_json_text(_completion_envelope(nfc))
        self.assertIn(nfd, nfd_json)
        self.assertNotEqual(nfd_json, nfc_json)
        self.assertNotEqual(canonical_payload_hash(json.loads(nfd_json)), canonical_payload_hash(json.loads(nfc_json)))
        self.assertEqual(validate_completion_data_json(nfd_json)["result"]["group"]["text"], nfd)

    def test_noncanonical_or_runtime_secret_payload_is_rejected(self) -> None:
        envelope = _completion_envelope()
        with self.assertRaisesRegex(TaskRollupSchemaError, "canonical"):
            validate_completion_data_json(json.dumps(envelope, ensure_ascii=False))
        envelope["result"]["group"]["token"] = "must-not-persist"
        with self.assertRaises(TaskRollupSchemaError):
            validate_completion_data_json(canonical_json_text(envelope))

    def test_oversized_and_excessively_deep_json_fail_safely(self) -> None:
        oversized = canonical_json_text(_completion_envelope("x" * (1024 * 1024)))
        with self.assertRaises(TaskRollupSchemaError):
            validate_completion_data_json(oversized)

        nested: object = "leaf"
        for _ in range(300):
            nested = [nested]
        with self.assertRaisesRegex(TaskRollupSchemaError, "deeply nested"):
            validate_completion_data_json(canonical_json_text({"x": nested}))

    def test_cross_field_receipt_forgery_is_rejected(self) -> None:
        envelope = _completion_envelope()
        forged = deepcopy(envelope["matchedOccurrences"][0])
        forged["memoId"] = "forged-memo"
        forged["key"] = canonical_payload_hash(
            [
                forged["memoId"], forged["revision"], forged["lineIndex"],
                forged["byteStart"], forged["marker"], forged["text"],
                forged["physicalPath"],
            ]
        )
        envelope["matchedOccurrences"] = [deepcopy(forged)]
        envelope["changedOccurrences"] = [deepcopy(forged)]
        envelope["result"]["group"]["occurrences"] = [deepcopy(forged)]
        with self.assertRaisesRegex(TaskRollupSchemaError, "same memos"):
            validate_completion_data_json(canonical_json_text(envelope))

        bad_key = _completion_envelope()
        bad_key["matchedOccurrences"][0]["key"] = "0" * 64
        bad_key["changedOccurrences"][0]["key"] = "0" * 64
        bad_key["result"]["group"]["occurrences"][0]["key"] = "0" * 64
        with self.assertRaisesRegex(TaskRollupSchemaError, "key does not match"):
            validate_completion_data_json(canonical_json_text(bad_key))

        prechecked_changed = _completion_envelope()
        for occurrence in (
            prechecked_changed["matchedOccurrences"][0],
            prechecked_changed["changedOccurrences"][0],
            prechecked_changed["result"]["group"]["occurrences"][0],
        ):
            occurrence["marker"] = "v-- "
            occurrence["checked"] = True
            occurrence["key"] = canonical_payload_hash(
                [
                    occurrence["memoId"], occurrence["revision"],
                    occurrence["lineIndex"], occurrence["byteStart"],
                    occurrence["marker"], occurrence["text"],
                    occurrence["physicalPath"],
                ]
            )
        with self.assertRaisesRegex(TaskRollupSchemaError, "unchecked"):
            validate_completion_data_json(canonical_json_text(prechecked_changed))

    def test_optional_context_is_bounded_and_exact(self) -> None:
        envelope = _completion_envelope()
        envelope["result"]["context"] = [
            {
                "key": "c" * 64,
                "kind": "description",
                "text": "Parent context",
                "checked": False,
            }
        ]
        validated = validate_completion_data_json(canonical_json_text(envelope))
        self.assertEqual(validated["result"]["context"], envelope["result"]["context"])

        envelope["result"]["context"] *= 129
        with self.assertRaisesRegex(TaskRollupSchemaError, "context"):
            validate_completion_data_json(canonical_json_text(envelope))

    def test_optional_memo_scope_is_strict_and_legacy_absence_remains_valid(self) -> None:
        legacy = _completion_envelope()
        self.assertNotIn(
            "memoScope",
            validate_completion_data_json(canonical_json_text(legacy))["result"],
        )
        for scope in ("all", "recent30"):
            envelope = _completion_envelope()
            envelope["result"]["memoScope"] = scope
            validated = validate_completion_data_json(canonical_json_text(envelope))
            self.assertEqual(validated["result"]["memoScope"], scope)
        invalid = _completion_envelope()
        invalid["result"]["memoScope"] = "recent90"
        with self.assertRaisesRegex(TaskRollupSchemaError, "memoScope"):
            validate_completion_data_json(canonical_json_text(invalid))


REACHABLE = init_scratch_database()


@unittest.skipUnless(REACHABLE, f"MySQL scratch DB unavailable ({DB_CONFIG['database']})")
class TaskRollupSyncDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        truncate_scratch_tables()
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, owner_id, name, icon, status, is_secret,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, 'Rollup', 'Beer', 0, 0, %s, %s, %s)
                    """,
                    (PROJECT, OWNER, BASE, BASE, BASE),
                )
                for actor, role in ((ACTOR, "editor"), (OTHER, "viewer")):
                    cursor.execute(
                        """
                        INSERT INTO project_members
                            (id, project_id, user_id, role, invited_at, updated_at_utc)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (f"pm-{actor}", PROJECT, actor, role, BASE, BASE),
                    )
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, %s, 'memo', 'Tasks', '-- Cafe', 0, %s, %s, %s)
                    """,
                    (MEMO, PROJECT, ACTOR, BASE, BASE, BASE),
                )

    def _apply(self, row: dict, actor: str = ACTOR):
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                return apply_changes(
                    cursor,
                    [{"seq": 1, "table": "task_rollup_completions", "rowId": OPERATION, "op": "update", "row": row}],
                    SIDE_SERVER,
                    expected_actor_id=actor,
                )

    def _stored(self) -> dict:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM task_rollup_completions WHERE id = %s", (OPERATION,))
                return cursor.fetchone()

    def test_ddl_has_corpus_index_receipt_indexes_and_triggers(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT TABLE_NAME, INDEX_NAME FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME IN ('memos', 'task_rollup_completions')
                    """
                )
                indexes = {(row["TABLE_NAME"], row["INDEX_NAME"]) for row in cursor.fetchall()}
                self.assertIn(("memos", "idx_memos_rollup_corpus"), indexes)
                self.assertIn(("task_rollup_completions", "idx_task_rollup_actor_project_date"), indexes)
                cursor.execute(
                    """
                    SELECT TRIGGER_NAME FROM information_schema.TRIGGERS
                    WHERE TRIGGER_SCHEMA = DATABASE()
                      AND EVENT_OBJECT_TABLE = 'task_rollup_completions'
                    """
                )
                self.assertEqual(len(cursor.fetchall()), 3)

    def test_feed_is_exact_actor_private_even_from_project_owner(self) -> None:
        row = _wire_row(memo_scope="recent30")
        outcome = self._apply(row)
        self.assertEqual(outcome.applied, 1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Inbound apply intentionally suppresses the outbox. Simulate the
                # originating node's normal API write so the feed has a receipt seq.
                cursor.execute(
                    "UPDATE task_rollup_completions "
                    "SET updated_at_utc = updated_at_utc WHERE id = %s",
                    (OPERATION,),
                )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                own, _ = read_changes(cursor, 0, 100, ACTOR)
                other, _ = read_changes(cursor, 0, 100, OTHER)
                owner, _ = read_changes(cursor, 0, 100, OWNER)
        self.assertIn(OPERATION, {change["rowId"] for change in own})
        own_receipt = next(
            change for change in own if change["rowId"] == OPERATION
        )
        self.assertEqual(
            json.loads(own_receipt["row"]["data_json"])["result"]["memoScope"],
            "recent30",
        )
        self.assertNotIn(OPERATION, {change["rowId"] for change in other})
        self.assertNotIn(OPERATION, {change["rowId"] for change in owner})

    def test_apply_rejects_actor_rebind_and_hard_delete(self) -> None:
        self.assertEqual(self._apply(_wire_row()).applied, 1)
        rebound = _wire_row(BASE + timedelta(seconds=2), actor=OTHER)
        rejected = self._apply(rebound, actor=OTHER)
        self.assertEqual(rejected.applied, 0)
        self.assertEqual(len(rejected.rejected), 1)
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                hard = apply_changes(
                    cursor,
                    [{"table": "task_rollup_completions", "rowId": OPERATION, "op": "delete", "row": None}],
                    SIDE_SERVER,
                    expected_actor_id=ACTOR,
                )
        self.assertEqual(hard.applied, 0)
        self.assertEqual(hard.rejected[0]["reason"], "task_rollup_hard_delete_forbidden")
        self.assertEqual(self._stored()["actor_id"], ACTOR)

    def test_raw_sync_rejects_oversized_metadata_without_inserting(self) -> None:
        oversized = _wire_row()
        oversized["data_json"] = canonical_json_text(
            _completion_envelope("x" * (1024 * 1024))
        )
        outcome = self._apply(oversized)
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(len(outcome.rejected), 1)
        self.assertIn("size limit", outcome.rejected[0]["reason"])
        self.assertIsNone(self._stored())

    def test_new_receipt_defers_until_source_memo_arrives(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM memos WHERE id = %s", (MEMO,))
        deferred = self._apply(_wire_row())
        self.assertEqual(deferred.applied, 0)
        self.assertEqual(len(deferred.deferred), 1)
        self.assertEqual(
            deferred.deferred[0]["reason"],
            f"missing_parent:memos:{MEMO}",
        )
        self.assertIsNone(self._stored())

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, %s, 'memo', 'Tasks', '-- Cafe', 0, %s, %s, %s)
                    """,
                    (MEMO, PROJECT, ACTOR, BASE, BASE, BASE),
                )
        applied = self._apply(_wire_row())
        self.assertEqual(applied.applied, 1)

    def test_new_receipt_requires_later_clock_for_moved_source_history(self) -> None:
        foreign_project = "foreign-project"
        foreign_memo = "foreign-memo"
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, owner_id, name, icon, status, is_secret,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, 'Foreign', 'Beer', 0, 0, %s, %s, %s)
                    """,
                    (foreign_project, OWNER, BASE, BASE, BASE),
                )
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, %s, 'memo', 'Foreign', '-- forged', 0, %s, %s, %s)
                    """,
                    (foreign_memo, foreign_project, ACTOR, BASE, BASE, BASE),
                )
        envelope = _completion_envelope()
        for occurrence in (
            envelope["matchedOccurrences"][0],
            envelope["changedOccurrences"][0],
            envelope["result"]["group"]["occurrences"][0],
        ):
            occurrence["memoId"] = foreign_memo
            occurrence["key"] = canonical_payload_hash(
                [
                    occurrence["memoId"], occurrence["revision"],
                    occurrence["lineIndex"], occurrence["byteStart"],
                    occurrence["marker"], occurrence["text"],
                    occurrence["physicalPath"],
                ]
            )
        envelope["result"]["changedMemoIds"] = [foreign_memo]
        envelope["beforeRevisions"] = {foreign_memo: "b" * 64}
        envelope["afterManifest"][0]["memoId"] = foreign_memo
        envelope["result"]["afterManifest"][0]["memoId"] = foreign_memo
        forged = _wire_row()
        forged["data_json"] = canonical_json_text(envelope)
        outcome = self._apply(forged)
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(len(outcome.rejected), 1)
        self.assertIn("later causal clock", outcome.rejected[0]["reason"])
        self.assertIsNone(self._stored())

        moved_later = BASE + timedelta(seconds=1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE memos SET updated_at_utc = %s WHERE id = %s",
                    (moved_later, foreign_memo),
                )
        historical = self._apply(forged)
        self.assertEqual(historical.applied, 1)

    def test_new_receipt_requires_later_clock_for_deleted_source_history(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE memos SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
                    (BASE, BASE, MEMO),
                )
        unproven = self._apply(_wire_row())
        self.assertEqual(unproven.applied, 0)
        self.assertIn("later causal clock", unproven.rejected[0]["reason"])

        deleted_later = BASE + timedelta(seconds=1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE memos SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
                    (deleted_later, deleted_later, MEMO),
                )
        historical = self._apply(_wire_row())
        self.assertEqual(historical.applied, 1)

    def test_receipt_rejects_completed_time_mismatch(self) -> None:
        envelope = _completion_envelope()
        envelope["result"]["completedAtUtc"] = iso_utc(BASE + timedelta(seconds=1))
        row = _wire_row()
        row["data_json"] = canonical_json_text(envelope)
        outcome = self._apply(row)
        self.assertEqual(outcome.applied, 0)
        self.assertIn("completedAtUtc", outcome.rejected[0]["reason"])

    def test_undo_and_tombstone_are_monotonic_across_newer_lww_snapshot(self) -> None:
        self.assertEqual(self._apply(_wire_row()).applied, 1)
        newer_complete = _wire_row(BASE + timedelta(seconds=5))
        self.assertEqual(self._apply(newer_complete).applied, 1)
        undone = _wire_row(BASE + timedelta(seconds=1))
        undone.update(
            {
                "undo_operation_id": UNDO_OPERATION,
                "undo_payload_hash": "d" * 64,
                "undo_data_json": canonical_json_text(_undo_envelope()),
            }
        )
        self.assertEqual(self._apply(undone).applied, 1)
        self.assertEqual(self._stored()["updated_at_utc"], BASE + timedelta(seconds=5))

        stale_complete_with_newer_clock = _wire_row(BASE + timedelta(seconds=6))
        self.assertEqual(self._apply(stale_complete_with_newer_clock).applied, 1)
        stored = self._stored()
        self.assertEqual(stored["undo_operation_id"], UNDO_OPERATION)
        self.assertIsNotNone(stored["undo_data_json"])

        tombstone = deepcopy(stale_complete_with_newer_clock)
        tombstone["updated_at_utc"] = iso_utc(BASE + timedelta(seconds=3))
        tombstone["deleted_at"] = iso_utc(BASE + timedelta(seconds=3))
        self.assertEqual(self._apply(tombstone).applied, 1)
        resurrection = _wire_row(BASE + timedelta(seconds=7))
        self.assertEqual(self._apply(resurrection).applied, 1)
        self.assertIsNotNone(self._stored()["deleted_at"])

    def test_raw_sync_rejects_forged_undo_result_snapshot_fields(self) -> None:
        self.assertEqual(self._apply(_wire_row()).applied, 1)

        forged_envelopes: list[tuple[str, dict]] = []
        group_forgery = _undo_envelope()
        group_forgery["result"]["group"]["text"] = "forged group"
        forged_envelopes.append(("group", group_forgery))

        changed_ids_forgery = _undo_envelope()
        changed_ids_forgery["result"]["changedMemoIds"] = ["forged-memo"]
        changed_ids_forgery["result"]["afterManifest"][0]["memoId"] = "forged-memo"
        changed_ids_forgery["afterManifest"][0]["memoId"] = "forged-memo"
        forged_envelopes.append(("changedMemoIds", changed_ids_forgery))

        completed_forgery = _undo_envelope()
        completed_forgery["result"]["completedAtUtc"] = iso_utc(
            BASE + timedelta(seconds=5)
        )
        forged_envelopes.append(("completedAtUtc", completed_forgery))

        expires_forgery = _undo_envelope()
        expires_forgery["result"]["expiresAtUtc"] = iso_utc(
            BASE + timedelta(days=2)
        )
        forged_envelopes.append(("expiresAtUtc", expires_forgery))

        can_undo_forgery = _undo_envelope()
        can_undo_forgery["result"]["canUndo"] = True
        forged_envelopes.append(("canUndo", can_undo_forgery))

        scope_forgery = _undo_envelope()
        scope_forgery["result"]["memoScope"] = "recent30"
        forged_envelopes.append(("memoScope", scope_forgery))

        for expected_field, envelope in forged_envelopes:
            row = _wire_row(BASE + timedelta(seconds=1))
            row.update(
                {
                    "undo_operation_id": UNDO_OPERATION,
                    "undo_payload_hash": "d" * 64,
                    "undo_data_json": canonical_json_text(envelope),
                }
            )
            outcome = self._apply(row)
            self.assertEqual(outcome.applied, 0, expected_field)
            self.assertEqual(len(outcome.rejected), 1, expected_field)
            self.assertIn(expected_field, outcome.rejected[0]["reason"])
            self.assertIsNone(self._stored()["undo_operation_id"])

    def test_undo_preserves_recent30_memo_scope(self) -> None:
        self.assertEqual(
            self._apply(_wire_row(memo_scope="recent30")).applied,
            1,
        )
        row = _wire_row(BASE + timedelta(seconds=1), memo_scope="recent30")
        row.update(
            {
                "undo_operation_id": UNDO_OPERATION,
                "undo_payload_hash": "d" * 64,
                "undo_data_json": canonical_json_text(
                    _undo_envelope(memo_scope="recent30")
                ),
            }
        )
        self.assertEqual(self._apply(row).applied, 1)
        stored = self._stored()
        self.assertEqual(
            json.loads(stored["undo_data_json"])["result"]["memoScope"],
            "recent30",
        )


class TaskRollupTwoNodeTests(unittest.TestCase):
    reachable = False

    @classmethod
    def setUpClass(cls) -> None:
        cls.reachable = True
        for database in (NODE_A_DB, NODE_B_DB):
            with _selected_database(database):
                if not init_scratch_database():
                    cls.reachable = False
                    break

    def setUp(self) -> None:
        if not self.reachable:
            self.skipTest("two-node task-rollup scratch databases are unavailable")
        for database in (NODE_A_DB, NODE_B_DB):
            with _selected_database(database):
                truncate_scratch_tables()
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO projects
                                (id, owner_id, name, icon, status, is_secret,
                                 created_at, updated_at, updated_at_utc)
                            VALUES (%s, %s, 'Rollup', 'Beer', 0, 0, %s, %s, %s)
                            """,
                            (PROJECT, OWNER, BASE, BASE, BASE),
                        )
                        cursor.execute(
                            """
                            INSERT INTO project_members
                                (id, project_id, user_id, role, invited_at, updated_at_utc)
                            VALUES (%s, %s, %s, 'editor', %s, %s)
                            """,
                            (f"pm-{ACTOR}", PROJECT, ACTOR, BASE, BASE),
                        )
                        cursor.execute(
                            """
                            INSERT INTO memos
                                (id, project_id, created_by, kind, title, content, status,
                                 created_at, updated_at, updated_at_utc)
                            VALUES (%s, %s, %s, 'memo', 'Tasks', '-- Cafe', 0, %s, %s, %s)
                            """,
                            (MEMO, PROJECT, ACTOR, BASE, BASE, BASE),
                        )

    def test_receipt_unicode_privacy_tombstone_and_reverse_stale_converge(self) -> None:
        nfd = unicodedata.normalize("NFD", "Café")
        source = _wire_row()
        source["data_json"] = canonical_json_text(_completion_envelope(nfd))
        with _selected_database(NODE_A_DB):
            with get_db_connection(sync_applying=True) as conn:
                with conn.cursor() as cursor:
                    outcome = apply_changes(
                        cursor,
                        [{"seq": 1, "table": "task_rollup_completions", "rowId": OPERATION, "op": "insert", "row": source}],
                        SIDE_SERVER,
                        expected_actor_id=ACTOR,
                    )
                    self.assertEqual(outcome.applied, 1)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE task_rollup_completions SET updated_at_utc = updated_at_utc WHERE id = %s",
                        (OPERATION,),
                    )
                    own_feed, first_cursor = read_changes(cursor, 0, 100, ACTOR)
                    owner_feed, _ = read_changes(cursor, 0, 100, OWNER)
        completion_change = next(
            change for change in own_feed if change["table"] == "task_rollup_completions"
        )
        self.assertNotIn(
            OPERATION,
            {change["rowId"] for change in owner_feed},
        )

        with _selected_database(NODE_B_DB):
            with get_db_connection(sync_applying=True) as conn:
                with conn.cursor() as cursor:
                    applied = apply_changes(
                        cursor,
                        [completion_change],
                        SIDE_SERVER,
                        expected_actor_id=ACTOR,
                    )
                    self.assertEqual(applied.applied, 1)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT data_json FROM task_rollup_completions WHERE id = %s",
                        (OPERATION,),
                    )
                    self.assertEqual(cursor.fetchone()["data_json"], source["data_json"])

        deleted_at = BASE + timedelta(seconds=2)
        with _selected_database(NODE_A_DB):
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE task_rollup_completions
                        SET deleted_at = %s, updated_at_utc = %s
                        WHERE id = %s
                        """,
                        (deleted_at, deleted_at, OPERATION),
                    )
                    delete_feed, _ = read_changes(cursor, first_cursor, 100, ACTOR)
        delete_change = next(
            change for change in delete_feed if change["table"] == "task_rollup_completions"
        )
        with _selected_database(NODE_B_DB):
            with get_db_connection(sync_applying=True) as conn:
                with conn.cursor() as cursor:
                    applied = apply_changes(
                        cursor,
                        [delete_change],
                        SIDE_SERVER,
                        expected_actor_id=ACTOR,
                    )
                    self.assertEqual(applied.applied, 1)
                    stale_live = _wire_row(BASE + timedelta(seconds=10))
                    reverse = apply_changes(
                        cursor,
                        [{"seq": 99, "table": "task_rollup_completions", "rowId": OPERATION, "op": "update", "row": stale_live}],
                        SIDE_SERVER,
                        expected_actor_id=ACTOR,
                    )
                    self.assertEqual(reverse.applied, 1)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT deleted_at FROM task_rollup_completions WHERE id = %s",
                        (OPERATION,),
                    )
                    self.assertIsNotNone(cursor.fetchone()["deleted_at"])


if __name__ == "__main__":
    unittest.main()
