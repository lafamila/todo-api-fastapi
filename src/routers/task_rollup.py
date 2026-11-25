from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

try:
    from ..services.task_rollup_observability import RollupRequestObservation, emit_rollup_event
    from ..auth_utils import get_current_user
    from ..models.task_rollup import (
        TaskRollupCompleteRequest,
        TaskRollupPreviewRequest,
        TaskRollupUndoRequest,
    )
    from ..services.realtime import get_realtime_server
    from ..services.task_rollup_store import (
        MAX_JSON_BYTES,
        commit_completion,
        commit_undo,
        load_changed_memos,
        prepare_completion,
        prepare_undo,
        preview,
    )
except ImportError:  # pragma: no cover
    from services.task_rollup_observability import RollupRequestObservation, emit_rollup_event
    from auth_utils import get_current_user
    from models.task_rollup import (
        TaskRollupCompleteRequest,
        TaskRollupPreviewRequest,
        TaskRollupUndoRequest,
    )
    from services.realtime import get_realtime_server
    from services.task_rollup_store import (
        MAX_JSON_BYTES,
        commit_completion,
        commit_undo,
        load_changed_memos,
        prepare_completion,
        prepare_undo,
        preview,
    )


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["task-rollup"])
_ROLLUP_CAPACITY = asyncio.BoundedSemaphore(2)


def _release_capacity(task: asyncio.Task) -> None:
    _ROLLUP_CAPACITY.release()
    if not task.cancelled():
        task.exception()


async def _bounded_worker(function, *args):
    if _ROLLUP_CAPACITY.locked():
        raise HTTPException(
            status_code=429,
            detail={"code": "task_rollup_busy", "message": "Task rollup is busy"},
            headers={"Retry-After": "1"},
        )
    await _ROLLUP_CAPACITY.acquire()
    try:
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
    except BaseException:
        _ROLLUP_CAPACITY.release()
        raise
    worker.add_done_callback(_release_capacity)
    return await asyncio.shield(worker)


def _request_dict(model) -> dict:
    value = model.model_dump()
    if len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ) > MAX_JSON_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "task_rollup_payload_too_large", "message": "Request exceeds 1 MiB"},
        )
    return value


async def _reserve(user_id: str, operation_id: str, leases: list[dict]):
    realtime = get_realtime_server()
    if realtime is None or not realtime.available or not hasattr(realtime, "reserve_task_rollup"):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "memo_realtime_unavailable",
                "message": "Memo editing is unavailable until realtime locking is ready.",
            },
        )
    try:
        return await realtime.reserve_task_rollup(user_id, operation_id, leases)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code is None:
            raise
        raise HTTPException(
            status_code=409,
            detail={"code": code, "message": str(exc)},
        ) from exc


async def _run_reserved(reservation, function, *args):
    """Do not release a reservation until its shielded SQL worker is terminal."""
    try:
        if _ROLLUP_CAPACITY.locked():
            raise HTTPException(
                status_code=429,
                detail={"code": "task_rollup_busy", "message": "Task rollup is busy"},
                headers={"Retry-After": "1"},
            )
        await _ROLLUP_CAPACITY.acquire()
        try:
            validate_local = getattr(
                reservation, "validate_local", reservation.validate
            )
            worker = asyncio.create_task(
                asyncio.to_thread(
                    function, *args, validate_local, reservation.validate
                )
            )
        except BaseException:
            _ROLLUP_CAPACITY.release()
            raise
        worker.add_done_callback(_release_capacity)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # The client can disconnect, but SQL must reach commit/rollback before
            # its reservation is released. The durable receipt makes retry safe.
            try:
                await asyncio.shield(worker)
            finally:
                raise
    finally:
        await asyncio.shield(reservation.release())


