"""Project hierarchy/backlog integration tests against a guarded scratch DB."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime

from fastapi import HTTPException

from tests.scratch_db import (
    assert_scratch_database,
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

from src.connectors import get_db_connection
from src.auth_utils import get_current_user
from src.models.base import (
    CreateProjectRequest,
    DeleteProjectRequest,
    InviteMemberRequest,
    MoveMemoRequest,
)
from src.routers import articles, memos, projects
from src.utils import backlog_id


SCRATCH_DB = assert_scratch_database("teddynote_sync_hierarchy_api_test")
OWNER = {"id": "owner-1", "permission": "admin", "is_admin": True,
         "username": "owner", "display_name": "Owner", "email": "owner@example.test"}
MEMBER = {"id": "member-1", "permission": "user", "is_admin": False}


async def _create_root(name: str = "Root") -> dict:
    return await projects.create_project(
        CreateProjectRequest(name=name, icon="Beer", isSecret=False), OWNER
    )


class ProjectHierarchyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        use_scratch_database(SCRATCH_DB)
        if not init_scratch_database():
            raise unittest.SkipTest("MySQL hierarchy scratch database unavailable")

    def setUp(self) -> None:
        use_scratch_database(SCRATCH_DB)
        truncate_scratch_tables()

    def test_create_project_http_dependency_allows_project_level_owner_gate(self) -> None:
        route = next(
            route for route in projects.router.routes
            if route.path == "/api/projects" and "POST" in route.methods
        )
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        self.assertIn(get_current_user, dependency_calls)

    async def test_first_child_moves_all_rows_and_copies_live_roles(self) -> None:
        root = await _create_root()
        old_updated = datetime(2026, 1, 2, 3, 4, 5)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO project_members
                       (id, project_id, user_id, role, invited_at, updated_at_utc)
                       VALUES ('member-row', %s, %s, 'editor', %s, %s)""",
                    (root["id"], MEMBER["id"], old_updated, old_updated),
                )
                for memo_id, kind, deleted in (
                    ("memo-live", "memo", None),
                    ("memo-deleted", "memo", old_updated),
                ):
                    cursor.execute(
                        """INSERT INTO memos
                           (id, project_id, created_by, kind, title, content, status,
                            deleted_at, created_at, updated_at, updated_at_utc)
                           VALUES (%s, %s, %s, %s, %s, 'body', 0, %s, %s, %s, %s)""",
                        (memo_id, root["id"], OWNER["id"], kind, memo_id, deleted,
                         old_updated, old_updated, old_updated),
                    )
                cursor.execute(
                    "UPDATE memos SET content = 'body', updated_at = %s, updated_at_utc = %s "
                    "WHERE id = %s",
                    (old_updated, old_updated, backlog_id(root["id"], OWNER["id"])),
                )
                cursor.execute(
                    """INSERT INTO memo_versions
                       (id, memo_id, content, version, created_at, updated_at_utc)
                       VALUES ('version-1', 'memo-live', 'old', 1, %s, %s)""",
                    (old_updated, old_updated),
                )
                cursor.execute(
                    """INSERT INTO articles
                       (id, memo_id, project_id, author_id, author_slug, title, content,
                        published_version, created_at, published_at, updated_at)
                       VALUES ('article-1', 'memo-live', %s, %s, 'owner', 'memo-live',
                               'body', 1, %s, %s, %s)""",
                    (root["id"], OWNER["id"], old_updated, old_updated, old_updated),
                )

        child = await projects.create_project(
            CreateProjectRequest(
                name="First child", icon="Coffee", isSecret=False,
                parentProjectId=root["id"],
            ),
            OWNER,
        )

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT is_group FROM projects WHERE id = %s", (root["id"],))
                self.assertEqual(cursor.fetchone()["is_group"], 1)
                cursor.execute("SELECT id, project_id, updated_at FROM memos ORDER BY id")
                rows = cursor.fetchall()
                self.assertEqual({row["project_id"] for row in rows}, {child["id"]})
                self.assertTrue(all(row["updated_at"] == old_updated for row in rows))
                cursor.execute("SELECT memo_id FROM memo_versions WHERE id = 'version-1'")
                self.assertEqual(cursor.fetchone()["memo_id"], "memo-live")
                cursor.execute("SELECT project_id FROM articles WHERE id = 'article-1'")
                self.assertEqual(cursor.fetchone()["project_id"], child["id"])
                cursor.execute(
                    "SELECT user_id, role FROM project_members WHERE project_id = %s AND deleted_at IS NULL",
                    (child["id"],),
                )
                roles = {row["user_id"]: row["role"] for row in cursor.fetchall()}
                self.assertEqual(roles, {OWNER["id"]: "owner", MEMBER["id"]: "editor"})

        backlog = await memos.get_project_backlog(child["id"], OWNER)
        self.assertEqual(backlog["id"], backlog_id(root["id"], OWNER["id"]))
        with self.assertRaises(HTTPException) as group_backlog:
            await memos.get_project_backlog(root["id"], OWNER)
        self.assertEqual(group_backlog.exception.status_code, 409)

    async def test_later_child_does_not_inherit_members_and_move_requires_root_creator(self) -> None:
        root = await _create_root()
        first = await projects.create_project(
            CreateProjectRequest(name="One", icon="Beer", isSecret=False, parentProjectId=root["id"]), OWNER
        )
        await projects.invite_member(
            root["id"],
            InviteMemberRequest(userId=MEMBER["id"], role="editor", includeAllSubprojects=True),
            OWNER,
        )
        second = await projects.create_project(
            CreateProjectRequest(name="Two", icon="Beer", isSecret=False, parentProjectId=root["id"]), OWNER
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM project_members WHERE project_id = %s AND user_id = %s AND deleted_at IS NULL",
                    (second["id"], MEMBER["id"]),
                )
                self.assertEqual(cursor.fetchone()["count"], 0)
                moment = datetime(2026, 1, 1)
                cursor.execute(
                    """INSERT INTO memos
                       (id, project_id, created_by, kind, title, content, status, created_at, updated_at, updated_at_utc)
                       VALUES ('moving', %s, %s, 'memo', 'Move', 'body', 0, %s, %s, %s)""",
                    (first["id"], OWNER["id"], moment, moment, moment),
                )
        with self.assertRaises(HTTPException) as denied:
            await memos.move_memo("moving", MoveMemoRequest(targetProjectId=second["id"]), MEMBER)
        self.assertEqual(denied.exception.status_code, 403)
        moved = await memos.move_memo("moving", MoveMemoRequest(targetProjectId=second["id"]), OWNER)
        self.assertEqual(moved["projectId"], second["id"])

    async def test_actual_project_owner_can_create_child_without_service_admin_role(self) -> None:
        root = await _create_root()
        owner_as_user = {**OWNER, "permission": "user", "is_admin": False}
        child = await projects.create_project(
            CreateProjectRequest(
                name="Owner child", icon="Beer", isSecret=False,
                parentProjectId=root["id"],
            ),
            owner_as_user,
        )
        self.assertEqual(child["parentProjectId"], root["id"])
        with self.assertRaises(HTTPException) as denied:
            await projects.create_project(
                CreateProjectRequest(name="New root", icon="Beer", isSecret=False),
                owner_as_user,
            )
        self.assertEqual(denied.exception.status_code, 403)

    async def test_backlog_is_singleton_and_hidden_from_regular_memo_list(self) -> None:
        root = await _create_root()
        first = await memos.create_project_backlog(root["id"], OWNER)
        second = await memos.create_project_backlog(root["id"], OWNER)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["kind"], "backlog")
        self.assertEqual(await memos.get_project_memos(root["id"], OWNER), [])

    async def test_concurrent_backlog_initialization_converges_to_one_row(self) -> None:
        root = await _create_root()
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                moment = datetime(2026, 1, 1)
                cursor.execute(
                    "UPDATE memos SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
                    (moment, moment, backlog_id(root["id"], OWNER["id"])),
                )

        async def create_in_thread():
            return await asyncio.to_thread(
                lambda: asyncio.run(memos.create_project_backlog(root["id"], OWNER))
            )

        first, second = await asyncio.gather(create_in_thread(), create_in_thread())
        self.assertEqual(first["id"], second["id"])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM memos WHERE project_id = %s AND kind = 'backlog' AND deleted_at IS NULL",
                    (root["id"],),
                )
                self.assertEqual(cursor.fetchone()["count"], 1)

    async def test_concurrent_first_children_move_once_and_only_first_copies_members(self) -> None:
        root = await _create_root()
        moment = datetime(2026, 1, 1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO project_members
                       (id, project_id, user_id, role, invited_at, updated_at_utc)
                       VALUES ('race-member', %s, %s, 'editor', %s, %s)""",
                    (root["id"], MEMBER["id"], moment, moment),
                )
                cursor.execute(
                    """INSERT INTO memos
                       (id, project_id, created_by, kind, title, content, status, created_at, updated_at, updated_at_utc)
                       VALUES ('race-memo', %s, %s, 'memo', 'Race', 'body', 0, %s, %s, %s)""",
                    (root["id"], OWNER["id"], moment, moment, moment),
                )

        async def create_child(name: str):
            return await asyncio.to_thread(
                lambda: asyncio.run(projects.create_project(
                    CreateProjectRequest(name=name, icon="Beer", isSecret=False, parentProjectId=root["id"]),
                    OWNER,
                ))
            )

        children = await asyncio.gather(create_child("Race A"), create_child("Race B"))
        child_ids = {child["id"] for child in children}
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT project_id FROM memos WHERE id = 'race-memo'")
                memo_project_id = cursor.fetchone()["project_id"]
                self.assertIn(memo_project_id, child_ids)
                cursor.execute(
                    "SELECT project_id FROM project_members WHERE user_id = %s AND project_id IN (%s, %s) AND deleted_at IS NULL",
                    (MEMBER["id"], *child_ids),
                )
                self.assertEqual([row["project_id"] for row in cursor.fetchall()], [memo_project_id])

    async def test_group_stays_group_and_recreated_child_does_not_recopy_root_members(self) -> None:
        root = await _create_root()
        first = await projects.create_project(
            CreateProjectRequest(name="First", icon="Beer", isSecret=False, parentProjectId=root["id"]), OWNER
        )
        await projects.invite_member(
            root["id"], InviteMemberRequest(userId=MEMBER["id"], role="editor"), OWNER
        )
        await projects.delete_project(
            first["id"], DeleteProjectRequest(confirmationName="First"), OWNER
        )
        replacement = await projects.create_project(
            CreateProjectRequest(name="Replacement", icon="Beer", isSecret=False, parentProjectId=root["id"]), OWNER
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT is_group FROM projects WHERE id = %s", (root["id"],))
                self.assertEqual(cursor.fetchone()["is_group"], 1)
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM project_members WHERE project_id = %s AND user_id = %s AND deleted_at IS NULL",
                    (replacement["id"], MEMBER["id"]),
                )
                self.assertEqual(cursor.fetchone()["count"], 0)
        with self.assertRaises(HTTPException) as no_group_memos:
            await memos.get_project_memos(root["id"], OWNER)
        self.assertEqual(no_group_memos.exception.status_code, 409)
        moment = datetime(2026, 1, 1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memos
                       (id, project_id, created_by, kind, title, content, status,
                        created_at, updated_at, updated_at_utc)
                       VALUES ('stale-group-memo', %s, %s, 'memo', 'Stale', 'hidden', 0, %s, %s, %s)""",
                    (root["id"], OWNER["id"], moment, moment, moment),
                )
        with self.assertRaises(HTTPException) as direct_hidden:
            await memos.get_memo("stale-group-memo", OWNER)
        self.assertEqual(direct_hidden.exception.status_code, 404)

    async def test_child_only_member_sees_parent_for_navigation_without_parent_access(self) -> None:
        root = await _create_root()
        first = await projects.create_project(
            CreateProjectRequest(name="First", icon="Beer", isSecret=False, parentProjectId=root["id"]), OWNER
        )
        child_only = {"id": "child-only", "permission": "user", "is_admin": False}
        await projects.invite_member(
            first["id"], InviteMemberRequest(userId=child_only["id"], role="viewer"), OWNER
        )
        visible = await projects.get_projects(child_only)
        by_id = {item["id"]: item for item in visible}
        self.assertTrue(by_id[root["id"]]["navigationOnly"])
        self.assertFalse(by_id[root["id"]]["canWrite"])
        self.assertEqual(by_id[first["id"]]["role"], "viewer")
        with self.assertRaises(HTTPException) as blocked:
            await projects.get_project_members(root["id"], child_only)
        self.assertEqual(blocked.exception.status_code, 403)

    async def test_delete_requires_exact_name_and_creator_and_keeps_rows(self) -> None:
        root = await _create_root("Exact Root")
        child = await projects.create_project(
            CreateProjectRequest(name="Child", icon="Beer", isSecret=False, parentProjectId=root["id"]), OWNER
        )
        with self.assertRaises(HTTPException) as mismatch:
            await projects.delete_project(root["id"], DeleteProjectRequest(confirmationName="exact root"), OWNER)
        self.assertEqual(mismatch.exception.status_code, 422)
        with self.assertRaises(HTTPException) as denied:
            await projects.delete_project(root["id"], DeleteProjectRequest(confirmationName="Exact Root"), MEMBER)
        self.assertEqual(denied.exception.status_code, 403)
        result = await projects.delete_project(
            root["id"], DeleteProjectRequest(confirmationName="Exact Root"), OWNER
        )
        self.assertEqual(result["deletedProjects"], 2)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS count FROM projects WHERE id IN (%s, %s)", (root["id"], child["id"]))
                self.assertEqual(cursor.fetchone()["count"], 2)
        self.assertEqual(await projects.get_projects(MEMBER), [])

    async def test_deleted_project_articles_are_preserved_but_not_public(self) -> None:
        root = await _create_root("Published Root")
        moment = datetime(2026, 1, 1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memos
                       (id, project_id, created_by, kind, title, content, status, created_at, updated_at, updated_at_utc)
                       VALUES ('published-memo', %s, %s, 'memo', 'Published', 'body', 0, %s, %s, %s)""",
                    (root["id"], OWNER["id"], moment, moment, moment),
                )
                cursor.execute(
                    """INSERT INTO articles
                       (id, memo_id, project_id, author_id, author_slug, title, content,
                        published_version, created_at, published_at, updated_at)
                       VALUES ('published-article', 'published-memo', %s, %s, 'owner',
                               'Published', 'body', 1, %s, %s, %s)""",
                    (root["id"], OWNER["id"], moment, moment, moment),
                )
        await projects.delete_project(
            root["id"], DeleteProjectRequest(confirmationName="Published Root"), OWNER
        )
        self.assertEqual(await articles.get_articles(), [])
        with self.assertRaises(HTTPException) as missing:
            await articles.get_article("published-article")
        self.assertEqual(missing.exception.status_code, 404)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM articles WHERE id = 'published-article'")
                self.assertIsNotNone(cursor.fetchone())


if __name__ == "__main__":
    unittest.main()
