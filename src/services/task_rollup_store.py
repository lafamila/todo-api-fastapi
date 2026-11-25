from __future__ import annotations

import copy
import hashlib
import json
import uuid
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from fastapi import HTTPException

try:
    from .task_rollup_observability import emit_rollup_event
    from ..connectors import get_db_connection
    from ..timeutil import KST, UTC, iso_utc
    from ..utils import is_service_owner
    from .memo_revision import memo_revision
    from .task_rollup import (
        RollupValidationError,
        apply_markers,
        build_rollup,
    )
    from .task_rollup_limits import MAX_ROLLUP_MEMOS
    from .task_rollup_schema import (
        TaskRollupSchemaError,
        canonical_json_text,
        validate_completion_data_json,
        validate_undo_data_json,
    )
except ImportError:  # pragma: no cover
    from services.task_rollup_observability import emit_rollup_event
    from connectors import get_db_connection
    from timeutil import KST, UTC, iso_utc
    from utils import is_service_owner
    from services.memo_revision import memo_revision
    from services.task_rollup import (
        RollupValidationError,
        apply_markers,
        build_rollup,
    )
    from services.task_rollup_limits import MAX_ROLLUP_MEMOS
    from services.task_rollup_schema import (
        TaskRollupSchemaError,
        canonical_json_text,
        validate_completion_data_json,
        validate_undo_data_json,
    )


MAX_JSON_BYTES = 1024 * 1024
MEMO_COLUMNS = (
    "id, project_id, created_by, kind, title, content, status, "
    "created_at, updated_at, updated_at_utc, deleted_at"
)


@dataclass(frozen=True)
class RollupWork:
    payload_hash: str
    required_memo_ids: tuple[str, ...]
    replay: dict | None = None


def rollup_now_utc() -> datetime:
    """Feature-local clock seam; returns aware UTC at MySQL millisecond precision."""
    value = datetime.now(UTC)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def _now_pair() -> tuple[datetime, datetime]:
    aware = rollup_now_utc()
    if aware.tzinfo is None:
        aware = aware.replace(tzinfo=UTC)
    aware = aware.astimezone(UTC).replace(
        microsecond=(aware.microsecond // 1000) * 1000
    )
    return aware, aware.replace(tzinfo=None)


def _next_kst_boundary(now: datetime) -> datetime:
    local = now.astimezone(KST)
    tomorrow = local.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=KST).astimezone(UTC)


def _canonical_json(value: object) -> str:
    return canonical_json_text(value)


def _json_size(value: object) -> int:
    return len(_canonical_json(value).encode("utf-8"))


def _ensure_json_limit(value: object, label: str) -> None:
    if _json_size(value) > MAX_JSON_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "task_rollup_payload_too_large", "message": f"{label} exceeds 1 MiB"},
        )


def _normalized_aliases(aliases: list[dict]) -> list[dict]:
    return sorted(
        (
            {
                "seedKey": alias["seedKey"],
                "variantKeys": sorted(alias.get("variantKeys", [])),
            }
            for alias in aliases
        ),
        key=lambda alias: (alias["seedKey"], alias["variantKeys"]),
    )


def completion_payload_hash(
    project_id: str, request: dict, *, _legacy_scope: str = "all"
) -> str:
    """Hash semantic intent only; lease credentials are deliberately excluded."""
    semantic = {
        "aliases": _normalized_aliases(request.get("aliases", [])),
        "corpusDigest": request["corpusDigest"],
        "groupKey": request["groupKey"],
        "occurrenceKeys": sorted(request["occurrenceKeys"]),
        "projectId": project_id,
        # Compatibility-only constant: the removed product selector previously
        # hashed the default `all` value. Keeping it preserves issued receipts.
        "memoScope": _legacy_scope,
    }
    return hashlib.sha256(_canonical_json(semantic).encode("utf-8")).hexdigest()


def undo_payload_hash(
    project_id: str,
    operation_id: str,
    request: dict,
    *,
    _legacy_scope: str = "all",
) -> str:
    semantic = {
        "completionOperationId": operation_id,
        "corpusDigest": request["corpusDigest"],
        "projectId": project_id,
        "memoScope": _legacy_scope,
    }
    return hashlib.sha256(_canonical_json(semantic).encode("utf-8")).hexdigest()


def _accepted_undo_payload_hashes(
    project_id: str,
    operation_id: str,
    request: dict,
    recorded_scope: str,
) -> set[str]:
    requested_scope = request.get("memoScope")
    if requested_scope is not None:
        return {
            undo_payload_hash(
                project_id,
                operation_id,
                request,
                _legacy_scope=requested_scope,
            )
        }
    return {
        undo_payload_hash(project_id, operation_id, request),
        undo_payload_hash(
            project_id,
            operation_id,
            request,
            _legacy_scope=recorded_scope,
        ),
    }


