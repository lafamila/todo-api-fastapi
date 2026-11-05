"""Project rename and atomic cross-project bulk move integration tests."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime

from fastapi import HTTPException

from tests.scratch_db import (
    assert_scratch_database,
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

SCRATCH_DB = assert_scratch_database(
    os.environ.get("TODO_SYNC_TEST_DB", "todo_sync_controls_test")
)
use_scratch_database(SCRATCH_DB)

from src.connectors import DB_CONFIG, get_db_connection  # noqa: E402
from src.models.base import (  # noqa: E402
    BulkMoveMemosRequest,
    CreateProjectRequest,
    MoveMemoRequest,
    UpdateProjectRequest,
)
from src.routers import memos, projects  # noqa: E402


OWNER = {
    "id": "controls-owner",
    "permission": "admin",
    "is_admin": True,
    "username": "owner",
    "display_name": "Owner",
    "email": "owner@example.test",
}
OTHER_OWNER = {
    **OWNER,
    "id": "other-owner",
    "username": "other",
    "email": "other@example.test",
}
OUTSIDER = {"id": "outsider", "permission": "user", "is_admin": False}
BASE = datetime(2026, 11, 5, 1, 2, 3)


@unittest.skipUnless(init_scratch_database(), f"MySQL scratch unavailable ({DB_CONFIG['database']})")
class ProjectControlsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        use_scratch_database(SCRATCH_DB)
        truncate_scratch_tables()

    async def _project(self, name: str, user: dict = OWNER) -> dict:
        return await projects.create_project(
            CreateProjectRequest(name=name, icon="Beer", isSecret=False), user
        )

    def _insert_memo(
        self,
        memo_id: str,
        project_id: str,
        title: str,
        *,
        kind: str = "memo",
        deleted_at=None,
    ) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memos
                      (id, project_id, created_by, kind, title, content, status,
                       deleted_at, created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, %s, %s, %s, 'body', 0, %s, %s, %s, %s)
                    """,
                    (memo_id, project_id, OWNER["id"], kind, title, deleted_at, BASE, BASE, BASE),
                )

    async def test_owner_renames_project_and_advances_sync_clock(self) -> None:
        project = await self._project("Before")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT updated_at_utc FROM projects WHERE id = %s", (project["id"],))
                before_clock = cursor.fetchone()["updated_at_utc"]
        renamed = await projects.rename_project(
            project["id"], UpdateProjectRequest(name="  After  "), OWNER
        )
        self.assertEqual(renamed["name"], "After")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT name, updated_at_utc FROM projects WHERE id = %s",
                    (project["id"],),
                )
                row = cursor.fetchone()
        self.assertEqual(row["name"], "After")
        self.assertGreaterEqual(row["updated_at_utc"], before_clock)

    async def test_rename_rejects_non_owner_blank_long_deleted_and_duplicate(self) -> None:
        first = await self._project("First")
        second = await self._project("Second")
        cases = (
            (first["id"], UpdateProjectRequest(name="Nope"), OUTSIDER, 403),
            (first["id"], UpdateProjectRequest(name="   "), OWNER, 422),
            (first["id"], UpdateProjectRequest(name="x" * 256), OWNER, 422),
            (second["id"], UpdateProjectRequest(name="First"), OWNER, 409),
        )
        for project_id, request, actor, status in cases:
            with self.subTest(status=status, name=request.name), self.assertRaises(HTTPException) as raised:
                await projects.rename_project(project_id, request, actor)
            self.assertEqual(raised.exception.status_code, status)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE projects SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
                    (BASE, BASE, first["id"]),
                )
        with self.assertRaises(HTTPException) as deleted:
            await projects.rename_project(first["id"], UpdateProjectRequest(name="Hidden"), OWNER)
        self.assertEqual(deleted.exception.status_code, 404)

    async def test_bulk_move_across_owned_roots_preserves_history_articles_and_edit_time(self) -> None:
        source = await self._project("Source")
        target = await self._project("Target")
        self._insert_memo("m1", source["id"], "One")
        self._insert_memo("m2", source["id"], "Two")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memo_versions
                       (id,memo_id,content,version,note,created_at,updated_at_utc)
                       VALUES ('v1','m1','old',1,NULL,%s,%s)""",
                    (BASE, BASE),
                )
                cursor.execute(
                    """INSERT INTO articles
                       (id,memo_id,project_id,author_id,author_slug,title,content,
                        published_version,created_at,published_at,updated_at)
                       VALUES ('a1','m1',%s,%s,'owner','One','body',1,%s,%s,%s)""",
                    (source["id"], OWNER["id"], BASE, BASE, BASE),
                )
        response = await memos.bulk_move_memos(
            BulkMoveMemosRequest(
                memoIds=["m2", "m1"],
                sourceProjectId=source["id"],
                targetProjectId=target["id"],
                sourceConfirmationName="Source",
                targetConfirmationName="Target",
            ),
            OWNER,
        )
        self.assertEqual([memo["id"] for memo in response["movedMemos"]], ["m2", "m1"])
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id,project_id,content,updated_at FROM memos WHERE id IN ('m1','m2') ORDER BY id")
                rows = cursor.fetchall()
                self.assertEqual({row["project_id"] for row in rows}, {target["id"]})
                self.assertTrue(all(row["content"] == "body" and row["updated_at"] == BASE for row in rows))
                cursor.execute("SELECT memo_id,content FROM memo_versions WHERE id='v1'")
                self.assertEqual(cursor.fetchone(), {"memo_id": "m1", "content": "old"})
                cursor.execute("SELECT project_id FROM articles WHERE id='a1'")
                self.assertEqual(cursor.fetchone()["project_id"], target["id"])

    async def test_bulk_move_validation_failures_roll_back_every_memo_and_article(self) -> None:
        source = await self._project("Source")
        target = await self._project("Target")
        self._insert_memo("m1", source["id"], "One")
        self._insert_memo("m2", source["id"], "Two")
        self._insert_memo("collision", target["id"], "Two")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO articles
                       (id,memo_id,project_id,author_id,author_slug,title,content,
                        published_version,created_at,published_at,updated_at)
                       VALUES ('a1','m1',%s,%s,'owner','One','body',1,%s,%s,%s)""",
                    (source["id"], OWNER["id"], BASE, BASE, BASE),
                )

        base = dict(
            memoIds=["m1", "m2"], sourceProjectId=source["id"], targetProjectId=target["id"],
            sourceConfirmationName="Source", targetConfirmationName="Target",
        )
        attempts = (
            ({**base, "sourceConfirmationName": " Source "}, 422),
            ({**base, "targetConfirmationName": "target"}, 422),
            (base, 409),
            ({**base, "memoIds": ["m1", "collision"]}, 422),
            ({**base, "memoIds": ["m1", "missing"]}, 404),
            ({**base, "memoIds": ["m1", "m1"]}, 422),
        )
        for body, expected in attempts:
            with self.subTest(expected=expected, body=body), self.assertRaises(HTTPException) as raised:
                await memos.bulk_move_memos(BulkMoveMemosRequest(**body), OWNER)
            self.assertEqual(raised.exception.status_code, expected)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT id,project_id FROM memos WHERE id IN ('m1','m2') ORDER BY id")
                    self.assertEqual({row["project_id"] for row in cursor.fetchall()}, {source["id"]})
                    cursor.execute("SELECT project_id FROM articles WHERE id='a1'")
                    self.assertEqual(cursor.fetchone()["project_id"], source["id"])

    async def test_bulk_move_requires_actual_owner_of_both_roots_and_regular_live_memos(self) -> None:
        source = await self._project("Source")
        foreign_target = await self._project("Other Target", OTHER_OWNER)
        owned_target = await self._project("Owned Target")
        self._insert_memo("m1", source["id"], "One")
        self._insert_memo("dead", source["id"], "Dead", deleted_at=BASE)
        foreign_request = dict(
            sourceProjectId=source["id"], targetProjectId=foreign_target["id"],
            sourceConfirmationName="Source", targetConfirmationName="Other Target",
        )
        with self.assertRaises(HTTPException) as not_both_owner:
            await memos.bulk_move_memos(
                BulkMoveMemosRequest(memoIds=["m1"], **foreign_request), OWNER
            )
        self.assertEqual(not_both_owner.exception.status_code, 403)

        owned_request = dict(
            sourceProjectId=source["id"], targetProjectId=owned_target["id"],
            sourceConfirmationName="Source", targetConfirmationName="Owned Target",
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM memos WHERE project_id = %s AND kind = 'backlog' AND deleted_at IS NULL",
                    (source["id"],),
                )
                backlog_id = cursor.fetchone()["id"]
        for ids, expected in ((["dead"], 404), ([backlog_id], 422)):
            with self.subTest(ids=ids), self.assertRaises(HTTPException) as raised:
                await memos.bulk_move_memos(BulkMoveMemosRequest(memoIds=ids, **owned_request), OWNER)
            self.assertEqual(raised.exception.status_code, expected)

    async def test_single_and_bulk_move_same_title_serialize_on_target(self) -> None:
        group = await self._project("Group")
        left = await projects.create_project(
            CreateProjectRequest(
                name="Left", icon="Beer", isSecret=False,
                parentProjectId=group["id"],
            ),
            OWNER,
        )
        right = await projects.create_project(
            CreateProjectRequest(
                name="Right", icon="Beer", isSecret=False,
                parentProjectId=group["id"],
            ),
            OWNER,
        )
        bulk_source = await self._project("Bulk Source")
        self._insert_memo("single-memo", left["id"], "Same title")
        self._insert_memo("bulk-memo", bulk_source["id"], "Same title")

        async def single():
            return await asyncio.to_thread(
                lambda: asyncio.run(
                    memos.move_memo(
                        "single-memo",
                        MoveMemoRequest(targetProjectId=right["id"]),
                        OWNER,
                    )
                )
            )

        async def bulk():
            return await asyncio.to_thread(
                lambda: asyncio.run(
                    memos.bulk_move_memos(
                        BulkMoveMemosRequest(
                            memoIds=["bulk-memo"],
                            sourceProjectId=bulk_source["id"],
                            targetProjectId=right["id"],
                            sourceConfirmationName="Bulk Source",
                            targetConfirmationName="Right",
                        ),
                        OWNER,
                    )
                )
            )

        outcomes = await asyncio.gather(single(), bulk(), return_exceptions=True)
        self.assertEqual(sum(not isinstance(item, Exception) for item in outcomes), 1)
        rejected = next(item for item in outcomes if isinstance(item, HTTPException))
        self.assertEqual(rejected.status_code, 409)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM memos WHERE project_id = %s AND title = 'Same title' AND deleted_at IS NULL",
                    (right["id"],),
                )
                self.assertEqual(len(cursor.fetchall()), 1)


if __name__ == "__main__":
    unittest.main()
