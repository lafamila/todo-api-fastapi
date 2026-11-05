from __future__ import annotations

import asyncio
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import HTTPException

from tests.scratch_db import (
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

SCRATCH = use_scratch_database("todo_sync_attachment_delete_api_test")

from src.connectors import get_db_connection  # noqa: E402
from src.models.attachments import DeleteAttachmentsRequest  # noqa: E402
from src.routers.attachments import (  # noqa: E402
    delete_memo_attachments,
    get_attachment_content,
    list_memo_attachments,
)
from src.routers import attachments as attachment_routes  # noqa: E402
from src.routers.sync import _authorize_sync_changes  # noqa: E402
from src.services.sync_store import collect_local_changes  # noqa: E402
from src.services.attachment_storage import canonical_object_key  # noqa: E402
from src.timeutil import iso_utc  # noqa: E402


BASE = datetime(2026, 11, 5, 7, 0, 0)
PROJECT_ID = "delete-attachment-project"
MEMO_ID = "delete-attachment-memo"
BACKLOG_ID = "delete-private-backlog"
UPLOADER = "delete-uploader"
OTHER = "delete-other"
OWNER = "delete-project-owner"
ATTACH_UPLOADER = "11111111-1111-4111-8111-111111111111"
ATTACH_OTHER = "22222222-2222-4222-8222-222222222222"
ATTACH_DELETED = "33333333-3333-4333-8333-333333333333"
ATTACH_FAILED = "44444444-4444-4444-8444-444444444444"
REACHABLE = init_scratch_database()


def user(user_id: str, permission: str = "user") -> dict:
    return {
        "id": user_id,
        "username": user_id,
        "display_name": user_id,
        "email": f"{user_id}@example.test",
        "permission": permission,
        "is_admin": permission in {"owner", "admin", "superadmin"},
    }


def sync_change(row: dict) -> dict:
    return {
        "table": "memo_attachments",
        "rowId": row["id"],
        "op": "update",
        "seq": 1,
        "row": row,
    }


@unittest.skipUnless(REACHABLE, "attachment delete scratch DB unavailable")
class AttachmentDeleteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        use_scratch_database(SCRATCH)
        truncate_scratch_tables()
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id,owner_id,parent_id,is_group,name,icon,status,is_secret,
                         created_at,updated_at,updated_at_utc)
                    VALUES (%s,%s,NULL,0,'Delete attachments','Idea',0,0,%s,%s,%s)
                    """,
                    (PROJECT_ID, OWNER, BASE, BASE, BASE),
                )
                for account, role in (
                    (UPLOADER, "viewer"),
                    (OTHER, "editor"),
                    (OWNER, "owner"),
                ):
                    cursor.execute(
                        """
                        INSERT INTO project_members
                            (id,project_id,user_id,username,display_name,email,role,
                             invited_at,updated_at_utc)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            f"member-{account}", PROJECT_ID, account, account,
                            account, f"{account}@example.test", role, BASE, BASE,
                        ),
                    )
                for memo_id, kind, creator in (
                    (MEMO_ID, "memo", OWNER),
                    (BACKLOG_ID, "backlog", UPLOADER),
                ):
                    cursor.execute(
                        """
                        INSERT INTO memos
                            (id,project_id,created_by,kind,title,content,status,
                             created_at,updated_at,updated_at_utc)
                        VALUES (%s,%s,%s,%s,'Delete attachment memo','',0,%s,%s,%s)
                        """,
                        (memo_id, PROJECT_ID, creator, kind, BASE, BASE, BASE),
                    )
        self._insert_attachment(ATTACH_UPLOADER, MEMO_ID, UPLOADER)
        self._insert_attachment(ATTACH_OTHER, MEMO_ID, OTHER)
        self._insert_attachment(
            ATTACH_DELETED, MEMO_ID, UPLOADER, deleted_at=BASE
        )
        self._insert_attachment(
            ATTACH_FAILED, MEMO_ID, UPLOADER, status="failed"
        )

    def _insert_attachment(
        self,
        attachment_id: str,
        memo_id: str,
        creator: str,
        *,
        status: str = "ready",
        deleted_at: datetime | None = None,
    ) -> None:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memo_attachments
                        (id,memo_id,origin_memo_id,created_by,file_name,content_type,
                         byte_size,is_image,sha256,object_key,status,created_at,
                         updated_at_utc,deleted_at)
                    VALUES (%s,%s,%s,%s,%s,'text/plain',4,0,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        attachment_id, memo_id, memo_id, creator,
                        f"{attachment_id}.txt", attachment_id[0] * 64,
                        canonical_object_key(memo_id, attachment_id), status,
                        BASE, BASE, deleted_at,
                    ),
                )

    def _row(self, attachment_id: str) -> dict:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM memo_attachments WHERE id=%s", (attachment_id,)
                )
                return cursor.fetchone()

    async def test_viewer_can_soft_delete_own_ready_attachment_without_storage_io(self) -> None:
        with patch.object(attachment_routes, "_attachment_storage") as storage:
            result = await delete_memo_attachments(
                MEMO_ID,
                DeleteAttachmentsRequest(attachmentIds=[ATTACH_UPLOADER]),
                user(UPLOADER),
            )
        storage.assert_not_called()
        self.assertEqual(
            result, {"deletedIds": [ATTACH_UPLOADER], "deletedCount": 1}
        )
        row = self._row(ATTACH_UPLOADER)
        self.assertIsNotNone(row["deleted_at"])
        self.assertGreater(row["updated_at_utc"], BASE)
        listed = await list_memo_attachments(MEMO_ID, user(UPLOADER))
        self.assertNotIn(ATTACH_UPLOADER, {item["id"] for item in listed})
        with self.assertRaises(HTTPException) as content:
            await get_attachment_content(
                ATTACH_UPLOADER, inline=False, user=user(UPLOADER)
            )
        self.assertEqual(content.exception.status_code, 404)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                changes, _ = collect_local_changes(
                    cursor, 0, 100, account_id=UPLOADER
                )
        refs = {(item["table"], item["rowId"]) for item in changes}
        self.assertIn(("memo_attachments", ATTACH_UPLOADER), refs)

    async def test_project_owner_and_service_superadmin_cannot_delete_other_upload(self) -> None:
        for actor in (user(OWNER, "admin"), user("service-owner", "superadmin")):
            with self.subTest(actor=actor["id"]), self.assertRaises(HTTPException) as denied:
                await delete_memo_attachments(
                    MEMO_ID,
                    DeleteAttachmentsRequest(attachmentIds=[ATTACH_UPLOADER]),
                    actor,
                )
            self.assertEqual(denied.exception.status_code, 403)
            self.assertIsNone(self._row(ATTACH_UPLOADER)["deleted_at"])

    async def test_revoked_membership_blocks_own_delete(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE project_members SET deleted_at=%s,updated_at_utc=%s "
                    "WHERE project_id=%s AND user_id=%s",
                    (BASE + timedelta(seconds=1), BASE + timedelta(seconds=1),
                     PROJECT_ID, UPLOADER),
                )
        with self.assertRaises(HTTPException) as denied:
            await delete_memo_attachments(
                MEMO_ID,
                DeleteAttachmentsRequest(attachmentIds=[ATTACH_UPLOADER]),
                user(UPLOADER),
            )
        self.assertEqual(denied.exception.status_code, 403)
        self.assertIsNone(self._row(ATTACH_UPLOADER)["deleted_at"])

    async def test_mixed_ownership_request_rolls_back_every_attachment(self) -> None:
        with self.assertRaises(HTTPException) as denied:
            await delete_memo_attachments(
                MEMO_ID,
                DeleteAttachmentsRequest(
                    attachmentIds=[ATTACH_UPLOADER, ATTACH_OTHER]
                ),
                user(UPLOADER),
            )
        self.assertEqual(denied.exception.status_code, 403)
        self.assertIsNone(self._row(ATTACH_UPLOADER)["deleted_at"])
        self.assertIsNone(self._row(ATTACH_OTHER)["deleted_at"])

    async def test_unknown_or_nonready_row_rolls_back_valid_selection(self) -> None:
        unknown = str(uuid.uuid4())
        for ids, expected in (
            ([ATTACH_UPLOADER, unknown], 404),
            ([ATTACH_UPLOADER, ATTACH_FAILED], 409),
        ):
            with self.subTest(ids=ids), self.assertRaises(HTTPException) as denied:
                await delete_memo_attachments(
                    MEMO_ID,
                    DeleteAttachmentsRequest(attachmentIds=ids),
                    user(UPLOADER),
                )
            self.assertEqual(denied.exception.status_code, expected)
            self.assertIsNone(self._row(ATTACH_UPLOADER)["deleted_at"])

    async def test_same_uploader_attachment_from_other_memo_rolls_back(self) -> None:
        other_memo = "delete-other-memo"
        other_attachment = "66666666-6666-4666-8666-666666666666"
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id,project_id,created_by,kind,title,content,status,
                         created_at,updated_at,updated_at_utc)
                    VALUES (%s,%s,%s,'memo','Other memo','',0,%s,%s,%s)
                    """,
                    (other_memo, PROJECT_ID, OWNER, BASE, BASE, BASE),
                )
        self._insert_attachment(other_attachment, other_memo, UPLOADER)
        with self.assertRaises(HTTPException) as denied:
            await delete_memo_attachments(
                MEMO_ID,
                DeleteAttachmentsRequest(
                    attachmentIds=[ATTACH_UPLOADER, other_attachment]
                ),
                user(UPLOADER),
            )
        self.assertEqual(denied.exception.status_code, 404)
        self.assertIsNone(self._row(ATTACH_UPLOADER)["deleted_at"])
        self.assertIsNone(self._row(other_attachment)["deleted_at"])

    async def test_own_already_deleted_row_is_idempotent(self) -> None:
        result = await delete_memo_attachments(
            MEMO_ID,
            DeleteAttachmentsRequest(attachmentIds=[ATTACH_DELETED]),
            user(UPLOADER),
        )
        self.assertEqual(
            result, {"deletedIds": [ATTACH_DELETED], "deletedCount": 0}
        )
        self.assertEqual(self._row(ATTACH_DELETED)["deleted_at"], BASE)

    async def test_concurrent_same_delete_serializes_to_one_new_tombstone(self) -> None:
        async def delete_once():
            return await asyncio.to_thread(
                lambda: asyncio.run(
                    delete_memo_attachments(
                        MEMO_ID,
                        DeleteAttachmentsRequest(
                            attachmentIds=[ATTACH_UPLOADER]
                        ),
                        user(UPLOADER),
                    )
                )
            )

        first, second = await asyncio.gather(delete_once(), delete_once())
        self.assertEqual(
            sorted((first["deletedCount"], second["deletedCount"])), [0, 1]
        )
        self.assertEqual(first["deletedIds"], [ATTACH_UPLOADER])
        self.assertEqual(second["deletedIds"], [ATTACH_UPLOADER])
        self.assertIsNotNone(self._row(ATTACH_UPLOADER)["deleted_at"])

    async def test_private_backlog_requires_exact_owner_and_live_membership(self) -> None:
        backlog_attachment = "55555555-5555-4555-8555-555555555555"
        self._insert_attachment(backlog_attachment, BACKLOG_ID, UPLOADER)
        with self.assertRaises(HTTPException) as denied:
            await delete_memo_attachments(
                BACKLOG_ID,
                DeleteAttachmentsRequest(attachmentIds=[backlog_attachment]),
                user(OTHER),
            )
        self.assertEqual(denied.exception.status_code, 403)
        allowed = await delete_memo_attachments(
            BACKLOG_ID,
            DeleteAttachmentsRequest(attachmentIds=[backlog_attachment]),
            user(UPLOADER),
        )
        self.assertEqual(allowed["deletedCount"], 1)

    async def test_ids_must_be_canonical_unique_uuid4_without_side_effects(self) -> None:
        for ids in (
            [ATTACH_UPLOADER, ATTACH_UPLOADER],
            [
                "abcdefab-cdef-4abc-8abc-abcdefabcdef",
                "ABCDEFAB-CDEF-4ABC-8ABC-ABCDEFABCDEF",
            ],
            ["not-a-uuid"],
        ):
            with self.subTest(ids=ids), self.assertRaises(HTTPException) as invalid:
                await delete_memo_attachments(
                    MEMO_ID,
                    DeleteAttachmentsRequest(attachmentIds=ids),
                    user(UPLOADER),
                )
            self.assertEqual(invalid.exception.status_code, 422)
            self.assertIsNone(self._row(ATTACH_UPLOADER)["deleted_at"])

    def test_sync_tombstone_and_undelete_require_exact_uploader(self) -> None:
        live = self._row(ATTACH_OTHER)
        tombstone = {
            **live,
            "updated_at_utc": iso_utc(BASE + timedelta(seconds=2)),
            "deleted_at": iso_utc(BASE + timedelta(seconds=2)),
            "created_at": live["created_at"].isoformat(timespec="milliseconds"),
        }
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                with self.assertRaises(HTTPException):
                    _authorize_sync_changes(cursor, [sync_change(tombstone)], OWNER)
                _authorize_sync_changes(cursor, [sync_change(tombstone)], OTHER)

        deleted = self._row(ATTACH_DELETED)
        for cleared_value in (None, ""):
            undelete = {
                **deleted,
                "updated_at_utc": iso_utc(BASE + timedelta(seconds=3)),
                "deleted_at": cleared_value,
                "created_at": deleted["created_at"].isoformat(timespec="milliseconds"),
            }
            with get_db_connection() as conn:
                with (
                    conn.cursor() as cursor,
                    self.subTest(cleared_value=cleared_value),
                    self.assertRaises(HTTPException),
                ):
                    _authorize_sync_changes(
                        cursor, [sync_change(undelete)], UPLOADER
                    )

    def test_sync_allows_viewer_own_tombstone_but_rejects_revoked_and_hard_delete(self) -> None:
        live = self._row(ATTACH_UPLOADER)
        tombstone = {
            **live,
            "updated_at_utc": iso_utc(BASE + timedelta(seconds=2)),
            "deleted_at": iso_utc(BASE + timedelta(seconds=2)),
            "created_at": live["created_at"].isoformat(timespec="milliseconds"),
        }
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                _authorize_sync_changes(
                    cursor, [sync_change(tombstone)], UPLOADER
                )
                with self.assertRaises(HTTPException):
                    _authorize_sync_changes(
                        cursor,
                        [{
                            "table": "memo_attachments",
                            "rowId": ATTACH_UPLOADER,
                            "op": "delete",
                            "seq": 2,
                            "row": None,
                        }],
                        UPLOADER,
                    )
                with self.assertRaises(HTTPException):
                    _authorize_sync_changes(
                        cursor,
                        [{
                            **sync_change(tombstone),
                            "op": "delete",
                            "seq": 3,
                        }],
                        UPLOADER,
                    )

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE project_members SET deleted_at=%s,updated_at_utc=%s "
                    "WHERE project_id=%s AND user_id=%s",
                    (BASE + timedelta(seconds=1), BASE + timedelta(seconds=1),
                     PROJECT_ID, UPLOADER),
                )
        with get_db_connection() as conn:
            with conn.cursor() as cursor, self.assertRaises(HTTPException):
                _authorize_sync_changes(
                    cursor, [sync_change(tombstone)], UPLOADER
                )


if __name__ == "__main__":
    unittest.main()