def _content_hash(content: str) -> str:
    # Sync's wire schema NFC-normalizes memo content. A causal proof must survive
    # that representation-only transition without normalizing the local source.
    normalized = unicodedata.normalize("NFC", content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_json_load(raw: str | bytes | None) -> dict:
    if raw is None:
        raise HTTPException(status_code=409, detail={"code": "task_rollup_receipt_invalid"})
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise HTTPException(status_code=409, detail={"code": "task_rollup_receipt_invalid"})
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail={"code": "task_rollup_receipt_invalid"}) from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=409, detail={"code": "task_rollup_receipt_invalid"})
    return value


def _validated_completion_data(raw: str | bytes | None) -> dict:
    value = _safe_json_load(raw)
    try:
        return validate_completion_data_json(value)
    except TaskRollupSchemaError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "task_rollup_receipt_invalid", "message": "Stored task rollup receipt is invalid"},
        ) from exc


def _validated_undo_data(raw: str | bytes | None) -> dict:
    value = _safe_json_load(raw)
    try:
        return validate_undo_data_json(value)
    except TaskRollupSchemaError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "task_rollup_receipt_invalid", "message": "Stored task rollup undo receipt is invalid"},
        ) from exc


def _load_project_scope(cursor, project_id: str, user: dict, *, write: bool) -> dict:
    cursor.execute(
        """
        SELECT p.id, p.parent_id, p.is_group, pm.role
        FROM projects p
        LEFT JOIN projects parent ON parent.id = p.parent_id
        LEFT JOIN project_members pm
          ON pm.project_id = p.id AND pm.user_id = %s AND pm.deleted_at IS NULL
        WHERE p.id = %s AND p.deleted_at IS NULL
          AND (p.parent_id IS NULL OR (parent.id IS NOT NULL AND parent.deleted_at IS NULL))
        LIMIT 1
        """,
        (user["id"], project_id),
    )
    project = cursor.fetchone()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["is_group"]:
        raise HTTPException(status_code=409, detail="Group projects do not have a task rollup")
    role = "owner" if is_service_owner(user) else project.get("role")
    if role is None:
        raise HTTPException(status_code=403, detail="Access denied")
    if write and role not in {"owner", "editor"}:
        raise HTTPException(status_code=403, detail="Access denied")
    return {**project, "role": role}


def _lock_project_scope(
    cursor, project_id: str, user: dict, *, write: bool
) -> dict:
    # Resolve the root, then always lock root -> leaf -> membership.
    cursor.execute(
        "SELECT id, parent_id FROM projects WHERE id = %s LIMIT 1", (project_id,)
    )
    hint = cursor.fetchone()
    if not hint:
        raise HTTPException(status_code=404, detail="Project not found")
    root_id = hint.get("parent_id") or project_id
    cursor.execute(
        "SELECT id, parent_id, is_group, deleted_at FROM projects WHERE id = %s FOR UPDATE",
        (root_id,),
    )
    root = cursor.fetchone()
    if not root or root.get("deleted_at") is not None or root.get("parent_id") is not None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project_id == root_id:
        project = root
    else:
        cursor.execute(
            "SELECT id, parent_id, is_group, deleted_at FROM projects WHERE id = %s FOR UPDATE",
            (project_id,),
        )
        project = cursor.fetchone()
    if (
        not project
        or project.get("deleted_at") is not None
        or project.get("parent_id") not in {None, root_id}
    ):
        raise HTTPException(status_code=404, detail="Project not found")
    if project["is_group"]:
        raise HTTPException(status_code=409, detail="Group projects do not have a task rollup")

    cursor.execute(
        """
        SELECT role FROM project_members
        WHERE project_id = %s AND user_id = %s AND deleted_at IS NULL
        FOR UPDATE
        """,
        (project_id, user["id"]),
    )
    member = cursor.fetchone()
    role = "owner" if is_service_owner(user) else (member or {}).get("role")
    if role is None:
        raise HTTPException(status_code=403, detail="Access denied")
    if write and role not in {"owner", "editor"}:
        raise HTTPException(status_code=403, detail="Access denied")
    return {**project, "role": role}


def _require_write_scope(scope: dict) -> None:
    if scope["role"] not in {"owner", "editor"}:
        raise HTTPException(status_code=403, detail="Access denied")


def _load_corpus(cursor, project_id: str, *, for_update: bool = False) -> list[dict]:
    lock = "FOR UPDATE" if for_update else ""
    index = "FORCE INDEX (idx_memos_rollup_corpus)" if for_update else ""
    cursor.execute(
        f"""
        SELECT {MEMO_COLUMNS}
        FROM memos {index}
        WHERE project_id = %s AND kind = 'memo' AND deleted_at IS NULL
        ORDER BY id
        {lock}
        """,
        (project_id,),
    )
    rows = cursor.fetchall()
    return [
        {
            **row,
            "revision": memo_revision(row),
        }
        for row in rows
    ]


