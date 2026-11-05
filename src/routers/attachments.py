"""Authenticated private attachment endpoints for shared memos/private backlogs."""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import unicodedata
import uuid
import zipfile
from datetime import timedelta
from tempfile import SpooledTemporaryFile
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from ..auth_utils import get_current_user
    from ..config import (
        TODO_ATTACHMENT_MAX_COUNT_PER_MEMO,
        TODO_ATTACHMENT_MAX_FILE_BYTES,
        TODO_ATTACHMENT_MAX_ZIP_BYTES,
        TODO_ATTACHMENT_SPOOL_BYTES,
        TODO_ATTACHMENT_UPLOAD_STALE_SECONDS,
    )
    from ..connectors import get_db_connection
    from ..models.attachments import DownloadAttachmentsRequest
    from ..services.attachment_storage import (
        AttachmentStorageDisabled,
        AttachmentStorageError,
        canonical_object_key,
        get_attachment_storage,
    )
    from ..timeutil import iso_utc, localnow_naive, utcnow_naive
    from ..utils import require_locked_memo_access, require_memo_access
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user
    from config import (
        TODO_ATTACHMENT_MAX_COUNT_PER_MEMO,
        TODO_ATTACHMENT_MAX_FILE_BYTES,
        TODO_ATTACHMENT_MAX_ZIP_BYTES,
        TODO_ATTACHMENT_SPOOL_BYTES,
        TODO_ATTACHMENT_UPLOAD_STALE_SECONDS,
    )
    from connectors import get_db_connection
    from models.attachments import DownloadAttachmentsRequest
    from services.attachment_storage import (
        AttachmentStorageDisabled,
        AttachmentStorageError,
        canonical_object_key,
        get_attachment_storage,
    )
    from timeutil import iso_utc, localnow_naive, utcnow_naive
    from utils import require_locked_memo_access, require_memo_access


router = APIRouter(prefix="/api", tags=["attachments"])
ATTACHMENT_COLUMNS = (
    "id, memo_id, origin_memo_id, created_by, file_name, content_type, byte_size, is_image, "
    "sha256, object_key, status, created_at, updated_at_utc, deleted_at"
)
CONTENT_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
IMAGE_SIGNATURES = (
    ("image/png", lambda value: value.startswith(b"\x89PNG\r\n\x1a\n")),
    ("image/jpeg", lambda value: value.startswith(b"\xff\xd8\xff")),
    ("image/gif", lambda value: value.startswith((b"GIF87a", b"GIF89a"))),
    (
        "image/webp",
        lambda value: len(value) >= 12
        and value.startswith(b"RIFF")
        and value[8:12] == b"WEBP",
    ),
)


def _attachment_storage():
    try:
        return get_attachment_storage()
    except AttachmentStorageDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AttachmentStorageError as exc:
        raise HTTPException(
            status_code=503, detail="Memo attachment storage is unavailable"
        ) from exc


def _validate_attachment_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="attachmentId must be a UUID") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise HTTPException(status_code=422, detail="attachmentId must be a canonical UUID v4")
    return str(parsed)


def safe_file_name(value: str) -> str:
    if "/" in value or "\\" in value:
        raise HTTPException(status_code=422, detail="filename is unsafe")
    normalized = unicodedata.normalize("NFC", value)
    if any(
        unicodedata.category(character) in {"Cc", "Cf"}
        for character in normalized
    ):
        raise HTTPException(status_code=422, detail="filename is unsafe")
    name = normalized.strip().strip(".")
    if not name or name != normalized:
        raise HTTPException(status_code=422, detail="filename is unsafe")
    encoded = name.encode("utf-8")
    if len(encoded) > 255:
        raise HTTPException(status_code=422, detail="filename is too long")
    return name


def normalized_content_type(value: str | None, first_bytes: bytes) -> tuple[str, bool]:
    for image_type, matches in IMAGE_SIGNATURES:
        if matches(first_bytes):
            return image_type, True
    candidate = (value or "").split(";", 1)[0].strip().lower()
    if not CONTENT_TYPE_RE.fullmatch(candidate) or candidate.startswith("image/"):
        return "application/octet-stream", False
    return candidate, False


def content_disposition(file_name: str, *, inline: bool) -> str:
    fallback = "".join(
        character
        for character in file_name
        if 32 <= ord(character) < 127 and character not in {'"', "\\", ";"}
    ).strip() or "download"
    fallback = fallback[:120]
    disposition = "inline" if inline else "attachment"
    return (
        f'{disposition}; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(file_name, safe='')}"
    )


