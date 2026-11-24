import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path

from fastapi import HTTPException

from src.services.task_rollup_observability import (
    LOGGER,
    RollupRequestObservation,
    emit_rollup_event,
    summarize_rollup_events,
)


class RollupObservabilityTests(unittest.TestCase):
    def test_request_counts_latency_and_replay_never_log_content(self):
        result = {
            "changedMemoIds": ["memo-a"],
            "group": {"text": "PRIVATE TASK TEXT", "occurrences": [{"memoId": "memo-a"}], "children": []},
        }
        with self.assertLogs(LOGGER.name, level="INFO") as captured:
            with RollupRequestObservation("complete", "project-a", "operation-a") as observed:
                observed.finish(result, replay=True)
        event = json.loads(captured.records[0].getMessage())
        self.assertEqual(event["outcome"], "replay")
        self.assertEqual(event["memoCount"], 1)
        self.assertEqual(event["occurrenceCount"], 1)
        self.assertGreaterEqual(event["durationMs"], 0)
        self.assertIn("requestId", event)
        self.assertNotIn("PRIVATE TASK TEXT", json.dumps(event))

    def test_conflict_lease_and_invariant_alerts_are_content_free(self):
        failures = [
            (HTTPException(409, {"code": "task_rollup_source_changed", "message": "PRIVATE CONTENT"}), "conflict", False),
            (HTTPException(409, {"code": "reservation_fence_lost", "token": "SECRET"}), "lease_block", False),
            (RuntimeError("PRIVATE INVARIANT BODY"), "internal_error", True),
        ]
        for failure, outcome, alert in failures:
            with self.subTest(outcome=outcome), self.assertLogs(LOGGER.name, level="INFO") as captured:
                with self.assertRaises(type(failure)):
                    with RollupRequestObservation("undo", "p", "operation"):
                        raise failure
            event = json.loads(captured.records[0].getMessage())
            self.assertEqual(event["outcome"], outcome)
            self.assertEqual(event["alert"], alert)
            self.assertNotIn("PRIVATE", captured.records[0].getMessage())
            self.assertNotIn("SECRET", captured.records[0].getMessage())

    def test_metrics_and_alert_reduction(self):
        events = [
            {"event": "task_rollup", "stage": "request", "action": "preview", "outcome": "success", "durationMs": duration, "status": 200}
            for duration in [1, 2, 100]
        ]
        events += [
            {"event": "task_rollup", "stage": "request", "outcome": "lease_block", "status": 409},
            {"event": "task_rollup", "stage": "request", "outcome": "replay", "status": 200},
            {"event": "task_rollup", "stage": "transaction", "outcome": "rollback"},
            {"event": "task_rollup", "stage": "notification", "outcome": "notification_failure", "alert": True},
            {"event": "task_rollup", "stage": "sync", "outcome": "schema_mismatch", "alert": True},
        ]
        metrics = summarize_rollup_events(events)
        self.assertEqual(metrics["requests"], 5)
        self.assertEqual(metrics["latency"]["preview"], {"count": 3, "p50Ms": 2, "p95Ms": 100})
        self.assertEqual(metrics["conflictRate"], .2)
        for key in ("leaseBlocks", "rollbacks", "replays", "notificationFailures", "schemaRejections"):
            self.assertEqual(metrics[key], 1)
        self.assertEqual(metrics["alerts"], 2)

    def test_read_only_metrics_cli_accepts_prefixed_logs(self):
        script = Path(__file__).parents[1] / "scripts" / "task_rollup_metrics.py"
        payload = 'INFO: {"event":"task_rollup","stage":"request","action":"preview","outcome":"success","status":200,"durationMs":4}\nother noise\n'
        result = subprocess.run([sys.executable, str(script)], input=payload, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["latency"]["preview"]["p95Ms"], 4)


class RollupRequestContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_id_propagates_into_sql_worker_without_global_state(self):
        with self.assertLogs(LOGGER.name, level="INFO") as captured:
            with RollupRequestObservation("complete", "p", "operation") as observed:
                await asyncio.to_thread(emit_rollup_event, "complete", "rollback", stage="transaction", operation_id="operation")
                observed.finish({})
        events = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual(events[0]["requestId"], events[1]["requestId"])
        self.assertEqual(events[0]["stage"], "transaction")
        with self.assertLogs(LOGGER.name, level="INFO") as next_capture:
            emit_rollup_event("sync", "schema_mismatch", stage="sync")
        self.assertNotIn("requestId", json.loads(next_capture.records[0].getMessage()))
