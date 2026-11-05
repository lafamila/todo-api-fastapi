"""Hierarchy/backlog sync security and convergence tests on a dedicated scratch DB."""

from __future__ import annotations

import unittest
import os
from datetime import datetime, timedelta

from fastapi import HTTPException

from tests.scratch_db import (
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

use_scratch_database("todo_sync_hierarchy_test")

from src.connectors import DB_CONFIG, get_db_connection  # noqa: E402
from src.routers.sync import (  # noqa: E402
    _authorize_sync_changes,
    _require_memo_write_access,
    _revalidate_after_sync_push,
)
from src.services.sync_daemon import SyncDaemon  # noqa: E402
from src.services.merge import MergeError, merge_memos, merge_projects  # noqa: E402
from src.services.sync_apply import SIDE_CLIENT, SIDE_SERVER, apply_changes  # noqa: E402
from src.services.sync_store import read_changes  # noqa: E402
from src.timeutil import iso_utc  # noqa: E402


ACCOUNT = "hierarchy-account"
OTHER = "other-account"
BASE = datetime(2026, 11, 4, 0, 0, 0)
REACHABLE = init_scratch_database()


def project_row(
    project_id: str,
    *,
    owner: str = ACCOUNT,
    parent_id: str | None = None,
    is_group: bool = False,
    clock: datetime = BASE,
    deleted_at: str | None = None,
) -> dict:
    return {
        "id": project_id,
        "owner_id": owner,
        "parent_id": parent_id,
        "is_group": is_group,
        "name": project_id,
        "icon": "Beer",
        "status": 0,
        "is_secret": False,
        "created_at": BASE.isoformat(timespec="milliseconds"),
        "updated_at": BASE.isoformat(timespec="milliseconds"),
        "updated_at_utc": iso_utc(clock),
        "deleted_at": deleted_at,
    }


def memo_row(
    memo_id: str,
    project_id: str,
    *,
    kind: str = "memo",
    content: str = "body",
    creator: str = ACCOUNT,
    clock: datetime = BASE,
    deleted_at: str | None = None,
) -> dict:
    return {
        "id": memo_id,
        "project_id": project_id,
        "created_by": creator,
        "kind": kind,
        "title": "백로그" if kind == "backlog" else memo_id,
        "content": content,
        "status": 0,
        "created_at": BASE.isoformat(timespec="milliseconds"),
        "updated_at": clock.isoformat(timespec="milliseconds"),
        "updated_at_utc": iso_utc(clock),
        "deleted_at": deleted_at,
    }


def change(table: str, row_id: str, row: dict, seq: int = 1) -> dict:
    return {"table": table, "rowId": row_id, "op": "update", "seq": seq, "row": row}


@unittest.skipUnless(REACHABLE, f"MySQL scratch DB unavailable ({DB_CONFIG['database']})")
class HierarchySyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_database = DB_CONFIG["database"]
        self._previous_env_database = os.environ.get("DB_NAME")
        use_scratch_database("todo_sync_hierarchy_test")
        truncate_scratch_tables()

    def tearDown(self) -> None:
        DB_CONFIG["database"] = self._previous_database
        if self._previous_env_database is None:
            os.environ.pop("DB_NAME", None)
        else:
            os.environ["DB_NAME"] = self._previous_env_database

    def insert_project(
        self,
        project_id: str,
        *,
        owner: str = ACCOUNT,
        parent_id: str | None = None,
        is_group: bool = False,
        deleted_at=None,
    ) -> None:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, owner_id, parent_id, is_group, name, icon, status,
                         is_secret, created_at, updated_at, updated_at_utc, deleted_at)
                    VALUES (%s, %s, %s, %s, %s, 'Beer', 0, 0, %s, %s, %s, %s)
                    """,
                    (
                        project_id, owner, parent_id, is_group, project_id,
                        BASE, BASE, BASE, deleted_at,
                    ),
                )

    def insert_member(self, member_id: str, project_id: str, user_id: str, role: str, deleted_at=None) -> None:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO project_members
                        (id, project_id, user_id, role, invited_at, updated_at_utc, deleted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (member_id, project_id, user_id, role, BASE, BASE, deleted_at),
                )

    def insert_memo(
        self,
        memo_id: str,
        project_id: str,
        *,
        kind: str = "memo",
        content: str = "body",
        clock: datetime = BASE,
        deleted_at=None,
    ) -> None:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, kind, title, content, status,
                         created_at, updated_at, updated_at_utc, deleted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s)
                    """,
                    (
                        memo_id, project_id, ACCOUNT, kind, memo_id, content,
                        BASE, clock, clock, deleted_at,
                    ),
                )

    def test_legitimate_first_child_batch_applies_child_then_irreversible_group(self) -> None:
        self.insert_project("root")
        self.insert_memo("old", "root", content="preserved")
        batch = [
            change("projects", "root", project_row("root", is_group=True, clock=BASE + timedelta(seconds=1)), 2),
            change("projects", "child", project_row("child", parent_id="root", clock=BASE + timedelta(seconds=1)), 1),
        ]
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                _authorize_sync_changes(cursor, batch, ACCOUNT)
                outcome = apply_changes(cursor, batch, SIDE_CLIENT)
        self.assertEqual(outcome.applied, 2)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT is_group FROM projects WHERE id = 'root'")
                self.assertEqual(cursor.fetchone()["is_group"], 1)
                cursor.execute("SELECT project_id, content FROM memos WHERE id = 'old'")
                self.assertEqual(cursor.fetchone(), {"project_id": "child", "content": "preserved"})

    def test_lone_group_toggle_is_forbidden(self) -> None:
        self.insert_project("root")
        payload = [change("projects", "root", project_row("root", is_group=True, clock=BASE + timedelta(seconds=1)))]
        with get_db_connection() as conn:
            with conn.cursor() as cursor, self.assertRaises(HTTPException):
                _authorize_sync_changes(cursor, payload, ACCOUNT)

    def test_lone_new_group_is_forbidden(self) -> None:
        payload = [change("projects", "new-root", project_row("new-root", is_group=True))]
        with get_db_connection() as conn:
            with conn.cursor() as cursor, self.assertRaises(HTTPException):
                _authorize_sync_changes(cursor, payload, ACCOUNT)

    def test_lone_child_under_leaf_is_forbidden(self) -> None:
        self.insert_project("root")
        payload = [change(
            "projects", "child", project_row("child", parent_id="root")
        )]
        with get_db_connection() as conn:
            with conn.cursor() as cursor, self.assertRaises(HTTPException):
                _authorize_sync_changes(cursor, payload, ACCOUNT)

    def test_tombstoned_child_does_not_authorize_group_transition(self) -> None:
        tombstone = iso_utc(BASE)
        self.insert_project("existing")
        for root_id, root_exists in (("new", False), ("existing", True)):
            payload = [
                change("projects", root_id, project_row(root_id, is_group=True)),
                change(
                    "projects",
                    f"{root_id}-dead-child",
                    project_row(
                        f"{root_id}-dead-child",
                        parent_id=root_id,
                        deleted_at=tombstone,
                    ),
                    2,
                ),
            ]
            with self.subTest(root=root_id, exists=root_exists):
                with get_db_connection() as conn:
                    with conn.cursor() as cursor, self.assertRaises(HTTPException):
                        _authorize_sync_changes(cursor, payload, ACCOUNT)

    def test_editor_cannot_move_memo_across_roots_or_delete_it(self) -> None:
        for root in ("a", "b"):
            self.insert_project(root, owner=OTHER)
            self.insert_member(f"pm-{root}", root, ACCOUNT, "editor")
        self.insert_memo("m", "a")
        moved = change("memos", "m", memo_row("m", "b", clock=BASE + timedelta(seconds=1)))
        deleted = change(
            "memos", "m",
            memo_row("m", "a", clock=BASE + timedelta(seconds=1), deleted_at=iso_utc(BASE + timedelta(seconds=1))),
        )
        hard_deleted = {"table": "memos", "rowId": "m", "op": "delete", "row": None}
        for payload in ([moved], [deleted], [hard_deleted]):
            with get_db_connection() as conn:
                with conn.cursor() as cursor, self.assertRaises(HTTPException):
                    _authorize_sync_changes(cursor, payload, ACCOUNT)

    def test_only_root_creator_can_move_memo_between_siblings(self) -> None:
        self.insert_project("root", is_group=True)
        self.insert_project("left", parent_id="root")
        self.insert_project("right", parent_id="root")
        self.insert_memo("m", "left")
        moved = change(
            "memos", "m", memo_row("m", "right", clock=BASE + timedelta(seconds=1))
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                _authorize_sync_changes(cursor, [moved], ACCOUNT)

        # Same siblings, but an editor of both children is not the root creator.
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE projects SET owner_id=%s", (OTHER,))
        self.insert_member("pm-left", "left", ACCOUNT, "editor")
        self.insert_member("pm-right", "right", ACCOUNT, "editor")
        with get_db_connection() as conn:
            with conn.cursor() as cursor, self.assertRaises(HTTPException):
                _authorize_sync_changes(cursor, [moved], ACCOUNT)

    def test_actual_owner_can_move_regular_memo_across_owned_roots(self) -> None:
        self.insert_project("source")
        self.insert_project("target")
        self.insert_memo("m", "source")
        moved = change(
            "memos", "m",
            memo_row("m", "target", clock=BASE + timedelta(seconds=1)),
        )
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                _authorize_sync_changes(cursor, [moved], ACCOUNT)
                outcome = apply_changes(cursor, [moved], SIDE_CLIENT)
        self.assertEqual(outcome.applied, 1)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT project_id FROM memos WHERE id='m'")
                self.assertEqual(cursor.fetchone()["project_id"], "target")

    def test_cross_root_backlog_move_is_rejected_by_auth_and_apply(self) -> None:
        self.insert_project("source")
        self.insert_project("target")
        self.insert_memo("backlog", "source", kind="backlog")
        moved = change(
            "memos", "backlog",
            memo_row(
                "backlog", "target", kind="backlog",
                clock=BASE + timedelta(seconds=1),
            ),
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor, self.assertRaises(HTTPException):
                _authorize_sync_changes(cursor, [moved], ACCOUNT)
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                outcome = apply_changes(cursor, [moved], SIDE_CLIENT)
        self.assertEqual(
            outcome.rejected[0]["reason"], "backlog_manual_move_forbidden"
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT project_id FROM memos WHERE id='backlog'")
                self.assertEqual(cursor.fetchone()["project_id"], "source")

    def test_first_child_batch_moves_backlog_with_group_transition(self) -> None:
        self.insert_project("root")
        self.insert_memo("backlog", "root", kind="backlog", content="preserved")
        clock = BASE + timedelta(seconds=1)
        batch = [
            change("projects", "root", project_row("root", is_group=True, clock=clock), 1),
            change("projects", "child", project_row("child", parent_id="root", clock=clock), 2),
            change(
                "memos", "backlog",
                memo_row("backlog", "child", kind="backlog", content="preserved", clock=clock),
                3,
            ),
        ]
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                _authorize_sync_changes(cursor, batch, ACCOUNT)
                outcome = apply_changes(cursor, batch, SIDE_CLIENT)
        self.assertFalse(outcome.rejected)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT project_id,content,kind FROM memos WHERE id='backlog'")
                self.assertEqual(
                    cursor.fetchone(),
                    {"project_id": "child", "content": "preserved", "kind": "backlog"},
                )

    def test_memo_kind_is_valid_and_immutable_through_raw_push(self) -> None:
        self.insert_project("leaf")
        self.insert_memo("m", "leaf")
        for kind in ("backlog", "evil"):
            payload = [change(
                "memos", "m",
                memo_row("m", "leaf", kind=kind, clock=BASE + timedelta(seconds=1)),
            )]
            with get_db_connection() as conn:
                with conn.cursor() as cursor, self.assertRaises(HTTPException):
                    _authorize_sync_changes(cursor, payload, ACCOUNT)

    def test_root_only_editor_cannot_write_through_group_into_unshared_child(self) -> None:
        self.insert_project("root", owner=OTHER, is_group=True)
        self.insert_project("child", owner=OTHER, parent_id="root")
        self.insert_member("pm-root", "root", ACCOUNT, "editor")
        payload = [change("memos", "new", memo_row("new", "root"))]
        with get_db_connection() as conn:
            with conn.cursor() as cursor, self.assertRaises(HTTPException):
                _authorize_sync_changes(cursor, payload, ACCOUNT)

    def test_deleted_project_cannot_be_resurrected_by_later_offline_edit(self) -> None:
        tombstone = BASE + timedelta(seconds=10)
        self.insert_project("root", deleted_at=tombstone)
        incoming = change(
            "projects", "root",
            project_row("root", clock=BASE + timedelta(seconds=20), deleted_at=None),
        )
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                outcome = apply_changes(cursor, [incoming], SIDE_SERVER)
        self.assertEqual(outcome.rejected[0]["reason"], "project_deletion_is_irreversible")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT deleted_at FROM projects WHERE id = 'root'")
                self.assertIsNotNone(cursor.fetchone()["deleted_at"])

    def test_distinct_backlog_ids_converge_and_preserve_content_and_versions(self) -> None:
        self.insert_project("leaf")
        self.insert_memo("z-backlog", "leaf", kind="backlog", content="old", clock=BASE)
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memo_versions
                       (id, memo_id, content, version, note, created_at, updated_at_utc)
                       VALUES ('vz', 'z-backlog', 'older', 1, NULL, %s, %s)""",
                    (BASE, BASE),
                )
                outcome = apply_changes(
                    cursor,
                    [change("memos", "a-backlog", memo_row(
                        "a-backlog", "leaf", kind="backlog", content="new",
                        clock=BASE + timedelta(seconds=1),
                    ))],
                    SIDE_SERVER,
                )
        self.assertEqual(outcome.duplicates[0]["resolution"], "backlog_converged")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, content FROM memos WHERE project_id='leaf' AND kind='backlog' AND deleted_at IS NULL")
                self.assertEqual(cursor.fetchall(), [{"id": "a-backlog", "content": "new"}])
                cursor.execute("SELECT content FROM memo_versions WHERE memo_id='a-backlog' ORDER BY version")
                self.assertEqual({row["content"] for row in cursor.fetchall()}, {"old", "older"})

    def test_revoked_historical_member_receives_only_own_membership_tombstone(self) -> None:
        self.insert_project("p", owner=OTHER)
        self.insert_member("pm-self", "p", ACCOUNT, "viewer", deleted_at=BASE)
        self.insert_member("pm-other", "p", "new-user", "editor")
        self.insert_memo("secret-new", "p", content="not visible")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memo_versions
                       (id,memo_id,content,version,note,created_at,updated_at_utc)
                       VALUES ('secret-version','secret-new','new secret',1,NULL,%s,%s)""",
                    (BASE, BASE),
                )
                # Seed explicit outbox entries because setup uses sync_applying=True.
                cursor.execute(
                    """INSERT INTO change_log (table_name,row_id,op,changed_at_utc)
                       VALUES ('projects','p','update',%s),
                              ('project_members','pm-self','update',%s),
                              ('project_members','pm-other','insert',%s),
                              ('memos','secret-new','insert',%s),
                              ('memo_versions','secret-version','insert',%s)""",
                    (BASE, BASE, BASE, BASE, BASE),
                )
                changes, _ = read_changes(cursor, 0, 100, ACCOUNT)
        self.assertEqual(
            {(entry["table"], entry["rowId"]) for entry in changes},
            {("project_members", "pm-self")},
        )

    def test_child_change_feed_includes_navigation_parent_with_original_unique_seq(self) -> None:
        self.insert_project("root", owner=OTHER, is_group=True)
        self.insert_project("child", owner=OTHER, parent_id="root")
        self.insert_member("pm-child", "child", ACCOUNT, "viewer")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO change_log (seq,table_name,row_id,op,changed_at_utc)
                       VALUES (10,'projects','root','insert',%s),
                              (20,'projects','child','update',%s)""",
                    (BASE, BASE),
                )
                changes, next_seq = read_changes(cursor, 15, 100, ACCOUNT)
        project_changes = [entry for entry in changes if entry["table"] == "projects"]
        self.assertEqual(
            {(entry["rowId"], entry["seq"]) for entry in project_changes},
            {("root", 10), ("child", 20)},
        )
        self.assertEqual(next_seq, 20)

    def test_delegated_lock_requires_live_leaf_memo_and_live_ancestor(self) -> None:
        self.insert_project("root", is_group=True)
        self.insert_project("leaf", parent_id="root")
        self.insert_memo("live", "leaf")
        self.insert_memo("group-memo", "root")
        self.insert_memo("deleted", "leaf", deleted_at=BASE)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                _require_memo_write_access(cursor, "live", ACCOUNT)
                for memo_id in ("group-memo", "deleted"):
                    with self.assertRaises(HTTPException):
                        _require_memo_write_access(cursor, memo_id, ACCOUNT)
                cursor.execute("UPDATE projects SET deleted_at=%s WHERE id='root'", (BASE,))
                with self.assertRaises(HTTPException):
                    _require_memo_write_access(cursor, "live", ACCOUNT)

    def test_legacy_merge_rejects_backlog_and_incompatible_project_hierarchy(self) -> None:
        self.insert_project("root", is_group=True)
        self.insert_project("child-a", parent_id="root")
        self.insert_project("other")
        self.insert_memo("backlog", "child-a", kind="backlog")
        self.insert_memo("memo", "child-a")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                with self.assertRaises(MergeError):
                    merge_memos(cursor, "backlog", "memo")
                with self.assertRaises(MergeError):
                    merge_projects(cursor, "child-a", "other")
                with self.assertRaises(MergeError):
                    merge_projects(cursor, "root", "other")

    def test_merge_rejects_memos_hidden_by_deleted_ancestor(self) -> None:
        self.insert_project("root", is_group=True)
        self.insert_project("child", parent_id="root")
        self.insert_memo("a", "child")
        self.insert_memo("b", "child")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE projects SET deleted_at=%s WHERE id='root'", (BASE,))
                with self.assertRaises(MergeError):
                    merge_memos(cursor, "a", "b")


class _RealtimeProbe:
    available = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def revoke_project_access(self, project_ids):
        self.calls.append(("revoke", tuple(project_ids)))

    async def revalidate_memo_access(self, memo_id):
        self.calls.append(("revalidate", memo_id))

    async def emit_memo_pulled(self, memo_id, *_args):
        self.calls.append(("emit", memo_id))

    async def emit_sync_applied(self, _payload):
        self.calls.append(("sync",))

    async def emit_projects_changed(self):
        self.calls.append(("projects",))


class RealtimeSyncOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pull_revalidates_structural_access_before_live_content_emit(self) -> None:
        probe = _RealtimeProbe()
        daemon = SyncDaemon(realtime_server=probe)
        await daemon._rebroadcast({
            "pull": {
                "appliedRows": [
                    {"table": "project_members", "rowId": "pm", "row": {"project_id": "p", "deleted_at": iso_utc(BASE)}},
                    {"table": "memos", "rowId": "m-deleted", "row": {"project_id": "p", "content": "secret", "deleted_at": iso_utc(BASE)}},
                    {"table": "memos", "rowId": "m-live", "row": {"project_id": "p", "content": "ok", "deleted_at": None}},
                ]
            }
        })
        self.assertEqual(probe.calls[:3], [
            ("revoke", ("p",)),
            ("revalidate", "m-deleted"),
            ("revalidate", "m-live"),
        ])
        self.assertNotIn(("emit", "m-deleted"), probe.calls)
        self.assertIn(("emit", "m-live"), probe.calls)

    async def test_push_revalidates_rooms_for_membership_and_memo_changes(self) -> None:
        probe = _RealtimeProbe()
        from unittest.mock import patch

        payload = [
            {"table": "project_members", "rowId": "pm", "row": {"project_id": "p"}},
            {"table": "memos", "rowId": "m", "row": {"project_id": "p"}},
        ]
        with patch("src.routers.sync.get_realtime_server", return_value=probe):
            await _revalidate_after_sync_push(
                [{"table": "project_members", "rowId": "pm"}, {"table": "memos", "rowId": "m"}],
                payload,
            )
        self.assertEqual(
            probe.calls,
            [("revoke", ("p",)), ("revalidate", "m"), ("projects",)],
        )


if __name__ == "__main__":
    unittest.main()
