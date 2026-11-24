from __future__ import annotations

import asyncio
import copy
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch

from pydantic import ValidationError
from fastapi import HTTPException
from starlette.requests import Request

from src.models.task_rollup import TaskRollupCompleteRequest
from src.routers import task_rollup as router
from src.services import task_rollup_store as store
from src.services.task_rollup import RollupValidationError
from tests.scratch_db import (
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)
from src.connectors import get_db_connection


UUID_1 = "00000000-0000-4000-8000-000000000001"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def complete_request(*, token: str = "secret", occurrence_keys=None, aliases=None):
    return {
        "operationId": UUID_1,
        "corpusDigest": DIGEST_A,
        "groupKey": DIGEST_B,
        "aliases": aliases or [],
        "occurrenceKeys": occurrence_keys or [DIGEST_C],
        "leases": [{"memoId": "memo-1", "token": token}],
    }


class TaskRollupDtoAndHashTests(unittest.TestCase):
    def test_complete_dto_rejects_non_uuid_and_duplicate_occurrences(self) -> None:
        with self.assertRaises(ValidationError):
            TaskRollupCompleteRequest.model_validate(
                {**complete_request(), "operationId": "not-a-uuid"}
            )
        with self.assertRaises(ValidationError):
            TaskRollupCompleteRequest.model_validate(
                complete_request(occurrence_keys=[DIGEST_C, DIGEST_C])
            )

    def test_semantic_hash_excludes_tokens_and_canonicalizes_sets(self) -> None:
        first = complete_request(
            token="first-secret",
            occurrence_keys=[DIGEST_B, DIGEST_C],
            aliases=[{"seedKey": DIGEST_A, "variantKeys": [DIGEST_C, DIGEST_B]}],
        )
        second = complete_request(
            token="rotated-secret",
            occurrence_keys=[DIGEST_C, DIGEST_B],
            aliases=[{"seedKey": DIGEST_A, "variantKeys": [DIGEST_B, DIGEST_C]}],
        )

        self.assertEqual(
            store.completion_payload_hash("project-1", first),
            store.completion_payload_hash("project-1", second),
        )
        self.assertNotEqual(
            store.completion_payload_hash("project-1", first),
            store.completion_payload_hash("project-2", first),
        )
        scoped = {**first, "memoScope": "recent30"}
        self.assertNotEqual(
            store.completion_payload_hash("project-1", first),
            store.completion_payload_hash("project-1", scoped),
        )
        self.assertNotIn("secret", store._canonical_json({
            "payloadHash": store.completion_payload_hash("project-1", first)
        }))

    def test_rollup_clock_exposes_kst_midnight_boundary(self) -> None:
        before = datetime(2026, 11, 24, 14, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 11, 24, 15, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(
            store._next_kst_boundary(before).isoformat(),
            "2026-11-24T15:00:00+00:00",
        )
        self.assertEqual(
            store._next_kst_boundary(after).isoformat(),
            "2026-11-25T15:00:00+00:00",
        )

    def test_parser_depth_limit_maps_to_whole_preview_413(self) -> None:
        error = RollupValidationError(
            "task rollup tree exceeds the maximum depth",
            code="treeDepthExceeded",
            status_code=413,
        )
        with patch.object(store, "build_rollup", side_effect=error):
            with self.assertRaises(HTTPException) as raised:
                store._build([], [])

        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(raised.exception.detail["code"], "treeDepthExceeded")


class _Reservation:
    def __init__(self) -> None:
        self.released = False
        self.valid_at_call = False

    def validate(self) -> bool:
        return not self.released

    async def release(self) -> None:
        self.released = True


class ReservedWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reservation_releases_only_after_worker_terminal(self) -> None:
        reservation = _Reservation()

        def worker(validate, final_validate):
            reservation.valid_at_call = validate()
            self.assertTrue(final_validate())
            return "committed"

        result = await router._run_reserved(reservation, worker)

        self.assertEqual(result, "committed")
        self.assertTrue(reservation.valid_at_call)
        self.assertTrue(reservation.released)

    async def test_postcommit_refresh_revalidates_rooms_and_invalidates_project(self) -> None:
        class Realtime:
            available = True

            def __init__(self) -> None:
                self.calls = []

            async def revalidate_memo_access(self, memo_id):
                self.calls.append(("revalidate", memo_id))

            async def emit_memo_pulled(self, memo_id, content, title, updated_at):
                self.calls.append(("memo", memo_id, content, title, updated_at))

            async def emit_projects_changed(self):
                self.calls.append(("projects",))

        realtime = Realtime()
        changed = [{
            "memoId": "memo-1",
            "content": "v-- done",
            "title": "Memo",
            "updatedAtUtc": "2026-11-24T01:00:00.000Z",
        }]
        with (
            patch.object(router, "get_realtime_server", return_value=realtime),
            patch.object(router, "load_changed_memos", return_value=changed),
        ):
            await router._notify_project_changed(
                "project-1", {"id": "user-1"}, {"changedMemoIds": ["memo-1"]}
            )

        self.assertEqual(
            realtime.calls,
            [
                ("revalidate", "memo-1"),
                ("memo", "memo-1", "v-- done", "Memo", "2026-11-24T01:00:00.000Z"),
                ("projects",),
            ],
        )

    async def test_cancellation_waits_for_sql_terminal_before_release(self) -> None:
        reservation = _Reservation()
        entered = asyncio.Event()
        allow_finish = asyncio.Event()

        def worker(validate, final_validate):
            entered_loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(allow_finish.wait(), entered_loop).result()
            reservation.valid_at_call = validate()
            self.assertTrue(final_validate())
            return "committed"

        entered_loop = asyncio.get_running_loop()
        task = asyncio.create_task(router._run_reserved(reservation, worker))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(reservation.released)
        allow_finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(reservation.valid_at_call)
        self.assertTrue(reservation.released)


class ManifestStateTests(unittest.TestCase):
    def test_equal_clock_with_different_content_is_conflict(self) -> None:
        clock = datetime(2026, 11, 24, 1, 2, 3)
        memo = {
            "id": "memo-1",
            "content": "different",
            "updated_at_utc": clock,
        }
        expected = [{
            "memoId": "memo-1",
            "revision": "wrong",
            "contentHash": "0" * 64,
            "updatedAtUtc": "2026-11-24T01:02:03.000Z",
        }]

        self.assertEqual(
            store._manifest_state(expected, {"memo-1": memo}), "conflict"
        )

    def test_nfd_to_nfc_wire_normalization_is_ready_but_not_undo_exact(self) -> None:
        clock = datetime(2026, 11, 24, 1, 2, 3)
        original = {
            "id": "memo-1",
            "content": "-- cafe\u0301",
            "updated_at_utc": clock,
        }
        synced = {
            "id": "memo-1",
            "content": "-- caf\u00e9",
            "updated_at_utc": clock,
        }

        state, exact = store._manifest_assessment(
            [store._after_memo(original)], {"memo-1": synced}
        )

        self.assertEqual(state, "ready")
        self.assertFalse(exact)


class TaskRollupScratchDbTests(unittest.TestCase):
    USER = {"id": "rollup-api-owner", "permission": "user", "is_admin": False}
    VIEWER = {"id": "rollup-api-viewer", "permission": "user", "is_admin": False}
    SERVICE_OWNER = {"id": "rollup-service-owner", "permission": "owner", "is_admin": True}

    @classmethod
    def setUpClass(cls) -> None:
        use_scratch_database()
        if not init_scratch_database():
            raise unittest.SkipTest("task rollup scratch database unavailable")

    def setUp(self) -> None:
        use_scratch_database()
        truncate_scratch_tables(extra=("task_rollup_completions",))
        now = datetime(2026, 11, 24, 0, 0, 0)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, owner_id, name, icon, status, is_secret, is_group,
                         created_at, updated_at, updated_at_utc)
                    VALUES ('rollup-project', %s, 'Rollup', 'R', 0, 0, 0, %s, %s, %s)
                    """,
                    (self.USER["id"], now, now, now),
                )
                cursor.execute(
                    """
                    INSERT INTO project_members
                        (id, project_id, user_id, role, invited_at, updated_at_utc)
                    VALUES ('rollup-member', 'rollup-project', %s, 'owner', %s, %s)
                    """,
                    (self.USER["id"], now, now),
                )
                cursor.execute(
                    """
                    INSERT INTO project_members
                        (id, project_id, user_id, role, invited_at, updated_at_utc)
                    VALUES ('rollup-viewer-member', 'rollup-project', %s, 'viewer', %s, %s)
                    """,
                    (self.VIEWER["id"], now, now),
                )
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES ('rollup-memo', 'rollup-project', %s, 'memo', 'Source', %s,
                            0, %s, %s, %s)
                    """,
                    (
                        self.USER["id"],
                        "-- caf\u00e9 \U0001f600\r\n설명\r\n-- caf\u00e9 \U0001f600\r\n-- caf\u00e9 \U0001f600\r\n",
                        now,
                        now,
                        now,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES ('rollup-memo-b', 'rollup-project', %s, 'memo', 'Other',
                            '-- forged target', 0, %s, %s, %s)
                    """,
                    (self.USER["id"], now, now, now),
                )

    def _request(self) -> dict:
        initial = store.preview("rollup-project", self.USER, [])
        group = next(
            group for group in initial["groups"]
            if len(group["occurrences"]) == 3
        )
        request = complete_request(
            occurrence_keys=[item["key"] for item in group["occurrences"]]
        )
        request["corpusDigest"] = initial["corpusDigest"]
        request["groupKey"] = group["key"]
        request["leases"] = [{"memoId": "rollup-memo", "token": "owned"}]
        return request

    def _insert_duplicate_source(self) -> None:
        now = datetime(2026, 11, 24, 0, 0, 0)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES ('rollup-memo-c', 'rollup-project', %s, 'memo', 'Copy',
                            '-- caf\u00e9 \U0001f600', 0, %s, %s, %s)
                    """,
                    (self.USER["id"], now, now, now),
                )

    def _multi_request(self, operation_id: str = UUID_1) -> dict:
        preview = store.preview("rollup-project", self.USER, [])
        group = next(
            item for item in preview["groups"]
            if {occurrence["memoId"] for occurrence in item["occurrences"]}
            == {"rollup-memo", "rollup-memo-c"}
        )
        return {
            "operationId": operation_id,
            "corpusDigest": preview["corpusDigest"],
            "groupKey": group["key"],
            "aliases": [],
            "occurrenceKeys": [item["key"] for item in group["occurrences"]],
            "leases": [
                {"memoId": "rollup-memo", "token": "owned-a"},
                {"memoId": "rollup-memo-c", "token": "owned-c"},
            ],
        }

    def _seed_large_scoped_corpus(self) -> list[str]:
        created = datetime(2026, 11, 25, 0, 0, 0)
        memo_ids = [f"scope-{index:03d}" for index in range(323)]
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, 'rollup-project', %s, 'memo', %s,
                            '-- Scoped task', 0, %s, %s, %s)
                    """,
                    [
                        (memo_id, self.USER["id"], memo_id, created, created, created)
                        for memo_id in memo_ids
                    ],
                )
        return memo_ids

    def test_complete_and_undo_are_atomic_prefix_only_writes(self) -> None:
        fixed_now = datetime(2026, 11, 24, 1, 0, 0, tzinfo=timezone.utc)
        with patch.object(store, "rollup_now_utc", return_value=fixed_now):
            request = self._request()
            work = store.prepare_completion("rollup-project", self.USER, request)
            completed = store.commit_completion(
                "rollup-project", self.USER, request, work, lambda: True
            )

            retry = {**request, "leases": [{"memoId": "rollup-memo", "token": "expired"}]}
            replay = store.prepare_completion("rollup-project", self.USER, retry)
            self.assertEqual(replay.replay, completed)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE project_members SET role = 'viewer' WHERE id = 'rollup-member'"
                    )
            downgraded_replay = store.prepare_completion(
                "rollup-project", self.USER, retry
            )
            self.assertEqual(downgraded_replay.replay, completed)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE project_members SET role = 'owner' WHERE id = 'rollup-member'"
                    )

            own_preview = store.preview("rollup-project", self.USER, [])
            service_owner_preview = store.preview(
                "rollup-project", self.SERVICE_OWNER, []
            )
            self.assertEqual(
                [item["operationId"] for item in own_preview["completions"]],
                [UUID_1],
            )
            self.assertEqual(service_owner_preview["completions"], [])

            self.assertEqual(completed["changedMemoIds"], ["rollup-memo"])
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT content FROM memos WHERE id = 'rollup-memo'")
                    checked = cursor.fetchone()["content"]
                    self.assertEqual(checked.count("v-- caf\u00e9 \U0001f600"), 3)
                    cursor.execute(
                        "SELECT COUNT(*) AS count FROM memo_versions WHERE memo_id = 'rollup-memo'"
                    )
                    self.assertEqual(cursor.fetchone()["count"], 1)

            current = store.preview("rollup-project", self.USER, [])
            undo_request = {
                "undoOperationId": "00000000-0000-4000-8000-000000000002",
                "corpusDigest": current["corpusDigest"],
                "leases": [{"memoId": "rollup-memo", "token": "owned"}],
            }
            undo_work = store.prepare_undo(
                "rollup-project", UUID_1, self.USER, undo_request
            )
            undone = store.commit_undo(
                "rollup-project", UUID_1, self.USER, undo_request,
                undo_work, lambda: True,
            )

        self.assertEqual(undone["status"], "undone")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'rollup-memo'")
                original = cursor.fetchone()["content"]
                self.assertEqual(original.count("-- caf\u00e9 \U0001f600"), 3)
                self.assertNotIn("v-- ", original)
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM memo_versions WHERE memo_id = 'rollup-memo'"
                )
                self.assertEqual(cursor.fetchone()["count"], 2)
                cursor.execute(
                    "SELECT undo_operation_id FROM task_rollup_completions WHERE id = %s",
                    (UUID_1,),
                )
                self.assertEqual(
                    cursor.fetchone()["undo_operation_id"], undo_request["undoOperationId"]
                )

    def test_injected_receipt_failure_rolls_back_memo_and_version(self) -> None:
        fixed_now = datetime(2026, 11, 24, 1, 0, 0, tzinfo=timezone.utc)
        with patch.object(store, "rollup_now_utc", return_value=fixed_now):
            request = self._request()
            work = store.prepare_completion("rollup-project", self.USER, request)

            def fail_at_receipt(stage: str) -> None:
                if stage == "completion":
                    raise RuntimeError("injected receipt failure")

            with patch.object(store, "_failure_point", side_effect=fail_at_receipt):
                with self.assertRaisesRegex(RuntimeError, "injected receipt failure"):
                    store.commit_completion(
                        "rollup-project", self.USER, request, work, lambda: True
                    )

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'rollup-memo'")
                self.assertNotIn("v-- ", cursor.fetchone()["content"])
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM memo_versions WHERE memo_id = 'rollup-memo'"
                )
                self.assertEqual(cursor.fetchone()["count"], 0)
                cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                self.assertEqual(cursor.fetchone()["count"], 0)

    def test_viewer_can_preview_but_cannot_complete_normal_memos(self) -> None:
        fixed_now = datetime(2026, 11, 24, 1, 0, 0, tzinfo=timezone.utc)
        with patch.object(store, "rollup_now_utc", return_value=fixed_now):
            request = self._request()
            preview = store.preview("rollup-project", self.VIEWER, [])
            self.assertFalse(preview["canComplete"])
            with self.assertRaises(HTTPException) as raised:
                store.prepare_completion(
                    "rollup-project", self.VIEWER, request
                )

        self.assertEqual(raised.exception.status_code, 403)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'rollup-memo'")
                self.assertNotIn("v-- ", cursor.fetchone()["content"])
                cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                self.assertEqual(cursor.fetchone()["count"], 0)

    def test_role_revoked_after_preflight_is_rechecked_under_lock(self) -> None:
        request = self._request()
        work = store.prepare_completion("rollup-project", self.USER, request)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE project_members SET role = 'viewer' WHERE id = 'rollup-member'"
                )

        with self.assertRaises(HTTPException) as raised:
            store.commit_completion(
                "rollup-project", self.USER, request, work, lambda: True
            )

        self.assertEqual(raised.exception.status_code, 403)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'rollup-memo'")
                self.assertNotIn("v-- ", cursor.fetchone()["content"])
                cursor.execute("SELECT COUNT(*) AS count FROM memo_versions")
                self.assertEqual(cursor.fetchone()["count"], 0)
                cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                self.assertEqual(cursor.fetchone()["count"], 0)

    def test_forged_cross_memo_receipt_is_rejected_before_undo_writes(self) -> None:
        fixed_now = datetime(2026, 11, 24, 1, 0, 0, tzinfo=timezone.utc)
        with patch.object(store, "rollup_now_utc", return_value=fixed_now):
            initial = store.preview("rollup-project", self.USER, [])
            forged_group = next(
                group for group in initial["groups"]
                if any(item["memoId"] == "rollup-memo-b" for item in group["occurrences"])
            )
            forged_occurrence = forged_group["occurrences"][0]
            request = self._request()
            work = store.prepare_completion("rollup-project", self.USER, request)
            store.commit_completion(
                "rollup-project", self.USER, request, work, lambda: True
            )
            current_digest = store.preview(
                "rollup-project", self.USER, []
            )["corpusDigest"]
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT data_json FROM task_rollup_completions WHERE id = %s",
                        (UUID_1,),
                    )
                    forged = store._safe_json_load(cursor.fetchone()["data_json"])
                    forged["matchedOccurrences"] = [forged_occurrence]
                    forged["changedOccurrences"] = [forged_occurrence]
                    forged["beforeRevisions"] = {
                        "rollup-memo-b": forged_occurrence["revision"]
                    }
                    cursor.execute(
                        "UPDATE task_rollup_completions SET data_json = %s WHERE id = %s",
                        (store._canonical_json(forged), UUID_1),
                    )

            undo_request = {
                "undoOperationId": "00000000-0000-4000-8000-000000000003",
                "corpusDigest": current_digest,
                "leases": [{"memoId": "rollup-memo-b", "token": "valid-b"}],
            }
            with self.assertRaises(HTTPException) as raised:
                store.prepare_undo(
                    "rollup-project", UUID_1, self.USER, undo_request
                )
            forced_work = store.RollupWork(
                store.undo_payload_hash("rollup-project", UUID_1, undo_request),
                ("rollup-memo-b",),
            )
            with self.assertRaises(HTTPException) as commit_raised:
                store.commit_undo(
                    "rollup-project", UUID_1, self.USER, undo_request,
                    forced_work, lambda: True,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "task_rollup_receipt_invalid")
        self.assertEqual(commit_raised.exception.status_code, 409)
        self.assertEqual(
            commit_raised.exception.detail["code"], "task_rollup_receipt_invalid"
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'rollup-memo-b'")
                self.assertEqual(cursor.fetchone()["content"], "-- forged target")
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM memo_versions WHERE memo_id = 'rollup-memo-b'"
                )
                self.assertEqual(cursor.fetchone()["count"], 0)

    def test_receipt_source_missing_is_pending_until_exact_source_arrives(self) -> None:
        fixed_now = datetime(2026, 11, 24, 1, 0, 0, tzinfo=timezone.utc)
        with patch.object(store, "rollup_now_utc", return_value=fixed_now):
            request = self._request()
            work = store.prepare_completion("rollup-project", self.USER, request)
            store.commit_completion(
                "rollup-project", self.USER, request, work, lambda: True
            )
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT * FROM memos WHERE id = 'rollup-memo'")
                    source = cursor.fetchone()
                    cursor.execute("DELETE FROM memos WHERE id = 'rollup-memo'")

            pending = store.preview("rollup-project", self.USER, [])["completions"][0]
            self.assertEqual(pending["status"], "pending")
            self.assertFalse(pending["canUndo"])

            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO memos
                            (id, project_id, created_by, kind, title, content, status,
                             deleted_at, created_at, updated_at, updated_at_utc)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            source["id"], source["project_id"], source["created_by"],
                            source["kind"], source["title"], source["content"],
                            source["status"], source["deleted_at"], source["created_at"],
                            source["updated_at"], source["updated_at_utc"],
                        ),
                    )

            ready = store.preview("rollup-project", self.USER, [])["completions"][0]

        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["canUndo"])

    def test_deleted_or_moved_source_is_superseded_without_current_text_leak(self) -> None:
        fixed_now = datetime(2026, 11, 24, 1, 0, 0, tzinfo=timezone.utc)
        with patch.object(store, "rollup_now_utc", return_value=fixed_now):
            request = self._request()
            work = store.prepare_completion("rollup-project", self.USER, request)
            store.commit_completion(
                "rollup-project", self.USER, request, work, lambda: True
            )
            secret = "INACCESSIBLE CURRENT SOURCE TEXT"
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE memos SET content = %s, deleted_at = %s WHERE id = 'rollup-memo'",
                        (secret, datetime(2026, 11, 24, 1, 1, 0)),
                    )
            deleted = store.preview("rollup-project", self.USER, [])["completions"][0]
            self.assertEqual(deleted["status"], "superseded")
            self.assertNotIn(secret, store._canonical_json(deleted))

            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO projects
                            (id, owner_id, name, icon, status, is_secret, is_group,
                             created_at, updated_at, updated_at_utc)
                        VALUES ('moved-project', %s, 'Moved', 'M', 0, 0, 0, %s, %s, %s)
                        """,
                        (
                            self.USER["id"], datetime(2026, 11, 24),
                            datetime(2026, 11, 24), datetime(2026, 11, 24),
                        ),
                    )
                    cursor.execute(
                        "UPDATE memos SET project_id = 'moved-project', deleted_at = NULL WHERE id = 'rollup-memo'"
                    )
            moved = store.preview("rollup-project", self.USER, [])["completions"][0]

        self.assertEqual(moved["status"], "superseded")
        self.assertNotIn(secret, store._canonical_json(moved))

    def test_undo_metadata_waits_for_inverse_sources_then_becomes_undone(self) -> None:
        fixed_now = datetime(2026, 11, 24, 1, 0, 0, tzinfo=timezone.utc)
        with patch.object(store, "rollup_now_utc", return_value=fixed_now):
            request = self._request()
            work = store.prepare_completion("rollup-project", self.USER, request)
            store.commit_completion(
                "rollup-project", self.USER, request, work, lambda: True
            )
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT * FROM memos WHERE id = 'rollup-memo'")
                    checked_source = cursor.fetchone()
            current = store.preview("rollup-project", self.USER, [])
            undo_request = {
                "undoOperationId": "00000000-0000-4000-8000-000000000004",
                "corpusDigest": current["corpusDigest"],
                "leases": [{"memoId": "rollup-memo", "token": "owned"}],
            }
            undo_work = store.prepare_undo(
                "rollup-project", UUID_1, self.USER, undo_request
            )
            with patch.object(
                store,
                "rollup_now_utc",
                return_value=datetime(2026, 11, 24, 1, 0, 1, tzinfo=timezone.utc),
            ):
                store.commit_undo(
                    "rollup-project", UUID_1, self.USER, undo_request,
                    undo_work, lambda: True,
                )
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT * FROM memos WHERE id = 'rollup-memo'")
                    inverse_source = cursor.fetchone()
                    cursor.execute(
                        """
                        UPDATE memos SET content = %s, updated_at = %s, updated_at_utc = %s
                        WHERE id = 'rollup-memo'
                        """,
                        (
                            checked_source["content"], checked_source["updated_at"],
                            checked_source["updated_at_utc"],
                        ),
                    )

            pending = store.preview("rollup-project", self.USER, [])["completions"][0]
            self.assertEqual(pending["status"], "pending")
            self.assertFalse(pending["canUndo"])

            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE memos SET content = %s, updated_at = %s, updated_at_utc = %s
                        WHERE id = 'rollup-memo'
                        """,
                        (
                            inverse_source["content"], inverse_source["updated_at"],
                            inverse_source["updated_at_utc"],
                        ),
                    )
            undone = store.preview("rollup-project", self.USER, [])["completions"][0]
            self.assertEqual(undone["status"], "undone")

            secret = "NEWER SOURCE MUST NOT HYDRATE INTO HISTORY"
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE memos SET content = %s, updated_at_utc = %s
                        WHERE id = 'rollup-memo'
                        """,
                        (secret, datetime(2026, 11, 24, 1, 0, 2)),
                    )
            superseded = store.preview("rollup-project", self.USER, [])["completions"][0]

        self.assertEqual(superseded["status"], "superseded")
        self.assertFalse(superseded["canUndo"])
        self.assertNotIn(secret, store._canonical_json(superseded))

    def test_completion_failure_injection_rolls_back_every_write_stage(self) -> None:
        stages = (
            "version:rollup-memo",
            "memo:rollup-memo",
            "version:rollup-memo-c",
            "memo:rollup-memo-c",
            "completion",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                self.setUp()
                self._insert_duplicate_source()
                request = self._multi_request()
                work = store.prepare_completion("rollup-project", self.USER, request)

                def inject(actual: str, target: str = stage) -> None:
                    if actual == target:
                        raise RuntimeError(f"injected {target}")

                with patch.object(store, "_failure_point", side_effect=inject):
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        store.commit_completion(
                            "rollup-project", self.USER, request, work, lambda: True
                        )
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "SELECT id, content FROM memos WHERE id IN ('rollup-memo', 'rollup-memo-c') ORDER BY id"
                        )
                        self.assertTrue(
                            all("v-- " not in row["content"] for row in cursor.fetchall())
                        )
                        cursor.execute("SELECT COUNT(*) AS count FROM memo_versions")
                        self.assertEqual(cursor.fetchone()["count"], 0)
                        cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                        self.assertEqual(cursor.fetchone()["count"], 0)

    def test_undo_failure_injection_rolls_back_every_write_stage(self) -> None:
        stages = (
            "version:rollup-memo",
            "undo-memo:rollup-memo",
            "version:rollup-memo-c",
            "undo-memo:rollup-memo-c",
            "undo-receipt",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                self.setUp()
                self._insert_duplicate_source()
                request = self._multi_request()
                work = store.prepare_completion("rollup-project", self.USER, request)
                store.commit_completion(
                    "rollup-project", self.USER, request, work, lambda: True
                )
                preview = store.preview("rollup-project", self.USER, [])
                undo_request = {
                    "undoOperationId": "00000000-0000-4000-8000-000000000005",
                    "corpusDigest": preview["corpusDigest"],
                    "leases": request["leases"],
                }
                undo_work = store.prepare_undo(
                    "rollup-project", UUID_1, self.USER, undo_request
                )

                def inject(actual: str, target: str = stage) -> None:
                    if actual == target:
                        raise RuntimeError(f"injected {target}")

                with patch.object(store, "_failure_point", side_effect=inject):
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        store.commit_undo(
                            "rollup-project", UUID_1, self.USER, undo_request,
                            undo_work, lambda: True,
                        )
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "SELECT content FROM memos WHERE id IN ('rollup-memo', 'rollup-memo-c')"
                        )
                        self.assertTrue(
                            all("v-- " in row["content"] for row in cursor.fetchall())
                        )
                        cursor.execute("SELECT COUNT(*) AS count FROM memo_versions")
                        self.assertEqual(cursor.fetchone()["count"], 2)
                        cursor.execute(
                            "SELECT undo_operation_id FROM task_rollup_completions WHERE id = %s",
                            (UUID_1,),
                        )
                        self.assertIsNone(cursor.fetchone()["undo_operation_id"])

    def test_corpus_gap_lock_blocks_phantom_insert_until_completion_commits(self) -> None:
        request = self._request()
        work = store.prepare_completion("rollup-project", self.USER, request)
        locked = threading.Event()
        release = threading.Event()

        def hook(stage: str) -> None:
            if stage == "memo:rollup-memo":
                locked.set()
                if not release.wait(3):
                    raise RuntimeError("phantom race barrier timed out")

        def insert_phantom() -> None:
            now = datetime(2026, 11, 24, 0, 1, 0)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO memos
                            (id, project_id, created_by, kind, title, content, status,
                             created_at, updated_at, updated_at_utc)
                        VALUES ('zz-rollup-phantom', 'rollup-project', %s, 'memo',
                                'Phantom', '-- caf\u00e9 \U0001f600', 0, %s, %s, %s)
                        """,
                        (self.USER["id"], now, now, now),
                    )

        with patch.object(store, "_failure_point", side_effect=hook):
            with ThreadPoolExecutor(max_workers=2) as pool:
                completion_future = pool.submit(
                    store.commit_completion,
                    "rollup-project", self.USER, request, work, lambda: True,
                )
                self.assertTrue(locked.wait(3))
                insert_future = pool.submit(insert_phantom)
                time.sleep(0.25)
                self.assertFalse(insert_future.done())
                release.set()
                result = completion_future.result(timeout=5)
                insert_future.result(timeout=5)

        self.assertEqual(result["operationId"], UUID_1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'zz-rollup-phantom'")
                self.assertTrue(cursor.fetchone()["content"].startswith("-- "))
                cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                self.assertEqual(cursor.fetchone()["count"], 1)

    def test_overlapping_completion_requests_serialize_without_partial_writes(self) -> None:
        request_one = self._request()
        request_two = copy.deepcopy(request_one)
        request_two["operationId"] = "00000000-0000-4000-8000-000000000006"
        work_one = store.prepare_completion("rollup-project", self.USER, request_one)
        work_two = store.prepare_completion("rollup-project", self.USER, request_two)
        barrier = threading.Barrier(2)

        def run(request, work):
            barrier.wait(timeout=3)
            try:
                return store.commit_completion(
                    "rollup-project", self.USER, request, work, lambda: True
                )
            except HTTPException as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(run, request_one, work_one),
                pool.submit(run, request_two, work_two),
            ]
            outcomes = [future.result(timeout=8) for future in futures]

        successes = [item for item in outcomes if isinstance(item, dict)]
        conflicts = [item for item in outcomes if isinstance(item, HTTPException)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].status_code, 409)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'rollup-memo'")
                self.assertEqual(cursor.fetchone()["content"].count("v-- "), 3)
                cursor.execute("SELECT COUNT(*) AS count FROM memo_versions")
                self.assertEqual(cursor.fetchone()["count"], 1)
                cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                self.assertEqual(cursor.fetchone()["count"], 1)

    def test_nested_completion_preserves_context_without_cascade(self) -> None:
        now = datetime(2026, 11, 24, 0, 0, 0)
        source = (
            "v-- Checked parent\n"
            "  - explanation\n"
            "    -- Child target\n"
            "    -- Sibling target\n"
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES ('rollup-nested', 'rollup-project', %s, 'memo', 'Nested',
                            %s, 0, %s, %s, %s)
                    """,
                    (self.USER["id"], source, now, now, now),
                )
        fixed_now = datetime(2026, 11, 24, 1, 0, 0, tzinfo=timezone.utc)
        with patch.object(store, "rollup_now_utc", return_value=fixed_now):
            preview = store.preview("rollup-project", self.USER, [])
            stack = list(preview["groups"])
            selected = None
            while stack:
                node = stack.pop()
                if node["text"] == "Child target":
                    selected = node
                    break
                stack.extend(node["children"])
            self.assertIsNotNone(selected)
            request = {
                "operationId": "00000000-0000-4000-8000-000000000007",
                "corpusDigest": preview["corpusDigest"],
                "groupKey": selected["key"],
                "aliases": [],
                "occurrenceKeys": [item["key"] for item in selected["occurrences"]],
                "leases": [{"memoId": "rollup-nested", "token": "nested-owned"}],
            }
            work = store.prepare_completion("rollup-project", self.USER, request)
            completed = store.commit_completion(
                "rollup-project", self.USER, request, work, lambda: True
            )
            expected_context = [
                {"key": completed["context"][0]["key"], "kind": "task", "text": "Checked parent", "checked": True},
                {"key": completed["context"][1]["key"], "kind": "description", "text": "explanation", "checked": False},
            ]
            self.assertEqual(completed["context"], expected_context)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT content FROM memos WHERE id = 'rollup-nested'")
                    checked = cursor.fetchone()["content"]
            self.assertIn("v-- Checked parent", checked)
            self.assertIn("v-- Child target", checked)
            self.assertIn("-- Sibling target", checked)

            reloaded = store.preview("rollup-project", self.USER, [])["completions"][0]
            self.assertEqual(reloaded["context"], expected_context)
            undo_request = {
                "undoOperationId": "00000000-0000-4000-8000-000000000008",
                "corpusDigest": store.preview("rollup-project", self.USER, [])["corpusDigest"],
                "leases": [{"memoId": "rollup-nested", "token": "nested-owned"}],
            }
            undo_work = store.prepare_undo(
                "rollup-project", request["operationId"], self.USER, undo_request
            )
            with patch.object(
                store,
                "rollup_now_utc",
                return_value=datetime(2026, 11, 24, 1, 0, 1, tzinfo=timezone.utc),
            ):
                undone = store.commit_undo(
                    "rollup-project", request["operationId"], self.USER,
                    undo_request, undo_work, lambda: True,
                )

        self.assertEqual(undone["context"], expected_context)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'rollup-nested'")
                restored = cursor.fetchone()["content"]
        self.assertIn("v-- Checked parent", restored)
        self.assertIn("-- Child target", restored)
        self.assertIn("-- Sibling target", restored)

    def test_large_batch_uses_one_strict_canonical_precommit_fence(self) -> None:
        now = datetime(2026, 11, 24, 0, 0, 0)
        bulk_ids = [f"rollup-bulk-{index:03d}" for index in range(95)]
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, 'rollup-project', %s, 'memo', %s,
                            '-- caf\u00e9 \U0001f600', 0, %s, %s, %s)
                    """,
                    [
                        (memo_id, self.USER["id"], memo_id, now, now, now)
                        for memo_id in bulk_ids
                    ],
                )
        preview = store.preview("rollup-project", self.USER, [])
        expected_ids = {"rollup-memo", *bulk_ids}
        group = next(
            item for item in preview["groups"]
            if {occurrence["memoId"] for occurrence in item["occurrences"]}
            == expected_ids
        )
        request = {
            "operationId": "00000000-0000-4000-8000-000000000009",
            "corpusDigest": preview["corpusDigest"],
            "groupKey": group["key"],
            "aliases": [],
            "occurrenceKeys": [item["key"] for item in group["occurrences"]],
            "leases": [
                {"memoId": memo_id, "token": f"token-{memo_id}"}
                for memo_id in sorted(expected_ids)
            ],
        }
        work = store.prepare_completion("rollup-project", self.USER, request)
        calls = {"local": 0, "strict": 0}

        def validate_local() -> bool:
            calls["local"] += 1
            return True

        def validate_strict() -> bool:
            calls["strict"] += 1
            return True

        result = store.commit_completion(
            "rollup-project", self.USER, request, work,
            validate_local, validate_strict,
        )

        self.assertEqual(len(result["changedMemoIds"]), 96)
        self.assertGreater(calls["local"], 96)
        self.assertEqual(calls["strict"], 1)

    def test_strict_final_fence_revocation_rolls_back_all_local_writes(self) -> None:
        request = self._request()
        work = store.prepare_completion("rollup-project", self.USER, request)
        calls = {"local": 0, "strict": 0}

        def validate_local() -> bool:
            calls["local"] += 1
            return True

        def validate_strict() -> bool:
            calls["strict"] += 1
            return False

        with self.assertRaises(HTTPException) as raised:
            store.commit_completion(
                "rollup-project", self.USER, request, work,
                validate_local, validate_strict,
            )

        self.assertEqual(raised.exception.detail["code"], "task_rollup_reservation_lost")
        self.assertGreater(calls["local"], 1)
        self.assertEqual(calls["strict"], 1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'rollup-memo'")
                self.assertNotIn("v-- ", cursor.fetchone()["content"])
                cursor.execute("SELECT COUNT(*) AS count FROM memo_versions")
                self.assertEqual(cursor.fetchone()["count"], 0)
                cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                self.assertEqual(cursor.fetchone()["count"], 0)

    def test_postcommit_notification_failure_keeps_durable_idempotent_result(self) -> None:
        request_payload = self._request()

        class Reservation:
            def validate_local(self) -> bool:
                return True

            def validate(self) -> bool:
                return True

            async def release(self) -> None:
                return None

        class FailingRealtime:
            available = True

            def __init__(self) -> None:
                self.reserve_calls = 0

            async def reserve_task_rollup(self, _user_id, _operation_id, _leases):
                self.reserve_calls += 1
                return Reservation()

            async def revalidate_memo_access(self, _memo_id):
                return None

            async def emit_memo_pulled(self, *_args):
                raise RuntimeError("forced memo notification failure")

            async def emit_projects_changed(self):
                raise RuntimeError("forced project notification failure")

        realtime = FailingRealtime()
        http_request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/projects/rollup-project/backlog/task-rollup/complete",
                "headers": [],
            }
        )
        data = TaskRollupCompleteRequest.model_validate(request_payload)
        with (
            patch.object(router, "get_realtime_server", return_value=realtime),
            self.assertLogs("src.routers.task_rollup", level="ERROR") as captured,
        ):
            result = asyncio.run(
                router.complete_task_rollup(
                    "rollup-project", data, http_request, self.USER
                )
            )

        retry_payload = copy.deepcopy(request_payload)
        retry_payload["leases"][0]["token"] = "expired-rotated-token"
        retry = asyncio.run(
            router.complete_task_rollup(
                "rollup-project",
                TaskRollupCompleteRequest.model_validate(retry_payload),
                http_request,
                self.USER,
            )
        )

        self.assertEqual(retry, result)
        self.assertEqual(realtime.reserve_calls, 1)
        logs = "\n".join(captured.output)
        self.assertIn(UUID_1, logs)
        self.assertIn("rollup-memo", logs)
        self.assertNotIn("expired-rotated-token", logs)
        self.assertNotIn("caf\u00e9", logs)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'rollup-memo'")
                self.assertEqual(cursor.fetchone()["content"].count("v-- "), 3)
                cursor.execute("SELECT COUNT(*) AS count FROM memo_versions")
                self.assertEqual(cursor.fetchone()["count"], 1)
                cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                self.assertEqual(cursor.fetchone()["count"], 1)

    def test_recent30_scopes_325_memos_and_undoes_aged_out_source_only(self) -> None:
        self._seed_large_scoped_corpus()
        with self.assertRaises(HTTPException) as all_error:
            store.preview("rollup-project", self.USER, [], "all")
        self.assertEqual(all_error.exception.status_code, 413)

        preview = store.preview("rollup-project", self.USER, [], "recent30")
        self.assertEqual(preview["memoScope"], "recent30")
        self.assertEqual(preview["totalMemoCount"], 325)
        self.assertEqual(preview["selectedMemoCount"], 30)
        self.assertEqual(preview["excludedMemoCount"], 295)
        group = next(item for item in preview["groups"] if item["text"] == "Scoped task")
        selected_ids = {item["memoId"] for item in group["occurrences"]}
        self.assertEqual(selected_ids, {f"scope-{index:03d}" for index in range(293, 323)})
        request = {
            "operationId": "00000000-0000-4000-8000-000000000010",
            "corpusDigest": preview["corpusDigest"],
            "groupKey": group["key"],
            "aliases": [],
            "occurrenceKeys": [item["key"] for item in group["occurrences"]],
            "leases": [
                {"memoId": memo_id, "token": f"token-{memo_id}"}
                for memo_id in sorted(selected_ids)
            ],
            "memoScope": "recent30",
        }
        work = store.prepare_completion("rollup-project", self.USER, request)
        completed = store.commit_completion(
            "rollup-project", self.USER, request, work, lambda: True
        )
        self.assertEqual(completed["memoScope"], "recent30")

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, content FROM memos WHERE id LIKE 'scope-%%' ORDER BY id"
                )
                by_id = {row["id"]: row["content"] for row in cursor.fetchall()}
        self.assertTrue(all(by_id[memo_id].startswith("v-- ") for memo_id in selected_ids))
        self.assertTrue(
            all(by_id[f"scope-{index:03d}"].startswith("-- ") for index in range(293))
        )
        recent_trails = store.preview(
            "rollup-project", self.USER, [], "recent30"
        )["completions"]
        self.assertEqual([item["operationId"] for item in recent_trails], [request["operationId"]])

        newest = datetime(2026, 11, 26, 0, 0, 0)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES ('scope-999', 'rollup-project', %s, 'memo', 'Newest',
                            '-- Scoped task', 0, %s, %s, %s)
                    """,
                    (self.USER["id"], newest, newest, newest),
                )
        shifted = store.preview("rollup-project", self.USER, [], "recent30")
        shifted_trail = shifted["completions"][0]
        self.assertEqual(shifted_trail["status"], "ready")
        undo_request = {
            "undoOperationId": "00000000-0000-4000-8000-000000000011",
            "corpusDigest": shifted["corpusDigest"],
            "leases": request["leases"],
            "memoScope": "recent30",
        }
        undo_work = store.prepare_undo(
            "rollup-project", request["operationId"], self.USER, undo_request
        )
        undone = store.commit_undo(
            "rollup-project", request["operationId"], self.USER,
            undo_request, undo_work, lambda: True,
        )
        self.assertEqual(undone["memoScope"], "recent30")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, content FROM memos WHERE id IN ('scope-293', 'scope-999') ORDER BY id"
                )
                final = {row["id"]: row["content"] for row in cursor.fetchall()}
        self.assertTrue(final["scope-293"].startswith("-- "))
        self.assertTrue(final["scope-999"].startswith("-- "))

    def test_recent30_new_memo_between_preview_and_commit_is_409_no_writes(self) -> None:
        self._seed_large_scoped_corpus()
        preview = store.preview("rollup-project", self.USER, [], "recent30")
        group = next(item for item in preview["groups"] if item["text"] == "Scoped task")
        selected_ids = sorted({item["memoId"] for item in group["occurrences"]})
        request = {
            "operationId": "00000000-0000-4000-8000-000000000012",
            "corpusDigest": preview["corpusDigest"],
            "groupKey": group["key"],
            "aliases": [],
            "occurrenceKeys": [item["key"] for item in group["occurrences"]],
            "leases": [
                {"memoId": memo_id, "token": f"token-{memo_id}"}
                for memo_id in selected_ids
            ],
            "memoScope": "recent30",
        }
        work = store.prepare_completion("rollup-project", self.USER, request)
        newest = datetime(2026, 11, 26, 0, 0, 0)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES ('scope-999', 'rollup-project', %s, 'memo', 'Newest',
                            '-- Scoped task', 0, %s, %s, %s)
                    """,
                    (self.USER["id"], newest, newest, newest),
                )
        with self.assertRaises(HTTPException) as raised:
            store.commit_completion(
                "rollup-project", self.USER, request, work, lambda: True
            )
        self.assertEqual(raised.exception.status_code, 409)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS count FROM memo_versions")
                self.assertEqual(cursor.fetchone()["count"], 0)
                cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                self.assertEqual(cursor.fetchone()["count"], 0)

    def test_completion_trails_are_filtered_by_recorded_scope(self) -> None:
        preview = store.preview("rollup-project", self.USER, [], "recent30")
        group = next(item for item in preview["groups"] if item["text"] == "caf\u00e9 \U0001f600")
        request = {
            "operationId": "00000000-0000-4000-8000-000000000013",
            "corpusDigest": preview["corpusDigest"],
            "groupKey": group["key"],
            "aliases": [],
            "occurrenceKeys": [item["key"] for item in group["occurrences"]],
            "leases": [{"memoId": "rollup-memo", "token": "owned"}],
            "memoScope": "recent30",
        }
        work = store.prepare_completion("rollup-project", self.USER, request)
        store.commit_completion(
            "rollup-project", self.USER, request, work, lambda: True
        )

        recent = store.preview("rollup-project", self.USER, [], "recent30")
        all_scope = store.preview("rollup-project", self.USER, [], "all")
        self.assertEqual([item["operationId"] for item in recent["completions"]], [request["operationId"]])
        self.assertEqual(all_scope["completions"], [])

    def test_recent30_metadata_ceiling_rejects_whole_preview_without_writes(self) -> None:
        with patch.object(store, "MAX_METADATA_MEMOS", 1):
            with self.assertRaises(HTTPException) as raised:
                store.preview("rollup-project", self.USER, [], "recent30")

        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(raised.exception.detail["code"], "tooManyMemoMetadata")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS count FROM memo_versions")
                self.assertEqual(cursor.fetchone()["count"], 0)
                cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                self.assertEqual(cursor.fetchone()["count"], 0)

    def test_recent30_full_metadata_gap_lock_blocks_concurrent_insert(self) -> None:
        preview = store.preview("rollup-project", self.USER, [], "recent30")
        group = next(item for item in preview["groups"] if item["text"] == "caf\u00e9 \U0001f600")
        request = {
            "operationId": "00000000-0000-4000-8000-000000000014",
            "corpusDigest": preview["corpusDigest"],
            "groupKey": group["key"],
            "aliases": [],
            "occurrenceKeys": [item["key"] for item in group["occurrences"]],
            "leases": [{"memoId": "rollup-memo", "token": "owned"}],
            "memoScope": "recent30",
        }
        work = store.prepare_completion("rollup-project", self.USER, request)
        locked = threading.Event()
        release = threading.Event()

        def hook(stage: str) -> None:
            if stage == "memo:rollup-memo":
                locked.set()
                if not release.wait(3):
                    raise RuntimeError("recent30 phantom race barrier timed out")

        def insert_phantom() -> None:
            now = datetime(2026, 11, 26, 0, 0, 0)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO memos
                            (id, project_id, created_by, kind, title, content, status,
                             created_at, updated_at, updated_at_utc)
                        VALUES ('zz-recent-phantom', 'rollup-project', %s, 'memo',
                                'Recent phantom', '-- caf\u00e9 \U0001f600', 0, %s, %s, %s)
                        """,
                        (self.USER["id"], now, now, now),
                    )

        with patch.object(store, "_failure_point", side_effect=hook):
            with ThreadPoolExecutor(max_workers=2) as pool:
                completion_future = pool.submit(
                    store.commit_completion,
                    "rollup-project", self.USER, request, work, lambda: True,
                )
                self.assertTrue(locked.wait(3))
                insert_future = pool.submit(insert_phantom)
                time.sleep(0.25)
                self.assertFalse(insert_future.done())
                release.set()
                completed = completion_future.result(timeout=5)
                insert_future.result(timeout=5)

        self.assertEqual(completed["memoScope"], "recent30")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT content FROM memos WHERE id = 'zz-recent-phantom'")
                self.assertTrue(cursor.fetchone()["content"].startswith("-- "))
                cursor.execute("SELECT COUNT(*) AS count FROM task_rollup_completions")
                self.assertEqual(cursor.fetchone()["count"], 1)


if __name__ == "__main__":
    unittest.main()