async def _notify_project_changed(project_id: str, user: dict, result: dict) -> None:
    realtime = get_realtime_server()
    if realtime is None or not realtime.available:
        return
    try:
        memos = await asyncio.to_thread(
            load_changed_memos, project_id, user, result.get("changedMemoIds", [])
        )
    except Exception:  # noqa: BLE001 - durable SQL result remains authoritative
        emit_rollup_event("notify", "notification_failure", stage="notification", project_id=project_id,
                          operation_id=result.get("operationId"), cause="refresh_load", alert=True)
        logger.exception(
            "task rollup authoritative refresh load failed: operation_id=%s",
            result.get("operationId"),
        )
        memos = []
    for memo in memos:
        try:
            await realtime.revalidate_memo_access(memo["memoId"])
            await realtime.emit_memo_pulled(
                memo["memoId"], memo["content"], memo["title"], memo["updatedAtUtc"]
            )
        except Exception:  # noqa: BLE001 - continue notifying other affected rooms
            emit_rollup_event("notify", "notification_failure", stage="notification", project_id=project_id,
                              operation_id=result.get("operationId"), memo_id=memo["memoId"], cause="memo_refresh", alert=True)
            logger.exception(
                "task rollup memo refresh notification failed: operation_id=%s memo_id=%s",
                result.get("operationId"),
                memo["memoId"],
            )
    try:
        await realtime.emit_projects_changed()
    except Exception:  # noqa: BLE001 - durable SQL result remains authoritative
        emit_rollup_event("notify", "notification_failure", stage="notification", project_id=project_id,
                          operation_id=result.get("operationId"), cause="project_invalidation", alert=True)
        logger.exception(
            "task rollup project invalidation notification failed: operation_id=%s",
            result.get("operationId"),
        )


@router.get("/projects/{project_id}/backlog/task-rollup")
async def get_task_rollup_preview(
    project_id: str, user: dict = Depends(get_current_user)
):
    with RollupRequestObservation("preview", project_id) as observed:
        return observed.finish(await _bounded_worker(preview, project_id, user, []))


@router.post("/projects/{project_id}/backlog/task-rollup/preview")
async def post_task_rollup_preview(
    project_id: str,
    data: TaskRollupPreviewRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    with RollupRequestObservation("preview", project_id) as observed:
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_JSON_BYTES:
            raise HTTPException(status_code=413, detail={"code": "task_rollup_payload_too_large"})
        payload = _request_dict(data)
        return observed.finish(
            await _bounded_worker(preview, project_id, user, payload["aliases"])
        )


@router.post("/projects/{project_id}/backlog/task-rollup/complete")
async def complete_task_rollup(
    project_id: str,
    data: TaskRollupCompleteRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    with RollupRequestObservation("complete", project_id, data.operationId) as observed:
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_JSON_BYTES:
            raise HTTPException(status_code=413, detail={"code": "task_rollup_payload_too_large"})
        payload = _request_dict(data)
        work = await _bounded_worker(prepare_completion, project_id, user, payload)
        if work.replay is not None:
            return observed.finish(work.replay, replay=True)
        reservation = await _reserve(user["id"], data.operationId, payload["leases"])
        result = await _run_reserved(
            reservation, commit_completion, project_id, user, payload, work
        )
        await _notify_project_changed(project_id, user, result)
        return observed.finish(result)


@router.post("/projects/{project_id}/backlog/task-rollup/{operation_id}/undo")
async def undo_task_rollup(
    project_id: str,
    operation_id: str,
    data: TaskRollupUndoRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    with RollupRequestObservation("undo", project_id, data.undoOperationId) as observed:
        try:
            parsed_operation_id = UUID(operation_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="operationId must be a UUID") from exc
        if str(parsed_operation_id) != operation_id.lower():
            raise HTTPException(status_code=422, detail="operationId must use canonical UUID form")
        operation_id = str(parsed_operation_id)
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_JSON_BYTES:
            raise HTTPException(status_code=413, detail={"code": "task_rollup_payload_too_large"})
        payload = _request_dict(data)
        work = await _bounded_worker(prepare_undo, project_id, operation_id, user, payload)
        if work.replay is not None:
            return observed.finish(work.replay, replay=True)
        reservation = await _reserve(user["id"], data.undoOperationId, payload["leases"])
        result = await _run_reserved(
            reservation, commit_undo, project_id, operation_id, user, payload, work
        )
        await _notify_project_changed(project_id, user, result)
        return observed.finish(result)
