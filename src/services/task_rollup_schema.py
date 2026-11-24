"""Canonical durable task-rollup receipt validation.

Completion receipts are synchronized metadata, not editable memo content. Their
identity and stored result are immutable; only the undo tuple and tombstone make
one-way transitions. HTTP and sync paths share these guards.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

TASK_ROLLUP_TABLE = "task_rollup_completions"
MAX_COMPLETION_JSON_BYTES = 1024 * 1024
MAX_UNDO_JSON_BYTES = 1024 * 1024
MAX_OCCURRENCES = 4096
MAX_MEMOS = 256
MAX_TREE_DEPTH = 128
MAX_JSON_DEPTH = MAX_TREE_DEPTH * 2 + 16

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEYS = {
    "lease", "leases", "leasetoken", "reservation", "reservationtoken",
    "requestid", "secret", "token",
}
_OCCURRENCE_FIELDS = {
    "key", "memoId", "memoTitle", "revision", "lineIndex", "byteStart",
    "marker", "checked", "text", "physicalPath",
}
_NODE_FIELDS = {
    "key", "kind", "text", "checked", "actionable", "occurrences", "children",
}
_AFTER_MEMO_FIELDS = {"memoId", "revision", "contentHash", "updatedAtUtc"}
_RESULT_FIELDS = {
    "operationId", "projectId", "groupKey", "group", "changedMemoIds",
    "afterManifest", "completedAtUtc", "expiresAtUtc", "status", "canUndo",
}
_RESULT_OPTIONAL_FIELDS = {"context", "memoScope"}
_CONTEXT_FIELDS = {"key", "kind", "text", "checked"}
_UNDO_MUTABLE_RESULT_FIELDS = {"afterManifest", "status", "canUndo"}
_DATA_FIELDS = {
    "result", "matchedOccurrences", "changedOccurrences", "beforeRevisions",
    "afterManifest",
}
_UNDO_DATA_FIELDS = {"result", "afterManifest"}

IMMUTABLE_COMPLETION_COLUMNS = (
    "id", "project_id", "actor_id", "group_key", "payload_hash", "data_json",
    "completed_at_utc", "completion_date_kst",
)
UNDO_COLUMNS = ("undo_operation_id", "undo_payload_hash", "undo_data_json")


class TaskRollupSchemaError(ValueError):
    """Safe validation error; messages never contain stored task text."""


def canonical_json_text(value: Any) -> str:
    """Return canonical JSON without Unicode normalization.

    NFC is a parser comparison rule only. Applying it to opaque persisted JSON would
    change immutable payload hashes for canonically equivalent original text.
    """
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise TaskRollupSchemaError("task rollup payload is not valid JSON") from exc


def canonical_payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_text(value).encode("utf-8")).hexdigest()


def validate_completion_data_json(value: str | dict) -> dict:
    payload, canonical = _load_canonical(value, MAX_COMPLETION_JSON_BYTES, "data_json")
    _exact_fields(payload, _DATA_FIELDS, "data_json")
    _reject_private_runtime_fields(payload)
    result = _validate_result(payload["result"], "data_json.result")
    matched = _validate_occurrences(payload["matchedOccurrences"], "matchedOccurrences")
    changed = _validate_occurrences(payload["changedOccurrences"], "changedOccurrences")
    if canonical_json_text(result["group"]["occurrences"]) != canonical_json_text(matched):
        raise TaskRollupSchemaError("result group occurrences do not match matchedOccurrences")
    expected_changed = [
        item for item in matched if item["marker"] == "-- " and not item["checked"]
    ]
    if canonical_json_text(changed) != canonical_json_text(expected_changed):
        raise TaskRollupSchemaError(
            "changedOccurrences must be exactly the unchecked matched occurrences"
        )
    revisions = payload["beforeRevisions"]
    if not isinstance(revisions, dict) or len(revisions) > MAX_MEMOS:
        raise TaskRollupSchemaError("beforeRevisions is invalid or too large")
    for memo_id, revision in revisions.items():
        _bounded_string(memo_id, "beforeRevisions memo id", 50)
        _bounded_string(revision, "beforeRevisions revision", 128)
    manifest = _validate_after_manifest(payload["afterManifest"], "afterManifest")
    if result["afterManifest"] != manifest:
        raise TaskRollupSchemaError("result afterManifest does not match envelope")
    changed_memo_ids = {item["memoId"] for item in changed}
    if not (
        changed_memo_ids
        == set(result["changedMemoIds"])
        == set(revisions)
        == {item["memoId"] for item in manifest}
    ):
        raise TaskRollupSchemaError("completion memo manifests do not identify the same memos")
    if isinstance(value, str) and value != canonical:
        raise TaskRollupSchemaError("data_json is not canonical JSON")
    return payload


def validate_undo_data_json(value: str | dict) -> dict:
    payload, canonical = _load_canonical(value, MAX_UNDO_JSON_BYTES, "undo_data_json")
    _exact_fields(payload, _UNDO_DATA_FIELDS, "undo_data_json")
    _reject_private_runtime_fields(payload)
    result = _validate_result(payload["result"], "undo_data_json.result")
    manifest = _validate_after_manifest(payload["afterManifest"], "undo afterManifest")
    if result["afterManifest"] != manifest:
        raise TaskRollupSchemaError("undo result afterManifest does not match envelope")
    if set(result["changedMemoIds"]) != {item["memoId"] for item in manifest}:
        raise TaskRollupSchemaError("undo result memo manifest is inconsistent")
    if result["status"] != "undone":
        raise TaskRollupSchemaError("undo result status must be undone")
    if result["canUndo"] is not False:
        raise TaskRollupSchemaError("undo result canUndo must be false")
    if isinstance(value, str) and value != canonical:
        raise TaskRollupSchemaError("undo_data_json is not canonical JSON")
    return payload


def validate_task_rollup_sync_row(
    cursor,
    row_id: str,
    row: dict,
    existing: dict | None = None,
    *,
    expected_actor_id: str | None = None,
) -> dict:
    """Validate a receipt before generic LWW comparison.

    A newer generic snapshot cannot clear a known undo result or tombstone. Those
    one-way fields are copied forward; identity/payload rebinds fail closed.
    """
    normalized = dict(row)
    if "id" in normalized and normalized.get("id") != row_id:
        raise TaskRollupSchemaError("task rollup operation id does not match row id")
    normalized["id"] = row_id
    if existing is not None:
        for column in IMMUTABLE_COMPLETION_COLUMNS:
            normalized.setdefault(column, existing.get(column))
        for column in IMMUTABLE_COMPLETION_COLUMNS:
            if not _db_values_equal(normalized.get(column), existing.get(column)):
                raise TaskRollupSchemaError(f"task rollup {column} is immutable")

    required = set(IMMUTABLE_COMPLETION_COLUMNS) | {"updated_at_utc"}
    missing = sorted(column for column in required if normalized.get(column) is None)
    if missing:
        raise TaskRollupSchemaError(f"task rollup row is missing required fields: {missing}")
    _uuid_string(row_id, "operation id")
    _bounded_string(normalized.get("project_id"), "project_id", 50)
    actor_id = _bounded_string(normalized.get("actor_id"), "actor_id", 50)
    _bounded_string(normalized.get("group_key"), "group_key", 64)
    _hash_string(normalized.get("payload_hash"), "payload_hash")
    if expected_actor_id is None:
        raise TaskRollupSchemaError("task rollup sync requires an authenticated account")
    if actor_id != expected_actor_id:
        raise TaskRollupSchemaError("task rollup actor does not match authenticated account")

    data = validate_completion_data_json(normalized["data_json"])
    result = data["result"]
    if (
        result["operationId"] != row_id
        or result["projectId"] != normalized["project_id"]
        or result["groupKey"] != normalized["group_key"]
    ):
        raise TaskRollupSchemaError("task rollup result identity does not match its row")
    _validate_datetime(normalized.get("completed_at_utc"), "completed_at_utc")
    _validate_date(normalized.get("completion_date_kst"), "completion_date_kst")
    _validate_datetime(normalized.get("updated_at_utc"), "updated_at_utc")
    if normalized.get("deleted_at") is not None:
        _validate_datetime(normalized["deleted_at"], "deleted_at")
    if _utc_naive(result["completedAtUtc"]) != _utc_naive(
        normalized["completed_at_utc"]
    ):
        raise TaskRollupSchemaError(
            "task rollup result completedAtUtc does not match its row"
        )

    incoming_undo = tuple(normalized.get(column) for column in UNDO_COLUMNS)
    present = tuple(value is not None for value in incoming_undo)
    if any(present) and not all(present):
        raise TaskRollupSchemaError("task rollup undo fields must transition together")
    if all(present):
        _uuid_string(str(incoming_undo[0]), "undo operation id")
        _hash_string(incoming_undo[1], "undo_payload_hash")
        undo = validate_undo_data_json(incoming_undo[2])
        if undo["result"]["operationId"] != row_id:
            raise TaskRollupSchemaError("undo result operationId must identify the completion")
        completion_result = data["result"]
        undo_result = undo["result"]
        immutable_fields = (
            set(completion_result) | set(undo_result)
        ) - _UNDO_MUTABLE_RESULT_FIELDS
        for field in sorted(immutable_fields):
            if canonical_json_text(completion_result.get(field)) != canonical_json_text(
                undo_result.get(field)
            ):
                raise TaskRollupSchemaError(
                    f"undo result {field} does not match the completion"
                )

    if existing is not None:
        existing_undo = tuple(existing.get(column) for column in UNDO_COLUMNS)
        existing_present = tuple(value is not None for value in existing_undo)
        if any(existing_present) and not all(existing_present):
            raise TaskRollupSchemaError("stored task rollup undo tuple is inconsistent")
        if all(existing_present):
            if all(present) and not all(
                _db_values_equal(left, right)
                for left, right in zip(incoming_undo, existing_undo, strict=True)
            ):
                raise TaskRollupSchemaError("task rollup undo result is immutable")
            for column, stored in zip(UNDO_COLUMNS, existing_undo, strict=True):
                normalized[column] = stored
        if existing.get("deleted_at") is not None and normalized.get("deleted_at") is None:
            normalized["deleted_at"] = existing["deleted_at"]

    _require_live_actor_scope(cursor, normalized["project_id"], actor_id)
    return normalized


def validate_new_task_rollup_source_dependencies(cursor, row: dict) -> str | None:
    """Validate source memo provenance for a receipt's first arrival.

    Returns the first missing memo id so the sync retry queue can defer the receipt.
    Existing receipts deliberately do not call this function: later source moves or
    soft-deletes make a receipt historical, but must not erase its immutable trail.
    Only identity columns are read; source text is never hydrated from this path.
    """
    data = validate_completion_data_json(row["data_json"])
    memo_ids = completion_source_memo_ids(data)
    if not memo_ids:
        raise TaskRollupSchemaError("task rollup receipt has no source memos")
    placeholders = ", ".join(["%s"] * len(memo_ids))
    cursor.execute(
        f"SELECT id, project_id, updated_at_utc, deleted_at "
        f"FROM memos WHERE id IN ({placeholders})",
        tuple(memo_ids),
    )
    sources = {str(source["id"]): source for source in cursor.fetchall()}
    missing = sorted(set(memo_ids) - set(sources))
    if missing:
        return missing[0]
    after_bounds = {
        str(item["memoId"]): _utc_naive(item["updatedAtUtc"])
        for item in data["afterManifest"]
    }
    completed_bound = _utc_naive(row["completed_at_utc"])
    for memo_id, source in sources.items():
        same_project = str(source["project_id"]) == str(row["project_id"])
        is_deleted = source.get("deleted_at") is not None
        if same_project and not is_deleted:
            continue
        causal_bound = after_bounds.get(memo_id, completed_bound)
        current_clock = max(
            clock
            for clock in (
                _utc_naive(source.get("updated_at_utc")),
                _utc_naive(source.get("deleted_at")),
            )
            if clock is not None
        )
        if current_clock <= causal_bound:
            reason = "deleted" if is_deleted else "moved"
            raise TaskRollupSchemaError(
                f"task rollup source memo {reason} without a later causal clock"
            )
    return None


def completion_source_memo_ids(data: dict) -> tuple[str, ...]:
    memo_ids = set(data["beforeRevisions"])
    for field in ("matchedOccurrences", "changedOccurrences", "afterManifest"):
        memo_ids.update(str(item["memoId"]) for item in data[field])
    pending = [data["result"]["group"]]
    while pending:
        node = pending.pop()
        memo_ids.update(str(item["memoId"]) for item in node["occurrences"])
        pending.extend(node["children"])
    if len(memo_ids) > MAX_MEMOS:
        raise TaskRollupSchemaError("task rollup receipt references too many source memos")
    return tuple(sorted(memo_ids))


def _require_live_actor_scope(cursor, project_id: str, actor_id: str) -> None:
    cursor.execute(
        """
        SELECT p.owner_id, p.parent_id, p.deleted_at,
               parent.id AS parent_exists, parent.deleted_at AS parent_deleted_at,
               pm.id AS member_id, pm.deleted_at AS member_deleted_at
        FROM projects p
        LEFT JOIN projects parent ON parent.id = p.parent_id
        LEFT JOIN project_members pm
               ON pm.project_id = p.id AND pm.user_id = %s
        WHERE p.id = %s
        LIMIT 1
        """,
        (actor_id, project_id),
    )
    project = cursor.fetchone()
    if project is None:
        return  # generic FK dependency handling defers until the project arrives
    live_hierarchy = project.get("deleted_at") is None and (
        project.get("parent_id") is None
        or (project.get("parent_exists") is not None and project.get("parent_deleted_at") is None)
    )
    actor_is_live = project.get("owner_id") == actor_id or (
        project.get("member_id") is not None and project.get("member_deleted_at") is None
    )
    if not live_hierarchy or not actor_is_live:
        raise TaskRollupSchemaError("task rollup actor lacks live project scope")


def _load_canonical(value: str | dict, limit: int, label: str) -> tuple[dict, str]:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > limit:
            raise TaskRollupSchemaError(f"{label} exceeds its size limit")
        try:
            payload = json.loads(value)
        except (RecursionError, TypeError, ValueError) as exc:
            raise TaskRollupSchemaError(f"{label} is not valid JSON") from exc
    elif isinstance(value, dict):
        payload = value
    else:
        raise TaskRollupSchemaError(f"{label} must be a JSON object")
    if not isinstance(payload, dict):
        raise TaskRollupSchemaError(f"{label} must be a JSON object")
    _validate_json_depth(payload)
    canonical = canonical_json_text(payload)
    if len(canonical.encode("utf-8")) > limit:
        raise TaskRollupSchemaError(f"{label} exceeds its size limit")
    return payload, canonical


def _validate_result(value: Any, label: str) -> dict:
    _required_optional_fields(
        value,
        _RESULT_FIELDS,
        _RESULT_OPTIONAL_FIELDS,
        label,
    )
    _uuid_string(value["operationId"], f"{label}.operationId")
    _bounded_string(value["projectId"], f"{label}.projectId", 50)
    _bounded_string(value["groupKey"], f"{label}.groupKey", 64)
    _validate_node(value["group"], f"{label}.group", 0)
    memo_ids = value["changedMemoIds"]
    if not isinstance(memo_ids, list) or len(memo_ids) > MAX_MEMOS:
        raise TaskRollupSchemaError(f"{label}.changedMemoIds is invalid or too large")
    if len(set(memo_ids)) != len(memo_ids):
        raise TaskRollupSchemaError(f"{label}.changedMemoIds contains duplicates")
    for memo_id in memo_ids:
        _bounded_string(memo_id, f"{label}.changedMemoIds", 50)
    value["afterManifest"] = _validate_after_manifest(value["afterManifest"], f"{label}.afterManifest")
    _validate_iso_text(value["completedAtUtc"], f"{label}.completedAtUtc")
    _validate_iso_text(value["expiresAtUtc"], f"{label}.expiresAtUtc")
    if value["status"] not in {"ready", "pending", "superseded", "conflict", "undone"}:
        raise TaskRollupSchemaError(f"{label}.status is invalid")
    if not isinstance(value["canUndo"], bool):
        raise TaskRollupSchemaError(f"{label}.canUndo must be boolean")
    if "context" in value:
        context = value["context"]
        if not isinstance(context, list) or len(context) > MAX_TREE_DEPTH:
            raise TaskRollupSchemaError(f"{label}.context is invalid or too large")
        for index, item in enumerate(context):
            item_label = f"{label}.context[{index}]"
            _exact_fields(item, _CONTEXT_FIELDS, item_label)
            _bounded_string(item["key"], f"{item_label}.key", 64)
            if item["kind"] not in {"task", "description"}:
                raise TaskRollupSchemaError(f"{item_label}.kind is invalid")
            _bounded_string(
                item["text"], f"{item_label}.text", MAX_COMPLETION_JSON_BYTES
            )
            if not isinstance(item["checked"], bool):
                raise TaskRollupSchemaError(f"{item_label}.checked must be boolean")
    if "memoScope" in value and value["memoScope"] not in {"all", "recent30"}:
        raise TaskRollupSchemaError(f"{label}.memoScope is invalid")
    return value


def _validate_node(value: Any, label: str, depth: int) -> None:
    if depth >= MAX_TREE_DEPTH:
        raise TaskRollupSchemaError("task rollup group tree is too deep")
    _exact_fields(value, _NODE_FIELDS, label)
    _bounded_string(value["key"], f"{label}.key", 64)
    if value["kind"] not in {"task", "description"}:
        raise TaskRollupSchemaError(f"{label}.kind is invalid")
    _bounded_string(value["text"], f"{label}.text", MAX_COMPLETION_JSON_BYTES)
    if not isinstance(value["checked"], bool) or not isinstance(value["actionable"], bool):
        raise TaskRollupSchemaError(f"{label} boolean fields are invalid")
    _validate_occurrences(value["occurrences"], f"{label}.occurrences")
    children = value["children"]
    if not isinstance(children, list) or len(children) > MAX_OCCURRENCES:
        raise TaskRollupSchemaError(f"{label}.children is invalid or too large")
    for index, child in enumerate(children):
        _validate_node(child, f"{label}.children[{index}]", depth + 1)


def _validate_occurrences(value: Any, label: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_OCCURRENCES:
        raise TaskRollupSchemaError(f"{label} is invalid or too large")
    keys: set[str] = set()
    for index, occurrence in enumerate(value):
        item_label = f"{label}[{index}]"
        _exact_fields(occurrence, _OCCURRENCE_FIELDS, item_label)
        key = _bounded_string(occurrence["key"], f"{item_label}.key", 512)
        if key in keys:
            raise TaskRollupSchemaError(f"{label} contains duplicate occurrence keys")
        keys.add(key)
        _bounded_string(occurrence["memoId"], f"{item_label}.memoId", 50)
        _bounded_string(occurrence["memoTitle"], f"{item_label}.memoTitle", 255)
        _bounded_string(occurrence["revision"], f"{item_label}.revision", 128)
        if not isinstance(occurrence["lineIndex"], int) or occurrence["lineIndex"] < 0:
            raise TaskRollupSchemaError(f"{item_label}.lineIndex is invalid")
        if not isinstance(occurrence["byteStart"], int) or occurrence["byteStart"] < 0:
            raise TaskRollupSchemaError(f"{item_label}.byteStart is invalid")
        if occurrence["marker"] not in {"-- ", "v-- ", "- "}:
            raise TaskRollupSchemaError(f"{item_label}.marker is invalid")
        if not isinstance(occurrence["checked"], bool):
            raise TaskRollupSchemaError(f"{item_label}.checked must be boolean")
        if occurrence["checked"] != (occurrence["marker"] == "v-- "):
            raise TaskRollupSchemaError(f"{item_label}.checked does not match marker")
        _bounded_string(occurrence["text"], f"{item_label}.text", MAX_COMPLETION_JSON_BYTES)
        path = occurrence["physicalPath"]
        if not isinstance(path, list) or len(path) > MAX_TREE_DEPTH:
            raise TaskRollupSchemaError(f"{item_label}.physicalPath is invalid")
        for part in path:
            _bounded_string(part, f"{item_label}.physicalPath", MAX_COMPLETION_JSON_BYTES)
        if occurrence["key"] != _occurrence_key(occurrence):
            raise TaskRollupSchemaError(f"{item_label}.key does not match occurrence manifest")
    return value


def _occurrence_key(occurrence: dict) -> str:
    # Keep byte-identical with task_rollup._occurrence_key. Text is part of the
    # parser's canonical source-position manifest and prevents a same-position text
    # substitution from retaining a trusted key.
    manifest = [
        occurrence["memoId"],
        occurrence["revision"],
        occurrence["lineIndex"],
        occurrence["byteStart"],
        occurrence["marker"],
        occurrence["text"],
        occurrence["physicalPath"],
    ]
    return hashlib.sha256(canonical_json_text(manifest).encode("utf-8")).hexdigest()


def _validate_after_manifest(value: Any, label: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_MEMOS:
        raise TaskRollupSchemaError(f"{label} is invalid or too large")
    memo_ids: set[str] = set()
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        _exact_fields(item, _AFTER_MEMO_FIELDS, item_label)
        memo_id = _bounded_string(item["memoId"], f"{item_label}.memoId", 50)
        if memo_id in memo_ids:
            raise TaskRollupSchemaError(f"{label} contains duplicate memo ids")
        memo_ids.add(memo_id)
        _bounded_string(item["revision"], f"{item_label}.revision", 128)
        _hash_string(item["contentHash"], f"{item_label}.contentHash")
        _validate_iso_text(item["updatedAtUtc"], f"{item_label}.updatedAtUtc")
    return value


def _exact_fields(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise TaskRollupSchemaError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise TaskRollupSchemaError(
            f"{label} fields do not match the canonical schema "
            f"(missing={sorted(expected - actual)}, unknown={sorted(actual - expected)})"
        )


def _required_optional_fields(
    value: Any,
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    if not isinstance(value, dict):
        raise TaskRollupSchemaError(f"{label} must be an object")
    actual = set(value)
    missing = required - actual
    unknown = actual - required - optional
    if missing or unknown:
        raise TaskRollupSchemaError(
            f"{label} fields do not match the canonical schema "
            f"(missing={sorted(missing)}, unknown={sorted(unknown)})"
        )


def _reject_private_runtime_fields(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                compact = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if compact in _FORBIDDEN_KEYS:
                    raise TaskRollupSchemaError("task rollup payload contains runtime credentials")
                pending.append(child)
        elif isinstance(current, list):
            pending.extend(current)


def _validate_json_depth(value: Any) -> None:
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise TaskRollupSchemaError("task rollup payload is too deeply nested")
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)


def _bounded_string(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise TaskRollupSchemaError(f"{label} is invalid or too long")
    return value


def _hash_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise TaskRollupSchemaError(f"{label} must be a lowercase sha256 digest")
    return value


def _uuid_string(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TaskRollupSchemaError(f"{label} must be a UUID") from exc
    if str(parsed) != value:
        raise TaskRollupSchemaError(f"{label} must use canonical UUID form")
    return value


def _validate_iso_text(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise TaskRollupSchemaError(f"{label} must be an ISO timestamp")
    _validate_datetime(value, label)


def _validate_datetime(value: Any, label: str) -> None:
    if isinstance(value, datetime):
        return
    if not isinstance(value, str) or not value:
        raise TaskRollupSchemaError(f"{label} must be an ISO timestamp")
    try:
        datetime.fromisoformat(value.rstrip("Zz"))
    except ValueError as exc:
        raise TaskRollupSchemaError(f"{label} must be an ISO timestamp") from exc


def _utc_naive(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        if text.endswith(("Z", "z")):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise TaskRollupSchemaError("task rollup causal clock is invalid") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _validate_date(value: Any, label: str) -> None:
    if isinstance(value, datetime):
        raise TaskRollupSchemaError(f"{label} must be a date")
    if isinstance(value, date):
        return
    if not isinstance(value, str):
        raise TaskRollupSchemaError(f"{label} must be an ISO date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise TaskRollupSchemaError(f"{label} must be an ISO date") from exc


def _db_values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, datetime) and isinstance(right, str):
        try:
            right = datetime.fromisoformat(right.rstrip("Zz"))
        except ValueError:
            return False
    elif isinstance(right, datetime) and isinstance(left, str):
        try:
            left = datetime.fromisoformat(left.rstrip("Zz"))
        except ValueError:
            return False
    if isinstance(left, date) and not isinstance(left, datetime) and isinstance(right, str):
        try:
            right = date.fromisoformat(right)
        except ValueError:
            return False
    elif isinstance(right, date) and not isinstance(right, datetime) and isinstance(left, str):
        try:
            left = date.fromisoformat(left)
        except ValueError:
            return False
    return left == right