def _load_whole_corpus(
    cursor,
    project_id: str,
    *,
    for_update: bool = False,
) -> tuple[list[dict], int]:
    lock = "FOR UPDATE" if for_update else ""
    index = "FORCE INDEX (idx_memos_rollup_corpus)" if for_update else ""
    cursor.execute(
        f"""
        SELECT id
        FROM memos {index}
        WHERE project_id = %s AND kind = 'memo' AND deleted_at IS NULL
        ORDER BY id LIMIT %s
        {lock}
        """,
        (project_id, MAX_ROLLUP_MEMOS + 1),
    )
    metadata = cursor.fetchall()
    if len(metadata) > MAX_ROLLUP_MEMOS:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "tooManyMemos",
                "message": "task rollup memo limit exceeded",
            },
        )
    rows = _load_corpus(cursor, project_id, for_update=for_update)
    return rows, len(metadata)


def _parser_memos(rows: list[dict]) -> list[dict]:
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "content": row.get("content") or "",
            "revision": row["revision"],
            "updated_at_utc": row.get("updated_at_utc"),
        }
        for row in rows
    ]


def _build(rows: list[dict], aliases: list[dict]) -> dict:
    try:
        return build_rollup(_parser_memos(rows), aliases=aliases)
    except RollupValidationError as exc:
        raise HTTPException(
            status_code=getattr(exc, "status_code", 422),
            detail={
                "code": getattr(exc, "code", "task_rollup_invalid"),
                "message": getattr(exc, "message", str(exc)),
            },
        ) from exc


def _receipt(cursor, operation_id: str) -> dict | None:
    cursor.execute(
        """
        SELECT id, project_id, actor_id, group_key, payload_hash, data_json,
               completed_at_utc, completion_date_kst, undo_operation_id,
               undo_payload_hash, undo_data_json, updated_at_utc, deleted_at
        FROM task_rollup_completions WHERE id = %s LIMIT 1
        """,
        (operation_id,),
    )
    return cursor.fetchone()


def _validate_receipt_identity(row: dict, project_id: str, actor_id: str) -> None:
    if row["project_id"] != project_id or row["actor_id"] != actor_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "task_rollup_operation_conflict", "message": "Operation ID is already in use"},
        )


def _require_live_receipt(row: dict) -> None:
    if row.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="Task rollup completion not found")


def _completion_replay(
    row: dict,
    payload_hash: str,
    compatible_hashes: tuple[str, ...] = (),
    expected_legacy_scope: str | None = None,
) -> dict:
    if row.get("deleted_at") is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "task_rollup_operation_deleted", "message": "Operation is no longer available"},
        )
    data = _validated_completion_data(row["data_json"])
    if (
        expected_legacy_scope is not None
        and data["result"].get("memoScope", "all") != expected_legacy_scope
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "task_rollup_operation_conflict", "message": "Operation scope does not match"},
        )
    recorded_scope = data["result"].get("memoScope", "all")
    if expected_legacy_scope is not None:
        accepted_hashes = set(compatible_hashes)
    else:
        accepted_hashes = {payload_hash}
        if recorded_scope == "recent30":
            accepted_hashes.update(compatible_hashes)
    if row["payload_hash"] not in accepted_hashes:
        raise HTTPException(
            status_code=409,
            detail={"code": "task_rollup_operation_conflict", "message": "Operation payload does not match"},
        )
    return data["result"]


def _group_occurrences(group: dict) -> list[dict]:
    occurrences = group.get("occurrences")
    if not isinstance(occurrences, list):
        raise HTTPException(status_code=409, detail={"code": "task_rollup_manifest_changed"})
    return occurrences


def _find_group_context(groups: list[dict], group_key: str) -> tuple[dict | None, list[dict]]:
    stack: list[tuple[dict, tuple[dict, ...]]] = [
        (node, ()) for node in reversed(groups)
    ]
    while stack:
        node, ancestors = stack.pop()
        if node.get("key") == group_key:
            return node, list(ancestors)
        summary = {
            "key": node["key"],
            "kind": node["kind"],
            "text": node["text"],
            "checked": bool(node["checked"]),
        }
        child_ancestors = (*ancestors, summary)
        stack.extend(
            (child, child_ancestors)
            for child in reversed(node.get("children", []))
        )
    return None, []


def _validate_selection(
    rollup: dict, request: dict
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    if rollup["corpusDigest"] != request["corpusDigest"]:
        raise HTTPException(status_code=409, detail={"code": "task_rollup_corpus_changed"})
    group, context = _find_group_context(rollup.get("groups", []), request["groupKey"])
    if not group or group.get("kind") != "task":
        raise HTTPException(status_code=409, detail={"code": "task_rollup_group_changed"})
    occurrences = _group_occurrences(group)
    actual_keys = [item["key"] for item in occurrences]
    if set(actual_keys) != set(request["occurrenceKeys"]) or len(actual_keys) != len(request["occurrenceKeys"]):
        raise HTTPException(status_code=409, detail={"code": "task_rollup_manifest_changed"})
    changed = [item for item in occurrences if item.get("marker") == "-- "]
    if not changed:
        raise HTTPException(status_code=409, detail={"code": "task_rollup_already_complete"})
    return group, occurrences, changed, context


def _apply(content: str, occurrences: list[dict], *, checked: bool) -> str:
    try:
        return apply_markers(content, occurrences, checked=checked)
    except RollupValidationError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "task_rollup_source_changed", "message": "Task source changed"},
        ) from exc