def _serialize_attachment(row: dict) -> dict:
    return {
        "id": row["id"],
        "memoId": row["memo_id"],
        "fileName": row["file_name"],
        "contentType": row["content_type"],
        "byteSize": int(row["byte_size"]),
        "isImage": bool(row["is_image"]),
        "status": row["status"],
        "createdBy": row["created_by"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAtUtc": iso_utc(row.get("updated_at_utc")),
    }


async def _read_bounded_body(request: Request) -> tuple[SpooledTemporaryFile, int, str, bytes]:
    declared = request.headers.get("content-length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if declared_size > TODO_ATTACHMENT_MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="Attachment exceeds the file size limit")
    output = SpooledTemporaryFile(max_size=TODO_ATTACHMENT_SPOOL_BYTES, mode="w+b")
    digest = hashlib.sha256()
    first_bytes = bytearray()
    total = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > TODO_ATTACHMENT_MAX_FILE_BYTES:
                raise HTTPException(
                    status_code=413, detail="Attachment exceeds the file size limit"
                )
            if len(first_bytes) < 32:
                first_bytes.extend(chunk[: 32 - len(first_bytes)])
            digest.update(chunk)
            output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=422, detail="Attachment body is empty")
        output.seek(0)
        return output, total, digest.hexdigest(), bytes(first_bytes)
    except BaseException:
        output.close()
        raise


def _validate_attachment_row(row: dict) -> None:
    try:
        expected_key = canonical_object_key(row["origin_memo_id"], row["id"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail="Attachment metadata is invalid"
        ) from exc
    if row["object_key"] != expected_key:
        raise HTTPException(status_code=502, detail="Attachment metadata is invalid")


def _load_attachment(
    cursor, attachment_id: str, user: dict, *, memo_id: str | None = None
) -> dict:
    cursor.execute(
        f"SELECT {ATTACHMENT_COLUMNS} FROM memo_attachments "
        "WHERE id = %s AND deleted_at IS NULL",
        (attachment_id,),
    )
    row = cursor.fetchone()
    if not row or (memo_id is not None and row["memo_id"] != memo_id):
        raise HTTPException(status_code=404, detail="Attachment not found")
    require_memo_access(cursor, row["memo_id"], user)
    _validate_attachment_row(row)
    return row


def _metadata_matches(
    row: dict,
    *,
    memo_id: str,
    file_name: str,
    content_type: str,
    byte_size: int,
    sha256: str,
) -> bool:
    try:
        expected_key = canonical_object_key(row["origin_memo_id"], row["id"])
    except (KeyError, ValueError):
        return False
    return (
        row["memo_id"] == memo_id
        and row["file_name"] == file_name
        and row["content_type"] == content_type
        and int(row["byte_size"]) == byte_size
        and row["sha256"] == sha256
        and row["object_key"] == expected_key
        and row["deleted_at"] is None
    )


def _mark_upload_failed(attachment_id: str, sha256: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE memo_attachments SET status = 'failed', updated_at_utc = %s "
                "WHERE id = %s AND sha256 = %s AND status = 'uploading'",
                (utcnow_naive(), attachment_id, sha256),
            )


def _verified_download(storage, row: dict, max_bytes: int):
    downloaded = storage.get_object(row["object_key"], max_bytes=max_bytes)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = downloaded.file.read(64 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    downloaded.file.seek(0)
    if total != int(row["byte_size"]) or digest.hexdigest() != row["sha256"]:
        downloaded.file.close()
        raise AttachmentStorageError("Stored attachment integrity check failed")
    return downloaded


def _downloaded_image_type(file_obj) -> str | None:
    position = file_obj.tell()
    file_obj.seek(0)
    first_bytes = file_obj.read(32)
    file_obj.seek(position)
    content_type, is_image = normalized_content_type(None, first_bytes)
    return content_type if is_image else None


def _stream_file(file_obj, chunk_size: int = 64 * 1024):
    try:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        file_obj.close()


def _response_headers(file_name: str, byte_size: int, *, inline: bool) -> dict[str, str]:
    try:
        safe_name = safe_file_name(file_name)
    except HTTPException:
        safe_name = "download"
    return {
        "Content-Length": str(byte_size),
        "Content-Disposition": content_disposition(safe_name, inline=inline),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }


@router.get("/memos/{memo_id}/attachments")
async def list_memo_attachments(
    memo_id: str, user: dict = Depends(get_current_user)
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            require_memo_access(cursor, memo_id, user)
            cursor.execute(
                f"SELECT {ATTACHMENT_COLUMNS} FROM memo_attachments "
                "WHERE memo_id = %s AND deleted_at IS NULL ORDER BY created_at, id",
                (memo_id,),
            )
            return [_serialize_attachment(row) for row in cursor.fetchall()]


@router.post("/memos/{memo_id}/attachments")
async def upload_memo_attachment(
    memo_id: str,
    request: Request,
    attachmentId: str = Query(...),
    filename: str = Query(...),
    user: dict = Depends(get_current_user),
):
    if user.get("permission") != "superadmin":
        raise HTTPException(status_code=403, detail="Super admin access required for uploads")
    attachment_id = _validate_attachment_id(attachmentId)
    file_name = safe_file_name(filename)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            require_memo_access(cursor, memo_id, user, write=True)
    storage = _attachment_storage()

    upload, byte_size, sha256, first_bytes = await _read_bounded_body(request)
    content_type, is_image = normalized_content_type(
        request.headers.get("content-type"), first_bytes
    )
    object_key = canonical_object_key(memo_id, attachment_id)
    created = False
    existing_ready_row = None
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # This must be the transaction's first read. The core helper
                # locks memo/project/membership in one current read, preventing
                # a waiter from retaining a stale REPEATABLE READ auth snapshot.
                require_locked_memo_access(cursor, memo_id, user, write=True)
                cursor.execute(
                    "UPDATE memo_attachments SET status = 'failed', updated_at_utc = %s "
                    "WHERE memo_id = %s AND status = 'uploading' "
                    "AND updated_at_utc < %s",
                    (
                        utcnow_naive(),
                        memo_id,
                        utcnow_naive()
                        - timedelta(seconds=TODO_ATTACHMENT_UPLOAD_STALE_SECONDS),
                    ),
                )
                cursor.execute(
                    f"SELECT {ATTACHMENT_COLUMNS} FROM memo_attachments "
                    "WHERE id = %s FOR UPDATE",
                    (attachment_id,),
                )
                existing = cursor.fetchone()
                if existing:
                    if existing["created_by"] != user["id"]:
                        raise HTTPException(
                            status_code=409,
                            detail="attachmentId belongs to another uploader",
                        )
                    try:
                        object_key = canonical_object_key(
                            existing["origin_memo_id"], attachment_id
                        )
                    except (KeyError, ValueError) as exc:
                        raise HTTPException(
                            status_code=502, detail="Attachment metadata is invalid"
                        ) from exc
                    if not _metadata_matches(
                        existing,
                        memo_id=memo_id,
                        file_name=file_name,
                        content_type=content_type,
                        byte_size=byte_size,
                        sha256=sha256,
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="attachmentId is already used for different content",
                        )
                    if existing["status"] == "ready":
                        existing_ready_row = existing
                    elif existing["status"] == "failed":
                        cursor.execute(
                            "SELECT COUNT(*) AS count FROM memo_attachments "
                            "WHERE memo_id = %s AND deleted_at IS NULL "
                            "AND status IN ('uploading', 'ready')",
                            (memo_id,),
                        )
                        if (
                            int(cursor.fetchone()["count"])
                            >= TODO_ATTACHMENT_MAX_COUNT_PER_MEMO
                        ):
                            raise HTTPException(
                                status_code=409,
                                detail="Memo attachment count limit reached",
                            )
                        cursor.execute(
                            "UPDATE memo_attachments SET status = 'uploading', "
                            "updated_at_utc = %s WHERE id = %s",
                            (utcnow_naive(), attachment_id),
                        )
                    elif existing["status"] != "uploading":
                        raise HTTPException(
                            status_code=502, detail="Attachment metadata is invalid"
                        )
                else:
                    cursor.execute(
                        "SELECT COUNT(*) AS count FROM memo_attachments "
                        "WHERE memo_id = %s AND deleted_at IS NULL "
                        "AND status IN ('uploading', 'ready')",
                        (memo_id,),
                    )
                    if int(cursor.fetchone()["count"]) >= TODO_ATTACHMENT_MAX_COUNT_PER_MEMO:
                        raise HTTPException(
                            status_code=409,
                            detail="Memo attachment count limit reached",
                        )
                    now = localnow_naive()
                    cursor.execute(
                        """
                        INSERT INTO memo_attachments
                            (id, memo_id, origin_memo_id, created_by, file_name,
                             content_type, byte_size,
                             is_image, sha256, object_key, status, created_at,
                             updated_at_utc, deleted_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                'uploading', %s, %s, NULL)
                        """,
                        (
                            attachment_id,
                            memo_id,
                            memo_id,
                            user["id"],
                            file_name,
                            content_type,
                            byte_size,
                            is_image,
                            sha256,
                            object_key,
                            now,
                            utcnow_naive(),
                        ),
                    )
                    created = True

        # Do not hold a database transaction during any storage network call.
        if existing_ready_row is not None:
            try:
                head = await asyncio.to_thread(storage.head_object, object_key)
            except AttachmentStorageError as exc:
                raise HTTPException(
                    status_code=502, detail="Attachment storage verification failed"
                ) from exc
            if (
                head
                and head.byte_size == byte_size
                and (head.sha256 is None or head.sha256 == sha256)
            ):
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        require_locked_memo_access(
                            cursor, memo_id, user, write=True
                        )
                return JSONResponse(
                    _serialize_attachment(existing_ready_row), status_code=200
                )
            # A missing object is repaired with the exact same immutable bytes.
            # Keep DB status ready so sync never observes a ready->uploading or
            # ready->failed rollback.
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    require_locked_memo_access(cursor, memo_id, user, write=True)

        try:
            await asyncio.to_thread(
                storage.put_object,
                object_key,
                upload,
                byte_size=byte_size,
                content_type=content_type,
                sha256=sha256,
            )
        except (AttachmentStorageError, asyncio.CancelledError) as exc:
            _mark_upload_failed(attachment_id, sha256)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(
                status_code=502, detail="Attachment upload failed"
            ) from exc

        if existing_ready_row is not None:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    require_locked_memo_access(cursor, memo_id, user, write=True)
                    cursor.execute(
                        f"SELECT {ATTACHMENT_COLUMNS} FROM memo_attachments "
                        "WHERE id = %s AND status = 'ready' AND deleted_at IS NULL",
                        (attachment_id,),
                    )
                    repaired = cursor.fetchone()
                    if repaired is None:
                        raise HTTPException(
                            status_code=409, detail="Attachment upload state changed"
                        )
            return JSONResponse(_serialize_attachment(repaired), status_code=200)

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    require_locked_memo_access(cursor, memo_id, user, write=True)
                    cursor.execute(
                        "UPDATE memo_attachments SET status = 'ready', updated_at_utc = %s "
                        "WHERE id = %s AND sha256 = %s AND status = 'uploading'",
                        (utcnow_naive(), attachment_id, sha256),
                    )
                    if cursor.rowcount != 1:
                        cursor.execute(
                            f"SELECT {ATTACHMENT_COLUMNS} FROM memo_attachments "
                            "WHERE id = %s AND status = 'ready' AND deleted_at IS NULL",
                            (attachment_id,),
                        )
                        concurrent_ready = cursor.fetchone()
                        if concurrent_ready and _metadata_matches(
                            concurrent_ready,
                            memo_id=memo_id,
                            file_name=file_name,
                            content_type=content_type,
                            byte_size=byte_size,
                            sha256=sha256,
                        ):
                            return JSONResponse(
                                _serialize_attachment(concurrent_ready), status_code=200
                            )
                        raise HTTPException(
                            status_code=409, detail="Attachment upload state changed"
                        )
                    cursor.execute(
                        f"SELECT {ATTACHMENT_COLUMNS} FROM memo_attachments WHERE id = %s",
                        (attachment_id,),
                    )
                    ready = cursor.fetchone()
        except HTTPException:
            _mark_upload_failed(attachment_id, sha256)
            raise
        return JSONResponse(
            _serialize_attachment(ready), status_code=201 if created else 200
        )
    finally:
        upload.close()


@router.get("/attachments/{attachment_id}/content")
async def get_attachment_content(
    attachment_id: str,
    inline: bool = Query(False),
    user: dict = Depends(get_current_user),
):
    attachment_id = _validate_attachment_id(attachment_id)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            row = _load_attachment(cursor, attachment_id, user)
    storage = _attachment_storage()
    if row["status"] != "ready":
        raise HTTPException(status_code=409, detail="Attachment is not ready")
    try:
        downloaded = await asyncio.to_thread(
            _verified_download, storage, row, TODO_ATTACHMENT_MAX_FILE_BYTES
        )
    except AttachmentStorageError as exc:
        raise HTTPException(status_code=502, detail="Attachment download failed") from exc
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            require_memo_access(cursor, row["memo_id"], user)
    detected_image_type = _downloaded_image_type(downloaded.file)
    allow_inline = inline and detected_image_type is not None
    return StreamingResponse(
        _stream_file(downloaded.file),
        media_type=detected_image_type or "application/octet-stream",
        headers=_response_headers(
            row["file_name"], downloaded.byte_size, inline=allow_inline
        ),
    )


def _unique_zip_name(file_name: str, used: set[str]) -> str:
    try:
        candidate = safe_file_name(file_name)
    except HTTPException:
        candidate = "download"
    candidate_key = unicodedata.normalize("NFC", candidate).casefold()
    if candidate_key not in used:
        used.add(candidate_key)
        return candidate
    stem, dot, suffix = candidate.rpartition(".")
    if not dot:
        stem, suffix = candidate, ""
    counter = 2
    while True:
        proposed = f"{stem} ({counter}){'.' + suffix if suffix else ''}"
        proposed_key = unicodedata.normalize("NFC", proposed).casefold()
        if proposed_key not in used:
            used.add(proposed_key)
            return proposed
        counter += 1


def write_zip_archive(archive, rows: list[dict], downloads: list) -> None:
    """Write safe, unique basenames without materializing file bodies in RAM."""
    used_names: set[str] = set()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for row, downloaded in zip(rows, downloads, strict=True):
            archive_name = _unique_zip_name(row["file_name"], used_names)
            with bundle.open(archive_name, "w") as target:
                shutil.copyfileobj(downloaded.file, target, length=64 * 1024)


def _build_verified_zip(storage, archive, rows: list[dict]) -> None:
    """Download/verify/write one object at a time to keep memory bounded."""
    used_names: set[str] = set()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for row in rows:
            downloaded = _verified_download(
                storage, row, TODO_ATTACHMENT_MAX_FILE_BYTES
            )
            try:
                archive_name = _unique_zip_name(row["file_name"], used_names)
                with bundle.open(archive_name, "w") as target:
                    shutil.copyfileobj(downloaded.file, target, length=64 * 1024)
            finally:
                downloaded.file.close()


@router.post("/memos/{memo_id}/attachments/download")
async def download_memo_attachments(
    memo_id: str,
    body: DownloadAttachmentsRequest,
    user: dict = Depends(get_current_user),
):
    if len(body.attachmentIds) > TODO_ATTACHMENT_MAX_COUNT_PER_MEMO:
        raise HTTPException(status_code=422, detail="Too many attachments selected")
    if len(set(body.attachmentIds)) != len(body.attachmentIds):
        raise HTTPException(status_code=422, detail="attachmentIds must be unique")
    validated_ids = [_validate_attachment_id(value) for value in body.attachmentIds]
    placeholders = ",".join(["%s"] * len(validated_ids))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            require_memo_access(cursor, memo_id, user)
            cursor.execute(
                f"SELECT {ATTACHMENT_COLUMNS} FROM memo_attachments "
                f"WHERE memo_id = %s AND id IN ({placeholders}) "
                "AND deleted_at IS NULL AND status = 'ready'",
                (memo_id, *validated_ids),
            )
            by_id = {row["id"]: row for row in cursor.fetchall()}
    if len(by_id) != len(validated_ids):
        raise HTTPException(status_code=404, detail="Attachment not found")
    storage = _attachment_storage()
    rows = [by_id[value] for value in validated_ids]
    for row in rows:
        _validate_attachment_row(row)
    total_size = sum(int(row["byte_size"]) for row in rows)
    if total_size > TODO_ATTACHMENT_MAX_ZIP_BYTES:
        raise HTTPException(status_code=413, detail="Selected attachments exceed ZIP limit")

    if len(rows) == 1:
        row = rows[0]
        try:
            downloaded = await asyncio.to_thread(
                _verified_download, storage, row, TODO_ATTACHMENT_MAX_FILE_BYTES
            )
        except AttachmentStorageError as exc:
            raise HTTPException(status_code=502, detail="Attachment download failed") from exc
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                require_memo_access(cursor, memo_id, user)
        return StreamingResponse(
            _stream_file(downloaded.file),
            media_type=_downloaded_image_type(downloaded.file)
            or "application/octet-stream",
            headers=_response_headers(
                row["file_name"], downloaded.byte_size, inline=False
            ),
        )

    archive = SpooledTemporaryFile(max_size=TODO_ATTACHMENT_SPOOL_BYTES, mode="w+b")
    try:
        await asyncio.to_thread(_build_verified_zip, storage, archive, rows)
        archive.seek(0, 2)
        archive_size = archive.tell()
        if archive_size > TODO_ATTACHMENT_MAX_ZIP_BYTES:
            raise HTTPException(status_code=413, detail="Generated ZIP exceeds size limit")
        archive.seek(0)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                require_memo_access(cursor, memo_id, user)
        return StreamingResponse(
            _stream_file(archive),
            media_type="application/zip",
            headers=_response_headers(
                "todo-attachments.zip", archive_size, inline=False
            ),
        )
    except AttachmentStorageError as exc:
        archive.close()
        raise HTTPException(status_code=502, detail="Attachment download failed") from exc
    except BaseException:
        archive.close()
        raise
