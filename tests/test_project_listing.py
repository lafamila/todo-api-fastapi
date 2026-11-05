"""실제 DB에서 프로젝트 목록의 SQL 호환성·정렬·권한 범위를 검증한다."""

from __future__ import annotations

import unittest
from datetime import datetime

from tests.scratch_db import (
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)
from src.connectors import get_db_connection
from src.models.base import CreateProjectRequest, InviteMemberRequest
from src.routers import projects


OWNER = {"id": "listing-owner", "permission": "admin", "is_admin": True}
EDITOR = {"id": "listing-editor", "permission": "user", "is_admin": False}
VIEWER = {"id": "listing-viewer", "permission": "user", "is_admin": False}
OUTSIDER = {"id": "listing-outsider", "permission": "user", "is_admin": False}
SERVICE_OWNER = {"id": "listing-service-owner", "permission": "owner", "is_admin": True}


class ProjectListingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        use_scratch_database()
        if not init_scratch_database():
            raise unittest.SkipTest("project listing scratch database unavailable")

    def setUp(self) -> None:
        use_scratch_database()
        truncate_scratch_tables()

    async def create(self, name: str, *, parent_id: str | None = None, owner=None):
        return await projects.create_project(
            CreateProjectRequest(
                name=name, icon="Beer", isSecret=False, parentProjectId=parent_id,
            ),
            owner or OWNER,
        )

    async def test_flat_project_owner_and_empty_member_lists(self) -> None:
        root = await self.create("Standalone")
        for actor, expected_ids in (
            (OWNER, [root["id"]]),
            (SERVICE_OWNER, [root["id"]]),
            (OUTSIDER, []),
        ):
            with self.subTest(permission=actor["permission"], user=actor["id"]):
                result = await projects.get_projects(actor)
                self.assertEqual([row["id"] for row in result], expected_ids)

    async def test_hierarchy_listing_is_ordered_unique_and_permission_scoped(self) -> None:
        root = await self.create("Group")
        await projects.invite_member(
            root["id"], InviteMemberRequest(userId=EDITOR["id"], role="editor"), OWNER,
        )
        first = await self.create("First child", parent_id=root["id"])
        second = await self.create("Second child", parent_id=root["id"])
        await projects.invite_member(
            second["id"], InviteMemberRequest(userId=EDITOR["id"], role="viewer"), OWNER,
        )
        await projects.invite_member(
            second["id"], InviteMemberRequest(userId=VIEWER["id"], role="viewer"), OWNER,
        )
        hidden = await self.create("Unshared child", parent_id=root["id"])
        deleted = await self.create("Deleted child", parent_id=root["id"])
        flat = await self.create("Standalone")
        other_owner = {"id": "listing-other-owner", "permission": "admin", "is_admin": True}
        unrelated = await self.create("Unrelated", owner=other_owner)
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                for project, month, day in (
                    (root, 1, 1), (unrelated, 2, 1), (flat, 3, 1),
                    (first, 4, 1), (second, 4, 2), (hidden, 4, 3), (deleted, 4, 4),
                ):
                    cursor.execute(
                        "UPDATE projects SET created_at = %s WHERE id = %s",
                        (datetime(2026, month, day), project["id"]),
                    )
                cursor.execute(
                    "UPDATE projects SET deleted_at = %s WHERE id = %s",
                    (datetime(2026, 5, 1), deleted["id"]),
                )

        for actor, expected_ids in (
            (OWNER, [root["id"], first["id"], second["id"], hidden["id"], flat["id"]]),
            (EDITOR, [root["id"], first["id"], second["id"]]),
            (VIEWER, [root["id"], second["id"]]),
            (OUTSIDER, []),
            (SERVICE_OWNER, [root["id"], first["id"], second["id"], hidden["id"], unrelated["id"], flat["id"]]),
        ):
            with self.subTest(permission=actor["permission"], user=actor["id"]):
                result = await projects.get_projects(actor)
                ids = [row["id"] for row in result]
                self.assertEqual(ids, expected_ids)
                self.assertEqual(len(ids), len(set(ids)))

        by_id = {row["id"]: row for row in await projects.get_projects(EDITOR)}
        self.assertEqual(by_id[first["id"]]["role"], "editor")
        self.assertEqual(by_id[second["id"]]["role"], "viewer")
        parent = (await projects.get_projects(VIEWER))[0]
        self.assertTrue(parent["navigationOnly"])
        self.assertIsNone(parent["role"])
        self.assertFalse(parent["canManage"])
        self.assertFalse(parent["canWrite"])
