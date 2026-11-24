"""Content-free structured rollup events; no telemetry service or dependency."""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4


LOGGER = logging.getLogger("uvicorn.error.task_rollup")
LOGGER.setLevel(logging.INFO)
_REQUEST_ID = ContextVar("task_rollup_request_id", default=None)


def emit_rollup_event(
    action: str, outcome: str, *, stage: str = "request", project_id: str | None = None,
    operation_id: str | None = None, memo_id: str | None = None, duration_ms: float | None = None,
    memo_count: int | None = None, occurrence_count: int | None = None,
    cause: str | None = None, status: int | None = None, alert: bool = False,
) -> dict:
    """Only these explicit scalar fields can reach logs; never accept payloads."""
    event = {
        "event": "task_rollup", "action": action, "outcome": outcome, "stage": stage,
        "atUtc": datetime.now(timezone.utc).isoformat(), "alert": alert,
    }
    if _REQUEST_ID.get():
        event["requestId"] = _REQUEST_ID.get()
    for key, value in (("projectId", project_id), ("operationId", operation_id), ("memoId", memo_id)):
        if value is not None:
            event[key] = str(value)[:80]
    if duration_ms is not None:
        event["durationMs"] = round(max(0.0, duration_ms), 3)
    if memo_count is not None:
        event["memoCount"] = memo_count
    if occurrence_count is not None:
        event["occurrenceCount"] = occurrence_count
    if cause is not None:
        event["cause"] = re.sub(r"[^a-zA-Z0-9_.-]", "_", cause)[:80]
    if status is not None:
        event["status"] = status
    LOGGER.log(logging.ERROR if alert else logging.INFO, json.dumps(event, sort_keys=True))
    return event


def _result_counts(result: dict) -> tuple[int, int]:
    memos: set[str] = set(result.get("changedMemoIds", []))
    occurrences = 0
    pending = list(result.get("groups", []))
    if isinstance(result.get("group"), dict):
        pending.append(result["group"])
    while pending:
        node = pending.pop()
        for occurrence in node.get("occurrences", []):
            memos.add(occurrence["memoId"])
            occurrences += 1
        pending.extend(node.get("children", []))
    return len(memos), occurrences


class RollupRequestObservation:
    """Use across async waits; perf_counter is independent of fixture/KST clocks."""

    def __init__(self, action: str, project_id: str, operation_id: str | None = None):
        self.action, self.project_id, self.operation_id = action, project_id, operation_id
        self.result: dict = {}
        self.replay = False

    def __enter__(self):
        self.started = perf_counter()
        self.request_token = _REQUEST_ID.set(str(uuid4()))
        return self

    def finish(self, result: dict, *, replay: bool = False) -> dict:
        self.result, self.replay = result, replay
        return result

    def __exit__(self, kind, error, traceback):
        import asyncio
        from fastapi import HTTPException

        outcome, cause, status, alert = "replay" if self.replay else "success", None, 200, False
        if isinstance(error, HTTPException):
            status = error.status_code
            cause = error.detail.get("code") if isinstance(error.detail, dict) else None
            cause = cause if isinstance(cause, str) else f"http_{status}"
            if any(word in cause for word in ("lease", "reservation", "locked")):
                outcome = "lease_block"
            else:
                outcome = "conflict" if status == 409 else "error"
        elif isinstance(error, asyncio.CancelledError):
            outcome, cause, status = "cancelled", "client_cancelled", 499
        elif error is not None:
            cause = type(error).__name__
            outcome, status = "internal_error", 500
            alert = isinstance(error, RuntimeError)
        memos, occurrences = _result_counts(self.result)
        emit_rollup_event(
            self.action, outcome, project_id=self.project_id, operation_id=self.operation_id,
            duration_ms=(perf_counter() - self.started) * 1000, memo_count=memos,
            occurrence_count=occurrences, cause=cause, status=status, alert=alert,
        )
        _REQUEST_ID.reset(self.request_token)
        return False


def summarize_rollup_events(events) -> dict:
    """Read-only log reduction for p50/p95, rates/counters and alert conditions."""
    counts: Counter = Counter()
    durations = defaultdict(list)
    alerts = 0
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "task_rollup":
            continue
        counts[f'{event.get("stage")}:{event.get("outcome")}'] += 1
        alerts += bool(event.get("alert"))
        if event.get("stage") == "request":
            counts["requests"] += 1
            counts["conflicts"] += event.get("status") == 409
            duration = event.get("durationMs")
            if isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0:
                durations[event.get("action", "unknown")].append(duration)
    percentiles = {}
    for action, values in durations.items():
        values.sort()
        percentiles[action] = {
            "count": len(values), "p50Ms": values[math.ceil(len(values) * .5) - 1],
            "p95Ms": values[math.ceil(len(values) * .95) - 1],
        }
    requests = counts["requests"]
    return {
        "requests": requests, "latency": percentiles,
        "conflictRate": counts["conflicts"] / requests if requests else 0.0,
        "leaseBlocks": counts["request:lease_block"],
        "rollbacks": counts["transaction:rollback"],
        "replays": counts["request:replay"] + counts["transaction:replay"],
        "notificationFailures": counts["notification:notification_failure"],
        "schemaRejections": counts["sync:schema_mismatch"], "alerts": alerts,
    }


def rollup_event_lines(lines):
    """Accept Uvicorn/Docker prefixes; ignore unrelated or malformed log lines."""
    for line in lines:
        start = line.find("{")
        if start < 0:
            continue
        try:
            yield json.loads(line[start:])
        except (ValueError, TypeError):
            continue


if __name__ == "__main__":
    import sys
    print(json.dumps(summarize_rollup_events(rollup_event_lines(sys.stdin)), sort_keys=True))
