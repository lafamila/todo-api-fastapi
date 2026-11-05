"""Sync v4 private-backlog and attachment metadata security tests."""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta

from fastapi import HTTPException

from tests.scratch_db import (
    assert_scratch_database,
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

SCRATCH = use_scratch_database("todo_sync_media_sync_test")

from src.connectors import DB_CONFIG, get_db_connection, init_db  # noqa: E402
from src.routers.sync import (  # noqa: E402
    SyncChangeInput,
    SyncPushRequest,
    _authorize_sync_changes,
    _issue_visible_to_user,
    _resolve_push_columns,
)
from src.services.sync_apply import SIDE_SERVER, apply_changes  # noqa: E402
from src.services.sync_daemon import SyncBlocked, SyncDaemon  # noqa: E402
from src.services.merge import merge_memos, merge_projects  # noqa: E402
from src.services.sync_store import collect_local_changes, read_changes, record_issue  # noqa: E402
from src.sync_schema import SCHEMA_VERSION, SYNC_TABLE_ORDER, declared_tables  # noqa: E402
from src.timeutil import iso_utc  # noqa: E402


BASE = datetime(2026, 11, 5, 0, 0, 0)
A = "account-a"
B = "account-b"
OWNER = "project-owner"
VIEWER = "account-viewer"
ATTACH_A = "11111111-1111-4111-8111-111111111111"
ATTACH_B = "22222222-2222-4222-8222-222222222222"
SHA_A = "a" * 64
SHA_B = "b" * 64
REACHABLE = init_scratch_database()


@contextmanager
def selected_database(name: str):
    assert_scratch_database(name)
    previous = DB_CONFIG["database"]
    previous_env = os.environ.get("DB_NAME")
    DB_CONFIG["database"] = name
    os.environ["DB_NAME"] = name
    try:
        yield
    finally:
        DB_CONFIG["database"] = previous
        if previous_env is None:
            os.environ.pop("DB_NAME", None)
        else:
            os.environ["DB_NAME"] = previous_env


def change(table: str, row_id: str, row: dict, seq: int = 1) -> dict:
    return {"table": table, "rowId": row_id, "op": "update", "seq": seq, "row": row}


def attachment_row(
    attachment_id: str,
    memo_id: str,
    creator: str,
    *,
    sha256: str = SHA_A,
    status: str = "ready",
    object_key: str | None = None,
) -> dict:
    return {
        "id": attachment_id,
        "memo_id": memo_id,
        "origin_memo_id": memo_id,
        "created_by": creator,
        "file_name": "image.png",
        "content_type": "image/png",
        "byte_size": 12,
        "is_image": True,
        "sha256": sha256,
        "object_key": object_key or f"todo/memos/{memo_id}/{attachment_id}",
        "status": status,
        "created_at": BASE.isoformat(timespec="milliseconds"),
        "updated_at_utc": iso_utc(BASE),
        "deleted_at": None,
    }


@unittest.skipUnless(REACHABLE, f"MySQL scratch DB unavailable ({DB_CONFIG['database']})")
class PrivateMediaSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_db = DB_CONFIG["database"]
        self.previous_env = os.environ.get("DB_NAME")
        use_scratch_database(SCRATCH)
        truncate_scratch_tables()
        self._seed_project()

    def tearDown(self) -> None:
        DB_CONFIG["database"] = self.previous_db
        if self.previous_env is None:
            os.environ.pop("DB_NAME", None)
        else:
            os.environ["DB_NAME"] = self.previous_env

    def _seed_project(self) -> None:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO projects
                       (id,owner_id,parent_id,is_group,name,icon,status,is_secret,
                        created_at,updated_at,updated_at_utc)
                       VALUES ('p',%s,NULL,0,'P','Beer',0,0,%s,%s,%s)""",
                    (OWNER, BASE, BASE, BASE),
                )
                for user, role in (
                    (A, "editor"),
                    (B, "editor"),
                    (VIEWER, "viewer"),
                    (OWNER, "owner"),
                ):
                    cursor.execute(
                        """INSERT INTO project_members
                           (id,project_id,user_id,role,invited_at,updated_at_utc)
                           VALUES (%s,'p',%s,%s,%s,%s)""",
                        (f"pm-{user}", user, role, BASE, BASE),
                    )

    def _insert_memo(self, memo_id: str, creator: str, kind: str = "backlog") -> None:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memos
                       (id,project_id,created_by,kind,title,content,status,
                        created_at,updated_at,updated_at_utc)
                       VALUES (%s,'p',%s,%s,%s,%s,0,%s,%s,%s)""",
                    (memo_id, creator, kind, memo_id, f"secret-{creator}", BASE, BASE, BASE),
                )

    def _insert_attachment(self, row: dict) -> None:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memo_attachments
                       (id,memo_id,origin_memo_id,created_by,file_name,content_type,byte_size,is_image,
                        sha256,object_key,status,created_at,updated_at_utc,deleted_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    tuple(
                        BASE if column in {"created_at", "updated_at_utc"}
                        else row[column]
                        for column in declared_tables()["memo_attachments"]
                    ),
                )

    def test_feed_hides_other_accounts_backlog_version_and_attachment_even_from_project_owner(self) -> None:
        self._insert_memo("backlog-a", A)
        self._insert_memo("backlog-b", B)
        row_a = attachment_row(ATTACH_A, "backlog-a", A)
        row_b = attachment_row(ATTACH_B, "backlog-b", B, sha256=SHA_B)
        self._insert_attachment(row_a)
        self._insert_attachment(row_b)
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                for suffix, memo_id in (("a", "backlog-a"), ("b", "backlog-b")):
                    cursor.execute(
                        """INSERT INTO memo_versions
                           (id,memo_id,content,version,note,created_at,updated_at_utc)
                           VALUES (%s,%s,%s,1,NULL,%s,%s)""",
                        (f"v-{suffix}", memo_id, f"version-{suffix}", BASE, BASE),
                    )
                rows = [
                    ("memos", "backlog-a"), ("memos", "backlog-b"),
                    ("memo_versions", "v-a"), ("memo_versions", "v-b"),
                    ("memo_attachments", ATTACH_A), ("memo_attachments", ATTACH_B),
                ]
                for table, row_id in rows:
                    cursor.execute(
                        "INSERT INTO change_log(table_name,row_id,op,changed_at_utc) VALUES(%s,%s,'update',%s)",
                        (table, row_id, BASE),
                    )

        for account, own_suffix in ((A, "a"), (B, "b")):
            with self.subTest(account=account):
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        changes, _ = read_changes(cursor, 0, 100, account)
                refs = {(item["table"], item["rowId"]) for item in changes}
                self.assertIn(("memos", f"backlog-{own_suffix}"), refs)
                self.assertIn(("memo_versions", f"v-{own_suffix}"), refs)
                self.assertIn(
                    ("memo_attachments", ATTACH_A if account == A else ATTACH_B), refs
                )
                self.assertNotIn(("memos", "backlog-b" if account == A else "backlog-a"), refs)

        # Project owner is not a private-backlog owner and receives neither account's data.
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                owner_changes, _ = read_changes(cursor, 0, 100, OWNER)
        self.assertFalse(
            {item["table"] for item in owner_changes}
            & {"memos", "memo_versions", "memo_attachments"}
        )

    def test_raw_push_cannot_forge_private_backlog_version_or_attachment(self) -> None:
        self._insert_memo("backlog-b", B)
        self._insert_attachment(attachment_row(ATTACH_B, "backlog-b", B, sha256=SHA_B))
        attempts = [
            change("memos", "backlog-b", {
                "id": "backlog-b", "project_id": "p", "created_by": B,
                "kind": "backlog", "title": "backlog-b", "content": "stolen",
                "status": 0, "created_at": BASE.isoformat(timespec="milliseconds"),
                "updated_at": BASE.isoformat(timespec="milliseconds"),
                "updated_at_utc": iso_utc(BASE + timedelta(seconds=1)), "deleted_at": None,
            }),
            change("memo_versions", "v-forged", {
                "id": "v-forged", "memo_id": "backlog-b", "content": "stolen",
                "version": 1, "note": None,
                "created_at": BASE.isoformat(timespec="milliseconds"),
                "updated_at_utc": iso_utc(BASE),
            }),
            change("memo_attachments", ATTACH_B, attachment_row(
                ATTACH_B, "backlog-b", B, sha256=SHA_B
            )),
        ]
        for attempt in attempts:
            with self.subTest(table=attempt["table"]):
                with get_db_connection() as conn:
                    with conn.cursor() as cursor, self.assertRaises(HTTPException):
                        _authorize_sync_changes(cursor, [attempt], A)

    def test_existing_private_version_partial_update_and_delete_require_exact_creator(self) -> None:
        self._insert_memo("backlog-a", A)
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO memo_versions
                       (id,memo_id,content,version,note,created_at,updated_at_utc)
                       VALUES ('private-version','backlog-a','private',1,NULL,%s,%s)""",
                    (BASE, BASE),
                )
        partial = change(
            "memo_versions",
            "private-version",
            {"id": "private-version", "updated_at_utc": iso_utc(BASE + timedelta(seconds=1))},
        )
        hard_delete = {
            "table": "memo_versions", "rowId": "private-version",
            "op": "delete", "seq": 2, "row": None,
        }
        for payload in ([partial], [hard_delete]):
            with self.subTest(op=payload[0]["op"]):
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        _authorize_sync_changes(cursor, payload, A)
                with get_db_connection() as conn:
                    with conn.cursor() as cursor, self.assertRaises(HTTPException):
                        _authorize_sync_changes(cursor, payload, OWNER)

    def test_viewer_can_push_own_private_backlog_and_version(self) -> None:
        self._insert_memo("backlog-viewer", VIEWER)
        memo_update = change("memos", "backlog-viewer", {
            "id": "backlog-viewer", "project_id": "p", "created_by": VIEWER,
            "kind": "backlog", "title": "백로그", "content": "viewer edit",
            "status": 0, "created_at": BASE.isoformat(timespec="milliseconds"),
            "updated_at": BASE.isoformat(timespec="milliseconds"),
            "updated_at_utc": iso_utc(BASE + timedelta(seconds=1)), "deleted_at": None,
        })
        version = change("memo_versions", "viewer-version", {
            "id": "viewer-version", "memo_id": "backlog-viewer",
            "content": "old", "version": 1, "note": None,
            "created_at": BASE.isoformat(timespec="milliseconds"),
            "updated_at_utc": iso_utc(BASE),
        })
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                _authorize_sync_changes(cursor, [memo_update, version], VIEWER)

    def test_attachment_metadata_is_canonical_immutable_and_status_validated(self) -> None:
        self._insert_memo("memo-a", A, kind="memo")
        self._insert_memo("memo-other", B, kind="memo")
        good = attachment_row(ATTACH_A, "memo-a", A)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                _authorize_sync_changes(
                    cursor, [change("memo_attachments", ATTACH_A, good)], A
                )
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                outcome = apply_changes(
                    cursor, [change("memo_attachments", ATTACH_A, good)], SIDE_SERVER
                )
        self.assertEqual(outcome.applied, 1)

        forged = [
            {**good, "object_key": f"todo/memos/other/{ATTACH_A}"},
            {**good, "created_by": B},
            {**good, "byte_size": 999},
            {**good, "status": "evil"},
            {**good, "file_name": "../image.png"},
            {**good, "file_name": "bad\x7fname.png"},
            {**good, "file_name": "photo\u202egnp.exe"},
            {**good, "file_name": "가" * 86},
            {**good, "content_type": "image/svg+xml", "is_image": True},
            {**good, "content_type": "image/png", "is_image": False},
            {**good, "content_type": "text/plain", "is_image": True},
            {**good, "content_type": "not a mime", "is_image": False},
            {**good, "memo_id": "memo-other"},
        ]
        for row in forged:
            with self.subTest(forgery=row):
                with get_db_connection() as conn:
                    with conn.cursor() as cursor, self.assertRaises(HTTPException):
                        _authorize_sync_changes(
                            cursor, [change("memo_attachments", ATTACH_A, row)], A
                        )

    def test_collect_local_changes_never_queues_other_private_or_attachment_creator(self) -> None:
        self._insert_memo("backlog-a", A)
        self._insert_memo("backlog-b", B)
        self._insert_memo("shared", A, kind="memo")
        self._insert_attachment(attachment_row(ATTACH_A, "shared", A))
        self._insert_attachment(attachment_row(ATTACH_B, "shared", B, sha256=SHA_B))
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                for table, row_id in (
                    ("memos", "backlog-a"), ("memos", "backlog-b"),
                    ("memo_attachments", ATTACH_A), ("memo_attachments", ATTACH_B),
                ):
                    cursor.execute(
                        "INSERT INTO change_log(table_name,row_id,op,changed_at_utc) VALUES(%s,%s,'update',%s)",
                        (table, row_id, BASE),
                    )
                changes, _ = collect_local_changes(cursor, 0, 100, account_id=A)
        refs = {(item["table"], item["rowId"]) for item in changes}
        self.assertIn(("memos", "backlog-a"), refs)
        self.assertIn(("memo_attachments", ATTACH_A), refs)
        self.assertNotIn(("memos", "backlog-b"), refs)
        self.assertNotIn(("memo_attachments", ATTACH_B), refs)

    def test_private_issue_is_visible_only_to_exact_backlog_owner(self) -> None:
        self._insert_memo("backlog-a", A)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                issue_id = record_issue(
                    cursor, "conflict", ref_table="memos", ref_id="backlog-a",
                    detail={"private": "do-not-leak"},
                )
                issue = {
                    "id": issue_id, "kind": "conflict", "ref_table": "memos",
                    "ref_id": "backlog-a", "peer_ref_id": None,
                }
                self.assertTrue(_issue_visible_to_user(
                    cursor, issue, {"id": A, "permission": "user"}
                ))
                self.assertFalse(_issue_visible_to_user(
                    cursor, issue, {"id": OWNER, "permission": "owner"}
                ))
                self.assertFalse(_issue_visible_to_user(
                    cursor, issue, {"id": B, "permission": "user"}
                ))

    def test_backlog_convergence_is_per_creator_and_preserves_attachment_origin(self) -> None:
        self._insert_memo("z-a", A)
        self._insert_memo("z-b", B)
        attached = attachment_row(ATTACH_A, "z-a", A)
        self._insert_attachment(attached)
        incoming = {
            "id": "a-a", "project_id": "p", "created_by": A,
            "kind": "backlog", "title": "백로그", "content": "new-a",
            "status": 0, "created_at": BASE.isoformat(timespec="milliseconds"),
            "updated_at": (BASE + timedelta(seconds=1)).isoformat(timespec="milliseconds"),
            "updated_at_utc": iso_utc(BASE + timedelta(seconds=1)), "deleted_at": None,
        }
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                outcome = apply_changes(
                    cursor, [change("memos", "a-a", incoming)], SIDE_SERVER
                )
        self.assertEqual(outcome.duplicates[0]["createdBy"], A)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id,created_by FROM memos WHERE project_id='p' AND kind='backlog' AND deleted_at IS NULL"
                )
                live = cursor.fetchall()
                self.assertEqual(sum(row["created_by"] == A for row in live), 1)
                self.assertEqual(sum(row["created_by"] == B for row in live), 1)
                winner_a = next(row["id"] for row in live if row["created_by"] == A)
                cursor.execute(
                    "SELECT memo_id,origin_memo_id,object_key FROM memo_attachments WHERE id=%s",
                    (ATTACH_A,),
                )
                metadata = cursor.fetchone()
        self.assertEqual(metadata["memo_id"], winner_a)
        self.assertEqual(metadata["origin_memo_id"], "z-a")
        self.assertEqual(metadata["object_key"], attached["object_key"])

    def test_owner_merge_reparents_other_editor_attachment_and_peer_accepts_it(self) -> None:
        self._insert_memo("shared-source", B, kind="memo")
        self._insert_memo("shared-winner", OWNER, kind="memo")
        attached = attachment_row(ATTACH_B, "shared-source", B, sha256=SHA_B)
        self._insert_attachment(attached)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                merge_memos(cursor, "shared-source", "shared-winner")
                cursor.execute("SELECT * FROM memo_attachments WHERE id=%s", (ATTACH_B,))
                wire_row = cursor.fetchone()
        self.assertEqual(wire_row["memo_id"], "shared-winner")
        self.assertEqual(wire_row["origin_memo_id"], "shared-source")

        # Model a peer before merge, then apply the owner-authorized association change.
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE memos SET deleted_at=NULL WHERE id='shared-source'")
                cursor.execute(
                    "UPDATE memo_attachments SET memo_id='shared-source' WHERE id=%s",
                    (ATTACH_B,),
                )
        serialized = {
            key: (
                iso_utc(value)
                if key in {"updated_at_utc", "deleted_at"} and value
                else value.isoformat(timespec="milliseconds")
                if key == "created_at" and value
                else bool(value)
                if key == "is_image"
                else value
            )
            for key, value in wire_row.items()
            if key in declared_tables()["memo_attachments"]
        }
        payload = [change("memo_attachments", ATTACH_B, serialized)]
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                _authorize_sync_changes(cursor, payload, OWNER)
                outcome = apply_changes(cursor, payload, SIDE_SERVER)
        self.assertEqual(outcome.applied, 1)

    def test_project_merge_converges_private_backlogs_per_creator_with_files(self) -> None:
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO projects
                       (id,owner_id,parent_id,is_group,name,icon,status,is_secret,
                        created_at,updated_at,updated_at_utc)
                       VALUES ('q',%s,NULL,0,'Q','Beer',0,0,%s,%s,%s)""",
                    (OWNER, BASE, BASE, BASE),
                )
        for creator in (A, B):
            self._insert_memo(f"p-{creator}", creator)
            with get_db_connection(sync_applying=True) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO memos
                           (id,project_id,created_by,kind,title,content,status,
                            created_at,updated_at,updated_at_utc)
                           VALUES (%s,'q',%s,'backlog','백로그',%s,0,%s,%s,%s)""",
                        (f"q-{creator}", creator, f"q-{creator}", BASE, BASE, BASE),
                    )
        attached = attachment_row(ATTACH_A, f"q-{A}", A)
        self._insert_attachment(attached)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                merge_projects(cursor, "q", "p")
                cursor.execute(
                    "SELECT created_by,COUNT(*) AS total FROM memos WHERE project_id='p' AND kind='backlog' AND deleted_at IS NULL GROUP BY created_by"
                )
                counts = {row["created_by"]: row["total"] for row in cursor.fetchall()}
                cursor.execute(
                    "SELECT memo_id,origin_memo_id,object_key FROM memo_attachments WHERE id=%s",
                    (ATTACH_A,),
                )
                metadata = cursor.fetchone()
        self.assertEqual(counts, {A: 1, B: 1})
        self.assertEqual(metadata["origin_memo_id"], f"q-{A}")
        self.assertEqual(metadata["object_key"], attached["object_key"])


@unittest.skipUnless(REACHABLE, "MySQL scratch DB unavailable")
class PrivateMediaTwoNodeTests(unittest.TestCase):
    NODE_A = assert_scratch_database("todo_sync_media_node_a")
    NODE_B = assert_scratch_database("todo_sync_media_node_b")

    @classmethod
    def setUpClass(cls) -> None:
        for database in (cls.NODE_A, cls.NODE_B):
            with selected_database(database):
                init_db()

    def setUp(self) -> None:
        for database in (self.NODE_A, self.NODE_B):
            with selected_database(database):
                truncate_scratch_tables()
                with get_db_connection(sync_applying=True) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            """INSERT INTO projects
                               (id,owner_id,parent_id,is_group,name,icon,status,is_secret,
                                created_at,updated_at,updated_at_utc)
                               VALUES ('p',%s,NULL,0,'P','Beer',0,0,%s,%s,%s)""",
                            (OWNER, BASE, BASE, BASE),
                        )
                        for user, role in ((OWNER, "owner"), (B, "editor")):
                            cursor.execute(
                                """INSERT INTO project_members
                                   (id,project_id,user_id,role,invited_at,updated_at_utc)
                                   VALUES (%s,'p',%s,%s,%s,%s)""",
                                (f"pm-{user}", user, role, BASE, BASE),
                            )
                        for memo_id, creator in (
                            ("shared-source", B), ("shared-winner", OWNER)
                        ):
                            cursor.execute(
                                """INSERT INTO memos
                                   (id,project_id,created_by,kind,title,content,status,
                                    created_at,updated_at,updated_at_utc)
                                   VALUES (%s,'p',%s,'memo',%s,%s,0,%s,%s,%s)""",
                                (memo_id, creator, memo_id, memo_id, BASE, BASE, BASE),
                            )
                        attachment = attachment_row(
                            ATTACH_B, "shared-source", B, sha256=SHA_B
                        )
                        cursor.execute(
                            """INSERT INTO memo_attachments
                               (id,memo_id,origin_memo_id,created_by,file_name,content_type,
                                byte_size,is_image,sha256,object_key,status,created_at,
                                updated_at_utc,deleted_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (
                                attachment["id"], attachment["memo_id"],
                                attachment["origin_memo_id"], attachment["created_by"],
                                attachment["file_name"], attachment["content_type"],
                                attachment["byte_size"], attachment["is_image"],
                                attachment["sha256"], attachment["object_key"],
                                attachment["status"], BASE, BASE, None,
                            ),
                        )

    def test_editor_attachment_survives_owner_merge_across_two_nodes(self) -> None:
        with selected_database(self.NODE_A):
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    merge_memos(cursor, "shared-source", "shared-winner")
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    changes, _ = collect_local_changes(
                        cursor, 0, 100, account_id=OWNER
                    )

        self.assertIn(
            ("memo_attachments", ATTACH_B),
            {(item["table"], item["rowId"]) for item in changes},
        )
        with selected_database(self.NODE_B):
            with get_db_connection(sync_applying=True) as conn:
                with conn.cursor() as cursor:
                    _authorize_sync_changes(cursor, changes, OWNER)
                    outcome = apply_changes(cursor, changes, SIDE_SERVER)
            self.assertFalse(outcome.rejected)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT memo_id,origin_memo_id,object_key,created_by "
                        "FROM memo_attachments WHERE id=%s",
                        (ATTACH_B,),
                    )
                    metadata = cursor.fetchone()
                    cursor.execute(
                        "SELECT deleted_at FROM memos WHERE id='shared-source'"
                    )
                    loser = cursor.fetchone()
        self.assertEqual(metadata["memo_id"], "shared-winner")
        self.assertEqual(metadata["origin_memo_id"], "shared-source")
        self.assertEqual(metadata["created_by"], B)
        self.assertEqual(
            metadata["object_key"],
            f"todo/memos/shared-source/{ATTACH_B}",
        )
        self.assertIsNotNone(loser["deleted_at"])


class V4SchemaTests(unittest.TestCase):
    def test_v4_declares_attachment_dependency_after_memos(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 4)
        self.assertIn("memo_attachments", declared_tables())
        self.assertLess(
            SYNC_TABLE_ORDER.index("memos"),
            SYNC_TABLE_ORDER.index("memo_attachments"),
        )

    def test_v3_drift_is_fail_closed_on_push_and_pull(self) -> None:
        body = SyncPushRequest(
            clientId="laptop:123e4567-e89b-42d3-a456-426614174000",
            schemaVersion=3,
            allowSchemaDrift=True,
            tables=declared_tables(),
            changes=[SyncChangeInput(table="memos", rowId="m")],
        )
        with self.assertRaises(HTTPException) as push:
            _resolve_push_columns(body)
        self.assertEqual(
            push.exception.detail["code"],
            "sync_private_media_schema_upgrade_required",
        )
        daemon = SyncDaemon()
        with self.assertRaises(SyncBlocked):
            daemon._check_schema({"schemaVersion": 3, "tables": declared_tables()})


if __name__ == "__main__":
    unittest.main()
