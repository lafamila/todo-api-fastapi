from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import unittest
import uuid
import zipfile
from datetime import timedelta
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from tests.scratch_db import (
    assert_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

from src.connectors import get_db_connection, init_db
from src.models.attachments import DownloadAttachmentsRequest
from src.routers.attachments import (
    download_memo_attachments,
    get_attachment_content,
    list_memo_attachments,
    upload_memo_attachment,
)
from src.routers import attachments as attachment_routes
from src.services.attachment_schema import ensure_attachment_schema
from src.services.attachment_storage import (
    AttachmentStorageError,
    AttachmentStorageClient,
    canonical_object_key,
    set_attachment_storage_for_tests,
)
from src.timeutil import localnow_naive, utcnow_naive


SCRATCH_DB = assert_scratch_database("todo_sync_media_storage_test")
PROJECT_ID = "attachment-api-project"
MEMO_ID = "attachment-api-memo"
BACKLOG_ID = "attachment-api-private-backlog"
OWNER_ID = "attachment-owner"

OWNER = {
    "id": OWNER_ID,
    "username": "attachment.owner",
    "display_name": "Attachment Owner",
    "email": "attachment.owner@example.test",
    "permission": "superadmin",
    "is_admin": True,
    "is_super_admin": True,
}
OUTSIDER = {
    "id": "attachment-outsider",
    "username": "attachment.outsider",
    "display_name": "Attachment Outsider",
    "email": "attachment.outsider@example.test",
    "permission": "user",
    "is_admin": False,
}


def _raw_request(body: bytes, content_type: str = "application/octet-stream") -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [
                (b"content-type", content_type.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
        },
        receive,
    )


async def _streaming_body(response) -> bytes:
    output = bytearray()
    async for chunk in response.body_iterator:
        output.extend(chunk if isinstance(chunk, bytes) else chunk.encode())
    return bytes(output)


class AttachmentApiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        endpoint = os.getenv("TODO_ATTACHMENT_TEST_S3_ENDPOINT")
        if not endpoint:
            raise unittest.SkipTest("TODO_ATTACHMENT_TEST_S3_ENDPOINT is not configured")
        use_scratch_database(SCRATCH_DB)
        init_db()
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                ensure_attachment_schema(cursor)
        cls.storage = AttachmentStorageClient(
            endpoint_url=endpoint,
            bucket_name=os.environ["TODO_ATTACHMENT_TEST_S3_BUCKET"],
            region=os.getenv("TODO_ATTACHMENT_TEST_S3_REGION", "us-east-1"),
            access_key=os.getenv("TODO_ATTACHMENT_TEST_S3_ACCESS_KEY", "test"),
            secret_key=os.getenv("TODO_ATTACHMENT_TEST_S3_SECRET_KEY", "test"),
            enabled=True,
        )
        set_attachment_storage_for_tests(cls.storage)

    @classmethod
    def tearDownClass(cls) -> None:
        set_attachment_storage_for_tests(None)

    def setUp(self) -> None:
        truncate_scratch_tables()
        now = localnow_naive()
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, owner_id, parent_id, is_group, name, icon, status,
                         is_secret, created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, NULL, 0, 'Attachment API', 'Idea', 0, 0,
                            %s, %s, %s)
                    """,
                    (PROJECT_ID, OWNER_ID, now, now, utcnow_naive()),
                )
                cursor.execute(
                    """
                    INSERT INTO project_members
                        (id, project_id, user_id, username, display_name, email,
                         role, invited_at, updated_at_utc)
                    VALUES ('attachment-owner-member', %s, %s, 'attachment.owner',
                            'Attachment Owner', 'attachment.owner@example.test',
                            'owner', %s, %s)
                    """,
                    (PROJECT_ID, OWNER_ID, now, utcnow_naive()),
                )
                for memo_id, kind, created_by in (
                    (MEMO_ID, "memo", OWNER_ID),
                    (BACKLOG_ID, "backlog", OWNER_ID),
                ):
                    cursor.execute(
                        """
                        INSERT INTO memos
                            (id, project_id, created_by, kind, title, content,
                             status, created_at, updated_at, updated_at_utc)
                        VALUES (%s, %s, %s, %s, 'Attachment memo', '', 0,
                                %s, %s, %s)
                        """,
                        (
                            memo_id,
                            PROJECT_ID,
                            created_by,
                            kind,
                            now,
                            now,
                            utcnow_naive(),
                        ),
                    )
        self.object_keys: list[str] = []

    def tearDown(self) -> None:
        for key in self.object_keys:
            try:
                self.storage.delete_object(key)
            except Exception:
                pass

    async def _upload(
        self,
        memo_id: str,
        attachment_id: str,
        filename: str,
        body: bytes,
        user: dict = OWNER,
    ):
        response = await upload_memo_attachment(
            memo_id,
            _raw_request(body, "image/png"),
            attachmentId=attachment_id,
            filename=filename,
            user=user,
        )
        if response.status_code < 400:
            self.object_keys.append(canonical_object_key(memo_id, attachment_id))
        return response

    async def test_raw_upload_list_inline_content_and_idempotent_retry(self) -> None:
        attachment_id = str(uuid.uuid4())
        body = b"\x89PNG\r\n\x1a\nprivate image bytes"
        first = await self._upload(MEMO_ID, attachment_id, "한글 이미지.png", body)
        self.assertEqual(first.status_code, 201, first.body)
        first_json = json.loads(first.body)
        self.assertEqual(first_json["status"], "ready")
        self.assertTrue(first_json["isImage"])

        listed = await list_memo_attachments(MEMO_ID, OWNER)
        self.assertEqual([item["id"] for item in listed], [attachment_id])

        content = await get_attachment_content(
            attachment_id, inline=True, user=OWNER
        )
        self.assertEqual(await _streaming_body(content), body)
        self.assertEqual(content.headers["content-type"], "image/png")
        self.assertTrue(content.headers["content-disposition"].startswith("inline;"))
        self.assertEqual(content.headers["x-content-type-options"], "nosniff")

        retry = await self._upload(MEMO_ID, attachment_id, "한글 이미지.png", body)
        self.assertEqual(retry.status_code, 200, retry.body)

        with self.assertRaises(HTTPException) as mismatch:
            await self._upload(MEMO_ID, attachment_id, "한글 이미지.png", body + b"x")
        self.assertEqual(mismatch.exception.status_code, 409)

    async def test_private_backlog_attachment_is_hidden_from_other_member(self) -> None:
        attachment_id = str(uuid.uuid4())
        body = b"private backlog file"
        response = await self._upload(BACKLOG_ID, attachment_id, "private.txt", body)
        self.assertEqual(response.status_code, 201, response.body)
        with self.assertRaises(HTTPException) as listed:
            await list_memo_attachments(BACKLOG_ID, OUTSIDER)
        self.assertIn(listed.exception.status_code, {403, 404})
        with self.assertRaises(HTTPException) as direct:
            await get_attachment_content(attachment_id, inline=False, user=OUTSIDER)
        self.assertIn(direct.exception.status_code, {403, 404})

    async def test_members_can_download_existing_files_but_cannot_upload(self) -> None:
        attachment_id = str(uuid.uuid4())
        body = b"shared download remains available"
        await self._upload(MEMO_ID, attachment_id, "shared.txt", body)
        member = {**OUTSIDER, "id": "attachment-viewer"}
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO project_members
                       (id,project_id,user_id,role,invited_at,updated_at_utc)
                       VALUES ('download-viewer',%s,%s,'viewer',%s,%s)""",
                    (PROJECT_ID, member["id"], localnow_naive(), utcnow_naive()),
                )
        listed = await list_memo_attachments(MEMO_ID, member)
        self.assertEqual([row["id"] for row in listed], [attachment_id])
        downloaded = await download_memo_attachments(
            MEMO_ID, DownloadAttachmentsRequest(attachmentIds=[attachment_id]), member,
        )
        self.assertEqual(await _streaming_body(downloaded), body)
        for permission in ("owner", "admin", "user"):
            with self.subTest(permission=permission), self.assertRaises(HTTPException) as denied:
                await self._upload(
                    MEMO_ID, str(uuid.uuid4()), "blocked.png", b"new bytes",
                    user={**OWNER, "permission": permission},
                )
            self.assertEqual(denied.exception.status_code, 403)

    async def test_multiple_selection_returns_safe_zip_with_exact_bytes(self) -> None:
        payloads = []
        ids = []
        for filename, body in (
            ("same.png", b"\x89PNG\r\n\x1a\nfirst"),
            ("same.png", b"\x89PNG\r\n\x1a\nsecond"),
        ):
            attachment_id = str(uuid.uuid4())
            uploaded = await self._upload(MEMO_ID, attachment_id, filename, body)
            self.assertEqual(uploaded.status_code, 201, uploaded.body)
            ids.append(attachment_id)
            payloads.append(body)
        response = await download_memo_attachments(
            MEMO_ID,
            DownloadAttachmentsRequest(attachmentIds=ids),
            OWNER,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(await _streaming_body(response))) as bundle:
            self.assertEqual(bundle.namelist(), ["same.png", "same (2).png"])
            self.assertEqual(bundle.read("same.png"), payloads[0])
            self.assertEqual(bundle.read("same (2).png"), payloads[1])

    async def test_forged_image_metadata_cannot_make_html_inline(self) -> None:
        attachment_id = str(uuid.uuid4())
        body = b"<html><script>alert(1)</script></html>"
        uploaded = await self._upload(MEMO_ID, attachment_id, "payload.html", body)
        self.assertEqual(uploaded.status_code, 201, uploaded.body)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE memo_attachments SET is_image = 1, content_type = 'text/html' "
                    "WHERE id = %s",
                    (attachment_id,),
                )
        response = await get_attachment_content(attachment_id, inline=True, user=OWNER)
        self.assertEqual(await _streaming_body(response), body)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))

    async def test_reparented_metadata_keeps_origin_object_key_without_copy(self) -> None:
        attachment_id = str(uuid.uuid4())
        body = b"origin key remains stable"
        uploaded = await self._upload(MEMO_ID, attachment_id, "stable.txt", body)
        self.assertEqual(uploaded.status_code, 201, uploaded.body)
        original_key = canonical_object_key(MEMO_ID, attachment_id)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE memo_attachments SET memo_id = %s WHERE id = %s",
                    (BACKLOG_ID, attachment_id),
                )
        listed = await list_memo_attachments(BACKLOG_ID, OWNER)
        self.assertEqual([item["id"] for item in listed], [attachment_id])
        response = await get_attachment_content(attachment_id, inline=False, user=OWNER)
        self.assertEqual(await _streaming_body(response), body)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT origin_memo_id, object_key FROM memo_attachments WHERE id = %s",
                    (attachment_id,),
                )
                row = cursor.fetchone()
        self.assertEqual(row["origin_memo_id"], MEMO_ID)
        self.assertEqual(row["object_key"], original_key)

    async def test_concurrent_uploads_cannot_cross_per_memo_count_boundary(self) -> None:
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())
        with patch.object(attachment_routes, "TODO_ATTACHMENT_MAX_COUNT_PER_MEMO", 1):
            results = await asyncio.gather(
                self._upload(MEMO_ID, first_id, "first.txt", b"first"),
                self._upload(MEMO_ID, second_id, "second.txt", b"second"),
                return_exceptions=True,
            )
        successes = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, HTTPException)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].status_code, 409)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM memo_attachments "
                    "WHERE memo_id = %s AND deleted_at IS NULL",
                    (MEMO_ID,),
                )
                count = int(cursor.fetchone()["count"])
        self.assertEqual(count, 1)

    async def test_failed_upload_retries_same_uuid_and_hash_to_ready(self) -> None:
        class FailOnceStorage:
            def __init__(self, delegate):
                self.delegate = delegate
                self.failed = False

            def put_object(self, *args, **kwargs):
                if not self.failed:
                    self.failed = True
                    raise AttachmentStorageError("controlled failure")
                return self.delegate.put_object(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.delegate, name)

        controlled = FailOnceStorage(self.storage)
        set_attachment_storage_for_tests(controlled)
        attachment_id = str(uuid.uuid4())
        body = b"retry exact bytes"
        try:
            with self.assertRaises(HTTPException) as failed:
                await self._upload(MEMO_ID, attachment_id, "retry.txt", body)
            self.assertEqual(failed.exception.status_code, 502)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT status, sha256 FROM memo_attachments WHERE id = %s",
                        (attachment_id,),
                    )
                    failed_row = cursor.fetchone()
            self.assertEqual(failed_row["status"], "failed")
            self.assertEqual(failed_row["sha256"], hashlib.sha256(body).hexdigest())

            retry = await self._upload(MEMO_ID, attachment_id, "retry.txt", body)
            self.assertEqual(retry.status_code, 200, retry.body)
            self.assertEqual(json.loads(retry.body)["status"], "ready")
        finally:
            set_attachment_storage_for_tests(self.storage)

    async def test_stale_uploading_reservation_releases_capacity(self) -> None:
        stale_id = str(uuid.uuid4())
        new_id = str(uuid.uuid4())
        old = utcnow_naive() - timedelta(hours=2)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memo_attachments
                        (id, memo_id, origin_memo_id, created_by, file_name,
                         content_type, byte_size, is_image, sha256, object_key,
                         status, created_at, updated_at_utc, deleted_at)
                    VALUES (%s, %s, %s, %s, 'stale.txt',
                            'application/octet-stream', 1, 0, %s, %s,
                            'uploading', %s, %s, NULL)
                    """,
                    (
                        stale_id,
                        MEMO_ID,
                        MEMO_ID,
                        OWNER_ID,
                        hashlib.sha256(b"x").hexdigest(),
                        canonical_object_key(MEMO_ID, stale_id),
                        localnow_naive(),
                        old,
                    ),
                )
        with (
            patch.object(attachment_routes, "TODO_ATTACHMENT_MAX_COUNT_PER_MEMO", 1),
            patch.object(attachment_routes, "TODO_ATTACHMENT_UPLOAD_STALE_SECONDS", 3600),
        ):
            uploaded = await self._upload(MEMO_ID, new_id, "new.txt", b"new")
        self.assertEqual(uploaded.status_code, 201, uploaded.body)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, status FROM memo_attachments WHERE id IN (%s, %s)",
                    (stale_id, new_id),
                )
                statuses = {row["id"]: row["status"] for row in cursor.fetchall()}
        self.assertEqual(statuses, {stale_id: "failed", new_id: "ready"})


if __name__ == "__main__":
    unittest.main()