def _lease_memo_ids(request: dict) -> tuple[str, ...]:
    return tuple(sorted(lease["memoId"] for lease in request.get("leases", [])))


def _validate_lease_manifest(required: tuple[str, ...], request: dict) -> None:
    supplied = _lease_memo_ids(request)
    if supplied != required:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "task_rollup_lease_manifest_changed",
                "requiredMemoIds": list(required),
            },
        )


def prepare_completion(project_id: str, user: dict, request: dict) -> RollupWork:
    _ensure_json_limit(request, "request")
    payload_hash = completion_payload_hash(project_id, request)
    legacy_scope = request.get("memoScope")
    compatibility_scope = legacy_scope or "recent30"
    compatible_hashes = (
        completion_payload_hash(
            project_id, request, _legacy_scope=compatibility_scope
        ),
    )
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _load_project_scope(cursor, project_id, user, write=False)
            row = _receipt(cursor, request["operationId"])
            if row:
                _validate_receipt_identity(row, project_id, user["id"])
                return RollupWork(
                    payload_hash,
                    (),
                    _completion_replay(
                        row,
                        payload_hash,
                        compatible_hashes,
                        expected_legacy_scope=legacy_scope,
                    ),
                )
            if legacy_scope is not None:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "task_rollup_scope_removed", "message": "memoScope is accepted only for replaying an issued operation"},
                )
            _load_project_scope(cursor, project_id, user, write=True)
            rows, _total = _load_whole_corpus(cursor, project_id)
    rollup = _build(rows, request.get("aliases", []))
    _group, _matched, changed, _context = _validate_selection(rollup, request)
    required = tuple(sorted({item["memoId"] for item in changed}))
    _validate_lease_manifest(required, request)
    return RollupWork(payload_hash, required)


def _failure_point(_stage: str) -> None:
    """Test-only patch seam for transaction rollback assertions."""


def _ensure_reservation(validate_reservation: Callable[[], bool]) -> None:
    if not validate_reservation():
        raise HTTPException(
            status_code=409,
            detail={"code": "task_rollup_reservation_lost", "message": "Memo lease reservation was lost"},
        )


def _configure_write_transaction(cursor) -> None:
    """Bound lock waits and make the indexed corpus read a next-key/gap fence."""
    cursor.execute("SET SESSION innodb_lock_wait_timeout = 5")
    cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")


def _raise_bounded_db_conflict(exc: BaseException) -> None:
    args = getattr(exc, "args", ())
    if args and args[0] in {1020, 1205, 1213}:
        raise HTTPException(
            status_code=409,
            detail={"code": "task_rollup_lock_conflict", "message": "Task sources changed concurrently"},
        ) from exc


def _version_memo(
    cursor,
    memo: dict,
    now_local: datetime,
    now_utc: datetime,
    validate_reservation: Callable[[], bool],
) -> None:
    if not memo.get("content"):
        return
    cursor.execute(
        "SELECT COALESCE(MAX(version), 0) AS max_version FROM memo_versions WHERE memo_id = %s",
        (memo["id"],),
    )
    _ensure_reservation(validate_reservation)
    next_version = cursor.fetchone()["max_version"] + 1
    cursor.execute(
        """
        INSERT INTO memo_versions
            (id, memo_id, content, version, note, created_at, updated_at_utc)
        VALUES (%s, %s, %s, %s, NULL, %s, %s)
        """,
        (str(uuid.uuid4()), memo["id"], memo["content"], next_version, now_local, now_utc),
    )
    _ensure_reservation(validate_reservation)
    _failure_point(f"version:{memo['id']}")


def _after_memo(memo: dict) -> dict:
    return {
        "memoId": memo["id"],
        "revision": memo_revision(memo),
        "contentHash": _content_hash(memo.get("content") or ""),
        "updatedAtUtc": iso_utc(memo["updated_at_utc"]),
    }


def _checked_group(group: dict) -> dict:
    result = copy.deepcopy(group)
    result["checked"] = True
    result["actionable"] = False
    return result


