from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException

from src.auth_utils import get_current_user
from src.routers import attachments
from tests.test_attachments_api import _raw_request


class AttachmentUploadPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_upload_rejects_non_superadmin_for_files_and_images(self) -> None:
        for permission in ("owner", "admin", "user", "visitor"):
            for content_type in ("application/octet-stream", "image/png"):
                with (
                    self.subTest(permission=permission, content_type=content_type),
                    patch.object(attachments, "get_db_connection") as database,
                    patch.object(attachments, "_attachment_storage") as storage,
                ):
                    app = FastAPI()
                    app.include_router(attachments.router)
                    app.dependency_overrides[get_current_user] = lambda: {
                        "id": "member", "permission": permission,
                    }
                    messages = []

                    async def receive():
                        self.fail("Forbidden uploads must not read the request body")

                    async def send(message):
                        messages.append(message)

                    await app(
                        {
                            "type": "http", "http_version": "1.1", "scheme": "http",
                            "method": "POST", "path": "/api/memos/memo/attachments",
                            "query_string": b"attachmentId=123e4567-e89b-42d3-a456-426614174000&filename=file.png",
                            "headers": [(b"content-type", content_type.encode())],
                            "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000),
                        }, receive, send,
                    )
                    self.assertEqual(messages[0]["status"], 403)
                    database.assert_not_called()
                    storage.assert_not_called()

    async def test_only_superadmin_can_reach_upload_io(self) -> None:
        for permission in (None, "visitor", "user", "admin", "owner", "SUPERADMIN"):
            with (
                self.subTest(permission=permission),
                patch.object(attachments, "get_db_connection") as database,
                patch.object(attachments, "_attachment_storage") as storage,
                patch.object(attachments, "_read_bounded_body") as read_body,
            ):
                with self.assertRaises(HTTPException) as denied:
                    await attachments.upload_memo_attachment(
                        "memo", _raw_request(b"file bytes"),
                        attachmentId="123e4567-e89b-42d3-a456-426614174000",
                        filename="file.txt",
                        user={"id": "project-owner", "permission": permission,
                              "is_admin": True, "is_super_admin": True},
                    )
                self.assertEqual(denied.exception.status_code, 403)
                database.assert_not_called()
                storage.assert_not_called()
                read_body.assert_not_called()

    async def test_superadmin_still_requires_memo_write_access(self) -> None:
        with (
            patch.object(attachments, "get_db_connection"),
            patch.object(attachments, "require_memo_access", side_effect=HTTPException(403, "Access denied")) as memo_access,
            patch.object(attachments, "_attachment_storage") as storage,
            patch.object(attachments, "_read_bounded_body") as read_body,
        ):
            with self.assertRaises(HTTPException) as denied:
                await attachments.upload_memo_attachment(
                    "private-memo", _raw_request(b"file bytes"),
                    attachmentId="123e4567-e89b-42d3-a456-426614174000",
                    filename="file.txt", user={"id": "admin", "permission": "superadmin"},
                )
            self.assertEqual(denied.exception.detail, "Access denied")
            self.assertTrue(memo_access.call_args.kwargs["write"])
            storage.assert_not_called()
            read_body.assert_not_called()

    async def test_existing_attachment_listing_is_still_allowed_for_members(self) -> None:
        with (
            patch.object(attachments, "get_db_connection") as database,
            patch.object(attachments, "require_memo_access") as memo_access,
        ):
            cursor = database.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = []
            member = {"id": "member", "permission": "user"}
            self.assertEqual(await attachments.list_memo_attachments("memo", member), [])
            memo_access.assert_called_once_with(cursor, "memo", member)

    async def test_superadmin_can_proceed_to_storage_after_memo_authorization(self) -> None:
        with (
            patch.object(attachments, "get_db_connection"),
            patch.object(attachments, "require_memo_access") as memo_access,
            patch.object(attachments, "_attachment_storage", side_effect=HTTPException(503, "Storage disabled")) as storage,
            patch.object(attachments, "_read_bounded_body") as read_body,
        ):
            with self.assertRaises(HTTPException) as unavailable:
                await attachments.upload_memo_attachment(
                    "memo", _raw_request(b"file bytes"),
                    attachmentId="123e4567-e89b-42d3-a456-426614174000",
                    filename="file.txt", user={"id": "member", "permission": "superadmin"},
                )
            self.assertEqual(unavailable.exception.status_code, 503)
            self.assertTrue(memo_access.call_args.kwargs["write"])
            storage.assert_called_once()
            read_body.assert_not_called()
