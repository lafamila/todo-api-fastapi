"""Account-private backlog authorization and legacy migration tests."""

from __future__ import annotations

import asyncio
import os
import threading
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException

from tests.scratch_db import (
    assert_scratch_database,
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

SCRATCH_DB = assert_scratch_database(
    os.environ.get("TODO_PRIVATE_BACKLOG_TEST_DB", "todo_sync_private_backlog_test")
)
use_scratch_database(SCRATCH_DB)

from src.connectors import DB_CONFIG, _init_sync_schema, get_db_connection  # noqa: E402
from src.models.base import CreateProjectRequest, InviteMemberRequest, UpdateMemoRequest  # noqa: E402
from src.routers import attachments, memos, projects  # noqa: E402
from src.services.attachment_storage import canonical_object_key  # noqa: E402
from src.services.realtime import MemoRealtimeServer  # noqa: E402
from src.utils import (  # noqa: E402
    backlog_id,
    can_read_memo,
    can_write_memo,
    require_memo_access,
    require_locked_memo_access,
)


OWNER = {
    "id": "private-owner",
    "permission": "admin",
    "is_admin": True,
    "username": "owner",
    "display_name": "Owner",
    "email": "owner@example.test",
}
VIEWER = {"id": "private-viewer", "permission": "user", "is_admin": False}
SERVICE_OWNER = {"id": "service-owner", "permission": "owner", "is_admin": True}
BASE = datetime(2026, 11, 5, 3, 0, 0)


@unittest.skipUnless(init_scratch_database(), f"MySQL scratch unavailable ({DB_CONFIG['database']})")
class PrivateBacklogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        use_scratch_database(SCRATCH_DB)
        truncate_scratch_tables()

    async def _project_with_viewer(self) -> dict:
        project = await projects.create_project(
            CreateProjectRequest(name="Private", icon="Beer", isSecret=False),
            OWNER,
        )
        await projects.invite_member(
            project["id"],
            InviteMemberRequest(userId=VIEWER["id"], role="viewer"),
            OWNER,
        )
        return project

    async def test_each_member_gets_deterministic_private_backlog(self) -> None:
        project = await self._project_with_viewer()
        owner_backlog = await memos.get_project_backlog(project["id"], OWNER)
        self.assertEqual(owner_backlog["id"], backlog_id(project["id"], OWNER["id"]))
        self.assertIsNone(await memos.get_project_backlog(project["id"], VIEWER))
        viewer_backlog = await memos.create_project_backlog(project["id"], VIEWER)
        again = await memos.create_project_backlog(project["id"], VIEWER)
        self.assertEqual(viewer_backlog["id"], backlog_id(project["id"], VIEWER["id"]))
        self.assertEqual(again["id"], viewer_backlog["id"])
        self.assertNotEqual(owner_backlog["id"], viewer_backlog["id"])

    async def test_owner_and_service_owner_cannot_read_or_write_another_backlog(self) -> None:
        project = await self._project_with_viewer()
        viewer_backlog = await memos.create_project_backlog(project["id"], VIEWER)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                for actor in (OWNER, SERVICE_OWNER):
                    with self.subTest(actor=actor["id"]):
                        self.assertFalse(can_read_memo(cursor, viewer_backlog["id"], actor))
                        self.assertFalse(can_write_memo(cursor, viewer_backlog["id"], actor))
                        with self.assertRaises(HTTPException) as denied:
                            require_memo_access(cursor, viewer_backlog["id"], actor)
                        self.assertEqual(denied.exception.status_code, 403)
                self.assertTrue(can_read_memo(cursor, viewer_backlog["id"], VIEWER))
                self.assertTrue(can_write_memo(cursor, viewer_backlog["id"], VIEWER))

        with patch.object(memos, "_require_memo_write_lease"):
            updated = await memos.update_memo(
                viewer_backlog["id"],
                UpdateMemoRequest(content="viewer private"),
                VIEWER,
                lease_token="test",
            )
            self.assertEqual(updated["content"], "viewer private")
            with self.assertRaises(HTTPException) as owner_denied:
                await memos.update_memo(
                    viewer_backlog["id"],
                    UpdateMemoRequest(content="owner overwrite"),
                    OWNER,
                    lease_token="test",
                )
            self.assertEqual(owner_denied.exception.status_code, 403)

    async def test_regular_memo_sharing_is_unchanged(self) -> None:
        project = await self._project_with_viewer()
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memos
                       (id,project_id,created_by,kind,title,content,status,created_at,updated_at,updated_at_utc)
                       VALUES ('regular',%s,%s,'memo','Regular','body',0,%s,%s,%s)""",
                    (project["id"], OWNER["id"], BASE, BASE, BASE),
                )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self.assertTrue(can_read_memo(cursor, "regular", VIEWER))
                self.assertFalse(can_write_memo(cursor, "regular", VIEWER))
                self.assertTrue(can_read_memo(cursor, "regular", SERVICE_OWNER))
                self.assertTrue(can_write_memo(cursor, "regular", SERVICE_OWNER))

    async def test_versions_and_socket_access_follow_private_owner(self) -> None:
        project = await self._project_with_viewer()
        viewer_backlog = await memos.create_project_backlog(project["id"], VIEWER)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memo_versions
                       (id,memo_id,content,version,note,created_at,updated_at_utc)
                       VALUES ('private-v1',%s,'secret history',1,NULL,%s,%s)""",
                    (viewer_backlog["id"], BASE, BASE),
                )
        versions = await memos.get_memo_versions(viewer_backlog["id"], True, VIEWER)
        self.assertEqual(versions[0]["content"], "secret history")
        with self.assertRaises(HTTPException) as denied:
            await memos.get_memo_versions(viewer_backlog["id"], True, OWNER)
        self.assertEqual(denied.exception.status_code, 403)

        server = MemoRealtimeServer.__new__(MemoRealtimeServer)
        self.assertTrue(
            server._can_access_memo(
                viewer_backlog["id"],
                {"user_id": VIEWER["id"], "permission": "user"},
                True,
            )
        )
        self.assertFalse(
            server._can_access_memo(
                viewer_backlog["id"],
                {"user_id": OWNER["id"], "permission": "owner"},
                False,
            )
        )

    async def test_attachment_metadata_inherits_private_backlog_scope(self) -> None:
        project = await self._project_with_viewer()
        viewer_backlog = await memos.create_project_backlog(project["id"], VIEWER)
        attachment_id = "123e4567-e89b-42d3-a456-426614174000"
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memo_attachments
                       (id,memo_id,origin_memo_id,created_by,file_name,content_type,byte_size,is_image,
                        sha256,object_key,status,created_at,updated_at_utc,deleted_at)
                       VALUES (%s,%s,%s,%s,'private.png','image/png',8,1,%s,%s,'ready',%s,%s,NULL)""",
                    (
                        attachment_id,
                        viewer_backlog["id"],
                        viewer_backlog["id"],
                        VIEWER["id"],
                        "0" * 64,
                        canonical_object_key(viewer_backlog["id"], attachment_id),
                        BASE,
                        BASE,
                    ),
                )
        own = await attachments.list_memo_attachments(viewer_backlog["id"], VIEWER)
        self.assertEqual([item["id"] for item in own], [attachment_id])
        for actor in (OWNER, SERVICE_OWNER):
            with self.subTest(actor=actor["id"]), self.assertRaises(HTTPException) as denied:
                await attachments.list_memo_attachments(viewer_backlog["id"], actor)
            self.assertEqual(denied.exception.status_code, 403)

    async def test_first_child_moves_every_private_backlog_without_copying(self) -> None:
        project = await self._project_with_viewer()
        owner_backlog = await memos.get_project_backlog(project["id"], OWNER)
        viewer_backlog = await memos.create_project_backlog(project["id"], VIEWER)
        child = await projects.create_project(
            CreateProjectRequest(
                name="Child",
                icon="Beer",
                isSecret=False,
                parentProjectId=project["id"],
            ),
            OWNER,
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id,project_id,created_by FROM memos WHERE kind='backlog' AND deleted_at IS NULL"
                )
                rows = cursor.fetchall()
        self.assertEqual(
            {(row["id"], row["project_id"], row["created_by"]) for row in rows},
            {
                (owner_backlog["id"], child["id"], OWNER["id"]),
                (viewer_backlog["id"], child["id"], VIEWER["id"]),
            },
        )

    async def test_membership_removal_hides_then_reinvite_restores_same_backlog(self) -> None:
        project = await self._project_with_viewer()
        backlog = await memos.create_project_backlog(project["id"], VIEWER)
        with patch.object(memos, "_require_memo_write_lease"):
            await memos.update_memo(
                backlog["id"],
                UpdateMemoRequest(content="preserved private body"),
                VIEWER,
                lease_token="test",
            )
        await projects.remove_member(project["id"], VIEWER["id"], OWNER)
        with self.assertRaises(HTTPException) as hidden:
            await memos.get_memo(backlog["id"], VIEWER)
        self.assertEqual(hidden.exception.status_code, 403)
        with self.assertRaises(HTTPException) as no_project_backlog:
            await memos.get_project_backlog(project["id"], VIEWER)
        self.assertEqual(no_project_backlog.exception.status_code, 403)

        await projects.invite_member(
            project["id"],
            InviteMemberRequest(userId=VIEWER["id"], role="viewer"),
            OWNER,
        )
        restored = await memos.get_project_backlog(project["id"], VIEWER)
        self.assertEqual(restored["id"], backlog["id"])
        self.assertEqual(restored["content"], "preserved private body")

    async def test_locked_access_serializes_membership_revoke_and_finalize_denies(self) -> None:
        project = await self._project_with_viewer()
        backlog = await memos.create_project_backlog(project["id"], VIEWER)
        locked = threading.Event()
        release = threading.Event()

        def reserve_metadata() -> None:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    require_locked_memo_access(
                        cursor, backlog["id"], VIEWER, write=True
                    )
                    locked.set()
                    self.assertTrue(release.wait(timeout=5))
                    cursor.execute(
                        """INSERT INTO memo_attachments
                           (id,memo_id,origin_memo_id,created_by,file_name,content_type,
                            byte_size,is_image,sha256,object_key,status,created_at,
                            updated_at_utc,deleted_at)
                           VALUES ('33333333-3333-4333-8333-333333333333',%s,%s,%s,
                                   'race.bin','application/octet-stream',1,0,%s,%s,
                                   'uploading',%s,%s,NULL)""",
                        (
                            backlog["id"],
                            backlog["id"],
                            VIEWER["id"],
                            "3" * 64,
                            canonical_object_key(
                                backlog["id"],
                                "33333333-3333-4333-8333-333333333333",
                            ),
                            BASE,
                            BASE,
                        ),
                    )

        reservation = asyncio.create_task(asyncio.to_thread(reserve_metadata))
        self.assertTrue(await asyncio.to_thread(locked.wait, 5))
        revoke = asyncio.create_task(
            asyncio.to_thread(
                lambda: asyncio.run(
                    projects.remove_member(project["id"], VIEWER["id"], OWNER)
                )
            )
        )
        await asyncio.sleep(0.1)
        self.assertFalse(revoke.done())
        release.set()
        await reservation
        await revoke

        with get_db_connection() as conn:
            with conn.cursor() as cursor, self.assertRaises(HTTPException) as denied:
                require_locked_memo_access(
                    cursor, backlog["id"], VIEWER, write=True
                )
        self.assertEqual(denied.exception.status_code, 403)

    async def test_legacy_null_creator_backfills_to_project_owner_without_mutation(self) -> None:
        project = await self._project_with_viewer()
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM memos WHERE project_id=%s AND kind='backlog'",
                    (project["id"],),
                )
                cursor.execute(
                    """INSERT INTO memos
                       (id,project_id,created_by,kind,title,content,status,created_at,updated_at,updated_at_utc)
                       VALUES ('legacy-backlog',%s,NULL,'backlog','백로그','legacy body',0,%s,%s,%s)""",
                    (project["id"], BASE, BASE, BASE),
                )
                cursor.execute(
                    """INSERT INTO memo_versions
                       (id,memo_id,content,version,note,created_at,updated_at_utc)
                       VALUES ('legacy-v1','legacy-backlog','history',1,NULL,%s,%s)""",
                    (BASE, BASE),
                )
                _init_sync_schema(cursor)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id,created_by,content,updated_at FROM memos WHERE id='legacy-backlog'"
                )
                row = cursor.fetchone()
                cursor.execute("SELECT memo_id,content FROM memo_versions WHERE id='legacy-v1'")
                version = cursor.fetchone()
        self.assertEqual(
            row,
            {
                "id": "legacy-backlog",
                "created_by": OWNER["id"],
                "content": "legacy body",
                "updated_at": BASE,
            },
        )
        self.assertEqual(version, {"memo_id": "legacy-backlog", "content": "history"})


if __name__ == "__main__":
    unittest.main()