def commit_completion(
    project_id: str,
    user: dict,
    request: dict,
    work: RollupWork,
    validate_reservation: Callable[[], bool],
    final_validate_reservation: Callable[[], bool] | None = None,
) -> dict:
    final_validate_reservation = final_validate_reservation or validate_reservation
    legacy_scope = request.get("memoScope")
    compatibility_scope = legacy_scope or "recent30"
    compatible_hashes = (
        completion_payload_hash(
            project_id, request, _legacy_scope=compatibility_scope
        ),
    )
    now, now_naive = _now_pair()
    now_local = now.astimezone(KST).replace(tzinfo=None)
    expires = _next_kst_boundary(now)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            try:
                _configure_write_transaction(cursor)
                scope = _lock_project_scope(cursor, project_id, user, write=False)
                _ensure_reservation(validate_reservation)
                existing = _receipt(cursor, request["operationId"])
                if existing:
                    _validate_receipt_identity(existing, project_id, user["id"])
                    result = _completion_replay(
                        existing,
                        work.payload_hash,
                        compatible_hashes,
                        expected_legacy_scope=legacy_scope,
                    )
                    _ensure_reservation(final_validate_reservation)
                    conn.commit()
                    emit_rollup_event("complete", "replay", stage="transaction", project_id=project_id,
                                      operation_id=request["operationId"])
                    return result

                if legacy_scope is not None:
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "task_rollup_scope_removed", "message": "memoScope is accepted only for replaying an issued operation"},
                    )

                _require_write_scope(scope)

                rows, _total = _load_whole_corpus(
                    cursor, project_id, for_update=True
                )
                _ensure_reservation(validate_reservation)
                rollup = _build(rows, request.get("aliases", []))
                group, matched, changed, context = _validate_selection(rollup, request)
                required = tuple(sorted({item["memoId"] for item in changed}))
                if required != work.required_memo_ids:
                    raise HTTPException(status_code=409, detail={"code": "task_rollup_manifest_changed"})
                _validate_lease_manifest(required, request)

                by_id = {memo["id"]: memo for memo in rows}
                occurrences_by_memo: dict[str, list[dict]] = {}
                for occurrence in matched:
                    occurrences_by_memo.setdefault(occurrence["memoId"], []).append(occurrence)

                after_manifest: list[dict] = []
                for memo_id in required:
                    memo = by_id[memo_id]
                    new_content = _apply(
                        memo.get("content") or "", occurrences_by_memo[memo_id], checked=True
                    )
                    if new_content == memo.get("content"):
                        raise HTTPException(status_code=409, detail={"code": "task_rollup_source_changed"})
                    _version_memo(
                        cursor, memo, now_local, now_naive, validate_reservation
                    )
                    cursor.execute(
                        "UPDATE memos SET content = %s, updated_at = %s, updated_at_utc = %s WHERE id = %s",
                        (new_content, now_local, now_naive, memo_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("task rollup memo update changed an unexpected row count")
                    _ensure_reservation(validate_reservation)
                    _failure_point(f"memo:{memo_id}")
                    memo = {**memo, "content": new_content, "updated_at": now_local, "updated_at_utc": now_naive}
                    after_manifest.append(_after_memo(memo))

                result = {
                    "operationId": request["operationId"],
                    "projectId": project_id,
                    "groupKey": request["groupKey"],
                    "group": _checked_group(group),
                    "context": context,
                    "changedMemoIds": list(required),
                    "afterManifest": after_manifest,
                    "completedAtUtc": iso_utc(now_naive),
                    "expiresAtUtc": iso_utc(expires),
                    "status": "ready",
                    "canUndo": True,
                }
                data = {
                    "result": result,
                    "matchedOccurrences": matched,
                    "changedOccurrences": changed,
                    "beforeRevisions": {memo_id: by_id[memo_id]["revision"] for memo_id in required},
                    "afterManifest": after_manifest,
                }
                _ensure_json_limit(data, "completion result")
                try:
                    validate_completion_data_json(data)
                except TaskRollupSchemaError as exc:
                    raise RuntimeError("generated task rollup receipt is invalid") from exc
                cursor.execute(
                    """
                    INSERT INTO task_rollup_completions
                        (id, project_id, actor_id, group_key, payload_hash, data_json,
                         completed_at_utc, completion_date_kst, updated_at_utc, deleted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    """,
                    (
                        request["operationId"], project_id, user["id"], request["groupKey"],
                        work.payload_hash, _canonical_json(data), now_naive,
                        now.astimezone(KST).date(), now_naive,
                    ),
                )
                _ensure_reservation(validate_reservation)
                _failure_point("completion")
                _ensure_reservation(final_validate_reservation)
                conn.commit()
                return result
            except BaseException as exc:
                conn.rollback()
                emit_rollup_event("complete", "rollback", stage="transaction", project_id=project_id,
                                  operation_id=request["operationId"], cause=type(exc).__name__)
                _raise_bounded_db_conflict(exc)
                raise


def _manifest_assessment(
    manifest: list[dict],
    current: dict[str, dict],
    source_states: dict[str, str] | None = None,
) -> tuple[str, bool]:
    state = "ready"
    exact = True
    for expected in manifest:
        memo = current.get(expected["memoId"])
        if memo is None:
            exact = False
            source_state = (source_states or {}).get(expected["memoId"], "pending")
            if source_state == "superseded":
                state = "superseded"
            elif state != "superseded":
                state = "pending"
            continue
        actual = _after_memo(memo)
        if actual == expected:
            continue
        exact = False
        actual_clock = memo.get("updated_at_utc")
        expected_raw = expected.get("updatedAtUtc") or ""
        try:
            expected_clock = datetime.fromisoformat(expected_raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return "conflict", False
        if actual_clock and actual_clock > expected_clock:
            state = "superseded"
        elif actual_clock == expected_clock:
            # Equal clock + equal NFC content is the same synced source bytes in
            # the wire schema, though it is intentionally not safe for local undo.
            if actual["contentHash"] != expected.get("contentHash"):
                return "conflict", False
        elif state != "superseded":
            state = "pending"
    return state, exact


def _manifest_state(manifest: list[dict], current: dict[str, dict]) -> str:
    return _manifest_assessment(manifest, current)[0]


def _missing_source_states(
    cursor, project_id: str, missing_ids: set[str]
) -> dict[str, str]:
    """Classify receipt sources without hydrating inaccessible text or titles."""
    states = {memo_id: "pending" for memo_id in missing_ids}
    ordered = sorted(missing_ids)
    for offset in range(0, len(ordered), 256):
        chunk = ordered[offset : offset + 256]
        placeholders = ", ".join(["%s"] * len(chunk))
        cursor.execute(
            f"""
            SELECT id, project_id, kind, deleted_at, updated_at_utc
            FROM memos
            WHERE id IN ({placeholders})
            """,
            tuple(chunk),
        )
        for row in cursor.fetchall():
            if (
                row.get("project_id") != project_id
                or row.get("kind") != "memo"
                or row.get("deleted_at") is not None
            ):
                states[row["id"]] = "superseded"
            # A live normal row in this project would be present in the locked/read
            # corpus snapshot. Treat any unexpected omission as pending, never as
            # evidence that inaccessible source text should be hydrated.
    return states


def _load_receipt_sources(
    cursor, project_id: str, memo_ids: set[str], *, for_update: bool = False
) -> dict[str, dict]:
    """Hydrate only live, same-project sources explicitly referenced by receipts."""
    result: dict[str, dict] = {}
    ordered = sorted(memo_ids)
    for offset in range(0, len(ordered), 256):
        chunk = ordered[offset : offset + 256]
        placeholders = ", ".join(["%s"] * len(chunk))
        lock = "FOR UPDATE" if for_update else ""
        cursor.execute(
            f"""
            SELECT {MEMO_COLUMNS}
            FROM memos
            WHERE project_id = %s AND kind = 'memo' AND deleted_at IS NULL
              AND id IN ({placeholders})
            {lock}
            """,
            (project_id, *chunk),
        )
        for row in cursor.fetchall():
            result[row["id"]] = {**row, "revision": memo_revision(row)}
    return result


def _visible_completions(
    cursor,
    project_id: str,
    user: dict,
    rows: list[dict],
    now: datetime,
) -> list[dict]:
    cursor.execute(
        """
        SELECT id, data_json, undo_data_json
        FROM task_rollup_completions
        WHERE project_id = %s AND actor_id = %s AND completion_date_kst = %s
          AND deleted_at IS NULL
        ORDER BY completed_at_utc, id
        """,
        (project_id, user["id"], now.astimezone(KST).date()),
    )
    stored_rows = cursor.fetchall()
    current = {row["id"]: row for row in rows}
    decoded: list[tuple[dict, dict, dict | None]] = []
    missing_ids: set[str] = set()
    for stored in stored_rows:
        data = _validated_completion_data(stored["data_json"])
        undo = (
            _validated_undo_data(stored["undo_data_json"])
            if stored.get("undo_data_json")
            else None
        )
        decoded.append((stored, data, undo))
        active_manifest = (
            undo.get("afterManifest", []) if undo is not None
            else data.get("afterManifest", [])
        )
        missing_ids.update(
            item["memoId"]
            for item in active_manifest
            if item["memoId"] not in current
        )
    current.update(_load_receipt_sources(cursor, project_id, missing_ids))
    still_missing = missing_ids - set(current)
    source_states = _missing_source_states(cursor, project_id, still_missing)

    completions = []
    for stored, data, undo in decoded:
        result = copy.deepcopy(data["result"])
        if undo is not None:
            result = copy.deepcopy(undo["result"])
            status, _exact = _manifest_assessment(
                undo.get("afterManifest", []), current, source_states
            )
            result["status"] = "undone" if status == "ready" else status
            result["canUndo"] = False
        else:
            status, exact = _manifest_assessment(
                data.get("afterManifest", []), current, source_states
            )
            result["status"] = status
            result["canUndo"] = status == "ready" and exact and result.get("canUndo", False)
        completions.append(result)
    return completions


def preview(
    project_id: str,
    user: dict,
    aliases: list[dict],
) -> dict:
    _ensure_json_limit({"aliases": aliases}, "request")
    now, _now_naive = _now_pair()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            scope = _load_project_scope(cursor, project_id, user, write=False)
            rows, _total_count = _load_whole_corpus(cursor, project_id)
            completions = _visible_completions(
                cursor, project_id, user, rows, now
            )
    rollup = _build(rows, aliases)
    result = {
        "projectId": project_id,
        "corpusDigest": rollup["corpusDigest"],
        "serverTimeUtc": iso_utc(now),
        "nextBoundaryUtc": iso_utc(_next_kst_boundary(now)),
        "canComplete": scope["role"] in {"owner", "editor"},
        "groups": rollup["groups"],
        "candidates": rollup["candidates"],
        "aliases": rollup["aliases"],
        "completions": completions,
    }
    _ensure_json_limit(result, "preview result")
    return result


def prepare_undo(project_id: str, operation_id: str, user: dict, request: dict) -> RollupWork:
    _ensure_json_limit(request, "request")
    payload_hash = undo_payload_hash(project_id, operation_id, request)
    now, _ = _now_pair()
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _load_project_scope(cursor, project_id, user, write=False)
            row = _receipt(cursor, operation_id)
            if not row:
                raise HTTPException(status_code=404, detail="Task rollup completion not found")
            _validate_receipt_identity(row, project_id, user["id"])
            _require_live_receipt(row)
            if row.get("undo_operation_id"):
                replay_result = _validated_undo_data(row["undo_data_json"])["result"]
                requested_legacy_scope = request.get("memoScope")
                recorded_scope = replay_result.get("memoScope", "all")
                if (
                    requested_legacy_scope is not None
                    and requested_legacy_scope != recorded_scope
                ):
                    raise HTTPException(status_code=409, detail={"code": "task_rollup_undo_conflict"})
                if row["undo_operation_id"] != request["undoOperationId"]:
                    raise HTTPException(status_code=409, detail={"code": "task_rollup_undo_conflict"})
                if row["undo_payload_hash"] not in _accepted_undo_payload_hashes(
                    project_id, operation_id, request, recorded_scope
                ):
                    raise HTTPException(status_code=409, detail={"code": "task_rollup_undo_conflict"})
                return RollupWork(payload_hash, (), replay_result)
            _load_project_scope(cursor, project_id, user, write=True)
            if row["completion_date_kst"] != now.astimezone(KST).date():
                raise HTTPException(status_code=409, detail={"code": "task_rollup_undo_expired"})
            data = _validated_completion_data(row["data_json"])
            if (
                request.get("memoScope") is not None
                and request["memoScope"] != data["result"].get("memoScope", "all")
            ):
                raise HTTPException(status_code=409, detail={"code": "task_rollup_undo_conflict"})
            if request.get("memoScope") is not None:
                payload_hash = undo_payload_hash(
                    project_id,
                    operation_id,
                    request,
                    _legacy_scope=request["memoScope"],
                )
            required = tuple(sorted({item["memoId"] for item in data.get("changedOccurrences", [])}))
    _validate_lease_manifest(required, request)
    return RollupWork(payload_hash, required)


def commit_undo(
    project_id: str,
    operation_id: str,
    user: dict,
    request: dict,
    work: RollupWork,
    validate_reservation: Callable[[], bool],
    final_validate_reservation: Callable[[], bool] | None = None,
) -> dict:
    final_validate_reservation = final_validate_reservation or validate_reservation
    now, now_naive = _now_pair()
    now_local = now.astimezone(KST).replace(tzinfo=None)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            try:
                _configure_write_transaction(cursor)
                scope = _lock_project_scope(cursor, project_id, user, write=False)
                _ensure_reservation(validate_reservation)
                row = _receipt(cursor, operation_id)
                if not row:
                    raise HTTPException(status_code=404, detail="Task rollup completion not found")
                _validate_receipt_identity(row, project_id, user["id"])
                _require_live_receipt(row)
                if row.get("undo_operation_id"):
                    result = _validated_undo_data(row["undo_data_json"])["result"]
                    if (
                        request.get("memoScope") is not None
                        and request["memoScope"] != result.get("memoScope", "all")
                    ):
                        raise HTTPException(status_code=409, detail={"code": "task_rollup_undo_conflict"})
                    if (
                        row["undo_operation_id"] != request["undoOperationId"]
                        or row["undo_payload_hash"]
                        not in _accepted_undo_payload_hashes(
                            project_id,
                            operation_id,
                            request,
                            result.get("memoScope", "all"),
                        )
                    ):
                        raise HTTPException(status_code=409, detail={"code": "task_rollup_undo_conflict"})
                    _ensure_reservation(final_validate_reservation)
                    conn.commit()
                    emit_rollup_event("undo", "replay", stage="transaction", project_id=project_id,
                                      operation_id=request["undoOperationId"])
                    return result
                _require_write_scope(scope)
                if row["completion_date_kst"] != now.astimezone(KST).date():
                    raise HTTPException(status_code=409, detail={"code": "task_rollup_undo_expired"})

                completion_data = _validated_completion_data(row["data_json"])
                if (
                    request.get("memoScope") is not None
                    and request["memoScope"]
                    != completion_data["result"].get("memoScope", "all")
                ):
                    raise HTTPException(status_code=409, detail={"code": "task_rollup_undo_conflict"})
                rows, _total = _load_whole_corpus(
                    cursor, project_id, for_update=True
                )
                _ensure_reservation(validate_reservation)
                rollup = _build(rows, [])
                if rollup["corpusDigest"] != request["corpusDigest"]:
                    raise HTTPException(status_code=409, detail={"code": "task_rollup_corpus_changed"})
                current = {memo["id"]: memo for memo in rows}
                receipt_ids = {
                    item["memoId"]
                    for item in completion_data.get("afterManifest", [])
                    if item["memoId"] not in current
                }
                current.update(
                    _load_receipt_sources(
                        cursor, project_id, receipt_ids, for_update=True
                    )
                )
                manifest_state, exact_manifest = _manifest_assessment(
                    completion_data.get("afterManifest", []), current
                )
                if manifest_state != "ready" or not exact_manifest:
                    raise HTTPException(status_code=409, detail={"code": "task_rollup_after_manifest_changed"})
                changed = completion_data.get("changedOccurrences", [])
                required = tuple(sorted({item["memoId"] for item in changed}))
                if required != work.required_memo_ids:
                    raise HTTPException(status_code=409, detail={"code": "task_rollup_manifest_changed"})
                _validate_lease_manifest(required, request)

                occurrences_by_memo: dict[str, list[dict]] = {}
                for occurrence in changed:
                    occurrences_by_memo.setdefault(occurrence["memoId"], []).append(occurrence)
                after_manifest: list[dict] = []
                for memo_id in required:
                    memo = current[memo_id]
                    new_content = _apply(
                        memo.get("content") or "", occurrences_by_memo[memo_id], checked=False
                    )
                    if new_content == memo.get("content"):
                        raise HTTPException(status_code=409, detail={"code": "task_rollup_source_changed"})
                    _version_memo(
                        cursor, memo, now_local, now_naive, validate_reservation
                    )
                    cursor.execute(
                        "UPDATE memos SET content = %s, updated_at = %s, updated_at_utc = %s WHERE id = %s",
                        (new_content, now_local, now_naive, memo_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("task rollup undo changed an unexpected row count")
                    _ensure_reservation(validate_reservation)
                    _failure_point(f"undo-memo:{memo_id}")
                    memo = {**memo, "content": new_content, "updated_at": now_local, "updated_at_utc": now_naive}
                    after_manifest.append(_after_memo(memo))

                result = copy.deepcopy(completion_data["result"])
                result["afterManifest"] = after_manifest
                result["status"] = "undone"
                result["canUndo"] = False
                undo_data = {"result": result, "afterManifest": after_manifest}
                _ensure_json_limit(undo_data, "undo result")
                try:
                    validate_undo_data_json(undo_data)
                except TaskRollupSchemaError as exc:
                    raise RuntimeError("generated task rollup undo receipt is invalid") from exc
                cursor.execute(
                    """
                    UPDATE task_rollup_completions
                    SET undo_operation_id = %s, undo_payload_hash = %s,
                        undo_data_json = %s, updated_at_utc = %s
                    WHERE id = %s AND undo_operation_id IS NULL
                    """,
                    (
                        request["undoOperationId"], work.payload_hash,
                        _canonical_json(undo_data), now_naive, operation_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("task rollup undo receipt changed concurrently")
                _ensure_reservation(validate_reservation)
                _failure_point("undo-receipt")
                _ensure_reservation(final_validate_reservation)
                conn.commit()
                return result
            except BaseException as exc:
                conn.rollback()
                emit_rollup_event("undo", "rollback", stage="transaction", project_id=project_id,
                                  operation_id=request["undoOperationId"], cause=type(exc).__name__)
                _raise_bounded_db_conflict(exc)
                raise


def load_changed_memos(
    project_id: str, user: dict, memo_ids: list[str]
) -> list[dict]:
    """Load authoritative post-commit content for access-revalidated room refresh."""
    ids = tuple(sorted(set(memo_ids)))
    if not ids:
        return []
    if len(ids) > MAX_ROLLUP_MEMOS:
        raise RuntimeError("task rollup changed memo set exceeds its limit")
    placeholders = ", ".join(["%s"] * len(ids))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            _load_project_scope(cursor, project_id, user, write=False)
            cursor.execute(
                f"""
                SELECT id, title, content, updated_at_utc
                FROM memos
                WHERE project_id = %s AND kind = 'memo' AND deleted_at IS NULL
                  AND id IN ({placeholders})
                ORDER BY id
                """,
                (project_id, *ids),
            )
            rows = cursor.fetchall()
    if tuple(row["id"] for row in rows) != ids:
        raise RuntimeError("task rollup changed memo became inaccessible")
    return [
        {
            "memoId": row["id"],
            "title": row["title"],
            "content": row.get("content") or "",
            "updatedAtUtc": iso_utc(row.get("updated_at_utc")),
        }
        for row in rows
    ]
