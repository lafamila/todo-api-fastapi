"""동기화 적용 정책을 **실제 MySQL/MariaDB 스크래치 DB** 로 검증한다.

트리거·`@sync_applying`·LWW·중복 감지는 DB 동작에 의존하므로 mock 으로는 의미가 없다.
`TODO_SYNC_TEST_DB` (기본 `teddynote_sync_t`) 를 매 테스트마다 비우고 쓴다. 대상이
스크래치 DB 가 아니면 `tests/scratch_db.py` 가 실행을 거부한다 (실사용 DB 보호).
접속이 안 되면 스킵한다 (CI 에 MySQL 이 없어도 나머지 테스트가 죽지 않게).

    venv/bin/python3.13 -m unittest tests.test_sync_apply
"""

import unittest
from datetime import datetime, timedelta

from tests.scratch_db import (
    init_scratch_database,
    truncate_scratch_tables,
    use_scratch_database,
)

# import 순서와 무관하게 스크래치 DB 로 확정한다 (실사용 DB 보호 — scratch_db.py 참조)
SCRATCH_DB = use_scratch_database()

from src.connectors import DB_CONFIG, get_db_connection  # noqa: E402
from src.services.merge import MergeError, merge_memos, merge_projects  # noqa: E402
from src.services.sync_apply import SIDE_CLIENT, SIDE_SERVER, apply_changes  # noqa: E402
from src.services.sync_store import (  # noqa: E402
    collect_local_changes,
    list_issues,
    max_change_seq,
    read_changes,
    serialize_row,
)
from src.timeutil import iso_utc  # noqa: E402

ACCOUNT = "account-under-test"
BASE = datetime(2026, 7, 29, 5, 0, 0)


REACHABLE = init_scratch_database()


@unittest.skipUnless(REACHABLE, f"MySQL scratch DB unavailable ({DB_CONFIG['database']})")
class SyncApplyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 스크래치 DB 를 비운다 (대상 DB 이름을 매번 다시 검증한다)
        truncate_scratch_tables()

    # -- helpers -----------------------------------------------------------

    def _insert_project(self, project_id: str, name: str, moment: datetime) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, owner_id, name, icon, status, is_secret,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, %s, 'Beer', 0, 0, %s, %s, %s)
                    """,
                    (project_id, ACCOUNT, name, moment, moment, moment),
                )
                cursor.execute(
                    """
                    INSERT INTO project_members
                        (id, project_id, user_id, username, display_name, email, role,
                         invited_at, updated_at_utc)
                    VALUES (%s, %s, %s, 'lafamila', 'lafamila', 'l@example.com', 'owner', %s, %s)
                    """,
                    (f"pm-{project_id}", project_id, ACCOUNT, moment, moment),
                )

    def _insert_memo(
        self, memo_id: str, project_id: str, title: str, content: str, moment: datetime
    ) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO memos
                        (id, project_id, created_by, title, content, status,
                         created_at, updated_at, updated_at_utc)
                    VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s)
                    """,
                    (memo_id, project_id, ACCOUNT, title, content, moment, moment, moment),
                )

    def _memo(self, memo_id: str) -> dict:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM memos WHERE id = %s", (memo_id,))
                return cursor.fetchone()

    def _versions(self, memo_id: str) -> list[dict]:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM memo_versions WHERE memo_id = %s ORDER BY version",
                    (memo_id,),
                )
                return cursor.fetchall()

    def _issues(self, kind: str | None = None) -> list[dict]:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                return list_issues(cursor, kind=kind)

    def _change_log(self) -> list[dict]:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM change_log ORDER BY seq")
                return cursor.fetchall()

    def _apply(self, changes: list[dict], incoming_side: str):
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                return apply_changes(cursor, changes, incoming_side=incoming_side)

    def _memo_change(
        self, memo_id: str, project_id: str, title: str, content: str, moment: datetime
    ) -> dict:
        return {
            "table": "memos",
            "rowId": memo_id,
            "op": "update",
            "row": {
                "id": memo_id,
                "project_id": project_id,
                "created_by": ACCOUNT,
                "title": title,
                "content": content,
                "status": 0,
                "created_at": moment.isoformat(timespec="milliseconds"),
                "updated_at": moment.isoformat(timespec="milliseconds"),
                "updated_at_utc": iso_utc(moment),
                "deleted_at": None,
            },
        }


class ChangeLogTriggerTests(SyncApplyTestCase):
    def test_normal_writes_are_recorded(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "hello", BASE)
        tables = [row["table_name"] for row in self._change_log()]
        self.assertIn("projects", tables)
        self.assertIn("memos", tables)
        self.assertIn("project_members", tables)

    def test_sync_applying_writes_are_not_recorded(self) -> None:
        """핑퐁 없음 — 동기화로 적용한 쓰기는 change_log 에 남지 않는다."""
        self._insert_project("p1", "Project", BASE)
        before = max_change_seq_now()
        self._apply(
            [self._memo_change("m1", "p1", "Memo", "from peer", BASE)], SIDE_SERVER
        )
        self.assertEqual(self._memo("m1")["content"], "from peer")
        self.assertEqual(max_change_seq_now(), before)


class ConflictPolicyTests(SyncApplyTestCase):
    def test_later_timestamp_wins_and_loser_is_preserved(self) -> None:
        """시나리오 1 — 양쪽에서 같은 메모 수정 → 늦은 쪽이 현재값, 패자는 버전으로 보존."""
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "server content", BASE + timedelta(seconds=10))

        outcome = self._apply(
            [
                self._memo_change(
                    "m1", "p1", "Memo", "client content", BASE + timedelta(seconds=30)
                )
            ],
            SIDE_CLIENT,
        )

        self.assertEqual(outcome.applied, 1)
        self.assertEqual(len(outcome.conflicts), 1)
        self.assertEqual(outcome.conflicts[0]["winner"], "incoming")
        self.assertEqual(outcome.conflicts[0]["loserSide"], "server")
        self.assertEqual(self._memo("m1")["content"], "client content")

        versions = self._versions("m1")
        self.assertEqual([v["content"] for v in versions], ["server content"])
        self.assertTrue(versions[0]["note"].startswith("충돌 · 원격"))

        issues = self._issues("conflict")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["refId"], "m1")

    def test_older_incoming_loses_but_content_is_still_preserved(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "server newer", BASE + timedelta(seconds=60))

        outcome = self._apply(
            [self._memo_change("m1", "p1", "Memo", "client older", BASE + timedelta(seconds=5))],
            SIDE_CLIENT,
        )

        self.assertEqual(outcome.applied, 0)
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(self._memo("m1")["content"], "server newer")
        versions = self._versions("m1")
        self.assertEqual([v["content"] for v in versions], ["client older"])
        self.assertTrue(versions[0]["note"].startswith("충돌 · 로컬"))

    def test_tie_goes_to_the_server_on_push(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "server value", BASE)

        outcome = self._apply(
            [self._memo_change("m1", "p1", "Memo", "client value", BASE)], SIDE_CLIENT
        )
        self.assertEqual(self._memo("m1")["content"], "server value")
        self.assertEqual(outcome.conflicts[0]["winner"], "existing")

    def test_tie_goes_to_the_server_on_pull(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "client value", BASE)

        outcome = self._apply(
            [self._memo_change("m1", "p1", "Memo", "server value", BASE)], SIDE_SERVER
        )
        self.assertEqual(self._memo("m1")["content"], "server value")
        self.assertEqual(outcome.conflicts[0]["winner"], "incoming")

    def test_identical_row_reapply_is_idempotent_and_silent(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "same", BASE)

        change = self._memo_change("m1", "p1", "Memo", "same", BASE)
        outcome = self._apply([change], SIDE_SERVER)
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.conflicts, [])
        self.assertEqual(list(self._issues()), [])

    def test_preserved_conflict_version_enters_change_log(self) -> None:
        """보존 버전은 상대 노드도 받아야 하므로 change_log 에 남아야 한다."""
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "server content", BASE)
        before = max_change_seq_now()

        self._apply(
            [self._memo_change("m1", "p1", "Memo", "client content", BASE + timedelta(seconds=5))],
            SIDE_CLIENT,
        )

        new_entries = [row for row in self._change_log() if int(row["seq"]) > before]
        self.assertEqual([row["table_name"] for row in new_entries], ["memo_versions"])


class DeleteVersusEditTests(SyncApplyTestCase):
    def test_newer_delete_wins_over_older_edit(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "content", BASE)

        change = self._memo_change("m1", "p1", "Memo", "content", BASE + timedelta(seconds=10))
        change["row"]["deleted_at"] = iso_utc(BASE + timedelta(seconds=10))
        self._apply([change], SIDE_SERVER)

        self.assertIsNotNone(self._memo("m1")["deleted_at"])

    def test_newer_edit_revives_soft_deleted_row(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "content", BASE)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE memos SET deleted_at = %s, updated_at_utc = %s WHERE id = %s",
                    (BASE, BASE, "m1"),
                )

        self._apply(
            [self._memo_change("m1", "p1", "Memo", "revived", BASE + timedelta(seconds=30))],
            SIDE_SERVER,
        )
        memo = self._memo("m1")
        self.assertIsNone(memo["deleted_at"])
        self.assertEqual(memo["content"], "revived")


class DependencyOrderTests(SyncApplyTestCase):
    def test_project_and_child_memo_apply_in_one_batch(self) -> None:
        """시나리오 2 — 오프라인에서 프로젝트+그 안의 메모 생성 → 의존성 순서로 둘 다 반영."""
        project_change = {
            "table": "projects",
            "rowId": "p-new",
            "op": "insert",
            "row": {
                "id": "p-new",
                "owner_id": ACCOUNT,
                "name": "Offline project",
                "icon": "Beer",
                "status": 0,
                "is_secret": False,
                "created_at": BASE.isoformat(timespec="milliseconds"),
                "updated_at": BASE.isoformat(timespec="milliseconds"),
                "updated_at_utc": iso_utc(BASE),
                "deleted_at": None,
            },
        }
        memo_change = self._memo_change("m-new", "p-new", "Offline memo", "body", BASE)

        # 메모가 먼저 오더라도 적용 순서는 projects → memos 로 정렬된다
        outcome = self._apply([memo_change, project_change], SIDE_SERVER)

        self.assertEqual(outcome.applied, 2)
        self.assertEqual(outcome.deferred, [])
        self.assertEqual(self._memo("m-new")["project_id"], "p-new")

    def test_orphan_row_is_deferred_not_rejected(self) -> None:
        outcome = self._apply(
            [self._memo_change("m-orphan", "p-missing", "Orphan", "body", BASE)], SIDE_SERVER
        )
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(len(outcome.deferred), 1)
        self.assertIn("missing_parent:projects:p-missing", outcome.deferred[0]["reason"])
        self.assertIsNone(self._memo("m-orphan"))


class DuplicateDetectionTests(SyncApplyTestCase):
    def test_duplicate_memo_title_is_recorded_not_blocked(self) -> None:
        """시나리오 3 — 같은 제목 메모가 양쪽에서 생겨도 차단하지 않고 기록한다."""
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m-local", "p1", "같은 제목", "local body", BASE)

        outcome = self._apply(
            [self._memo_change("m-remote", "p1", "같은 제목", "remote body", BASE)],
            SIDE_CLIENT,
        )

        self.assertEqual(outcome.applied, 1)
        self.assertEqual(len(outcome.duplicates), 1)
        self.assertEqual(outcome.duplicates[0]["kind"], "duplicate_memo")
        issues = self._issues("duplicate_memo")
        self.assertEqual(len(issues), 1)
        self.assertEqual({issues[0]["refId"], issues[0]["peerRefId"]}, {"m-local", "m-remote"})

    def test_duplicate_detection_normalizes_nfc_and_whitespace(self) -> None:
        import unicodedata

        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m-local", "p1", "한글", "local", BASE)
        decomposed = unicodedata.normalize("NFD", " 한글 ")

        outcome = self._apply(
            [self._memo_change("m-remote", "p1", decomposed, "remote", BASE)], SIDE_CLIENT
        )
        self.assertEqual(len(outcome.duplicates), 1)

    def test_duplicate_detection_keeps_case_distinction(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m-local", "p1", "Todo", "local", BASE)

        outcome = self._apply(
            [self._memo_change("m-remote", "p1", "todo", "remote", BASE)], SIDE_CLIENT
        )
        self.assertEqual(outcome.duplicates, [])

    def test_duplicate_project_name_is_recorded(self) -> None:
        self._insert_project("p1", "같은 프로젝트", BASE)
        change = {
            "table": "projects",
            "rowId": "p2",
            "op": "insert",
            "row": {
                "id": "p2",
                "owner_id": ACCOUNT,
                "name": "같은 프로젝트",
                "icon": "Beer",
                "status": 0,
                "is_secret": False,
                "created_at": BASE.isoformat(timespec="milliseconds"),
                "updated_at": BASE.isoformat(timespec="milliseconds"),
                "updated_at_utc": iso_utc(BASE),
                "deleted_at": None,
            },
        }
        outcome = self._apply([change], SIDE_CLIENT)
        self.assertEqual(outcome.duplicates[0]["kind"], "duplicate_project")

    def test_soft_deleted_rows_are_not_duplicate_candidates(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m-old", "p1", "같은 제목", "old", BASE)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE memos SET deleted_at = %s WHERE id = 'm-old'", (BASE,)
                )

        outcome = self._apply(
            [self._memo_change("m-new", "p1", "같은 제목", "new", BASE)], SIDE_CLIENT
        )
        self.assertEqual(outcome.duplicates, [])


class ChangeFeedTests(SyncApplyTestCase):
    def test_changes_are_scoped_to_the_account(self) -> None:
        self._insert_project("p-mine", "Mine", BASE)
        self._insert_memo("m-mine", "p-mine", "Mine", "body", BASE)
        # 다른 계정 소유 프로젝트 — 이 계정의 피드에 나오면 안 된다
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO projects
                        (id, owner_id, name, icon, status, is_secret,
                         created_at, updated_at, updated_at_utc)
                    VALUES ('p-other', 'someone-else', 'Other', 'Beer', 0, 0, %s, %s, %s)
                    """,
                    (BASE, BASE, BASE),
                )

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                changes, next_seq = read_changes(cursor, 0, 500, ACCOUNT)

        row_ids = {change["rowId"] for change in changes}
        self.assertIn("m-mine", row_ids)
        self.assertIn("p-mine", row_ids)
        self.assertNotIn("p-other", row_ids)
        # 걸러진 행이 있어도 커서는 전진해야 한다 (같은 구간을 무한히 다시 읽지 않게)
        self.assertEqual(next_seq, max_change_seq_now())

    def test_collect_local_changes_deduplicates_repeated_edits(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "v1", BASE)
        for content in ("v2", "v3"):
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE memos SET content = %s, updated_at_utc = %s WHERE id = 'm1'",
                        (content, BASE),
                    )

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                changes, _ = collect_local_changes(cursor, 0, 500)

        memo_changes = [c for c in changes if c["table"] == "memos"]
        self.assertEqual(len(memo_changes), 1)
        self.assertEqual(memo_changes[0]["row"]["content"], "v3")

    def test_serialize_row_emits_z_suffixed_utc_and_naive_wall_clock(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "body", BASE)
        payload = serialize_row("memos", self._memo("m1"))
        self.assertTrue(payload["updated_at_utc"].endswith("Z"))
        self.assertFalse(payload["created_at"].endswith("Z"))


class MergeTests(SyncApplyTestCase):
    def test_memo_merge_folds_loser_content_and_renumbers_versions(self) -> None:
        """시나리오 3 후속 — merge-into 로 정리되며 내용이 버전으로 합쳐진다."""
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m-win", "p1", "같은 제목", "winner body", BASE)
        self._insert_memo("m-lose", "p1", "같은 제목", "loser body", BASE)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                for index, content in enumerate(("loser v1", "loser v2"), start=1):
                    cursor.execute(
                        """
                        INSERT INTO memo_versions
                            (id, memo_id, content, version, note, created_at, updated_at_utc)
                        VALUES (%s, 'm-lose', %s, %s, NULL, %s, %s)
                        """,
                        (f"v{index}", content, index, BASE, BASE),
                    )
                cursor.execute(
                    """
                    INSERT INTO memo_versions
                        (id, memo_id, content, version, note, created_at, updated_at_utc)
                    VALUES ('vw1', 'm-win', 'winner v1', 1, NULL, %s, %s)
                    """,
                    (BASE, BASE),
                )

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                result = merge_memos(cursor, "m-lose", "m-win")

        self.assertEqual(result["movedVersions"], 2)
        self.assertEqual(result["movedContentVersion"], 2)
        versions = self._versions("m-win")
        self.assertEqual([v["version"] for v in versions], [1, 2, 3, 4])
        self.assertEqual(
            [v["content"] for v in versions],
            ["winner v1", "loser body", "loser v1", "loser v2"],
        )
        self.assertTrue(versions[1]["note"].startswith("병합 · 같은 제목"))
        self.assertEqual(list(self._versions("m-lose")), [])
        self.assertIsNotNone(self._memo("m-lose")["deleted_at"])
        self.assertIsNone(self._memo("m-win")["deleted_at"])

    def test_memo_merge_resolves_duplicate_issues(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m-win", "p1", "같은 제목", "winner", BASE)
        self._apply(
            [self._memo_change("m-lose", "p1", "같은 제목", "loser", BASE)], SIDE_CLIENT
        )
        self.assertEqual(len(self._issues("duplicate_memo")), 1)

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                merge_memos(cursor, "m-lose", "m-win")

        self.assertEqual(list(self._issues("duplicate_memo")), [])

    def test_project_merge_reparents_memos_and_merges_members(self) -> None:
        self._insert_project("p-win", "같은 프로젝트", BASE)
        self._insert_project("p-lose", "같은 프로젝트", BASE)
        self._insert_memo("m1", "p-lose", "Memo", "body", BASE)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO project_members
                        (id, project_id, user_id, username, display_name, email, role,
                         invited_at, updated_at_utc)
                    VALUES ('pm-extra', 'p-lose', 'other-user', 'other', 'other',
                            'o@example.com', 'editor', %s, %s)
                    """,
                    (BASE, BASE),
                )

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                result = merge_projects(cursor, "p-lose", "p-win")
                cursor.execute(
                    "SELECT user_id, role FROM project_members "
                    "WHERE project_id = 'p-win' AND deleted_at IS NULL ORDER BY user_id"
                )
                members = cursor.fetchall()

        self.assertEqual(result["movedMemos"], 1)
        self.assertEqual(result["mergedMembers"], 1)
        self.assertEqual(self._memo("m1")["project_id"], "p-win")
        self.assertEqual(
            [(m["user_id"], m["role"]) for m in members],
            [(ACCOUNT, "owner"), ("other-user", "editor")],
        )
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT deleted_at FROM projects WHERE id = 'p-lose'")
                self.assertIsNotNone(cursor.fetchone()["deleted_at"])

    def test_merge_rejects_same_id(self) -> None:
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m1", "p1", "Memo", "body", BASE)
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                with self.assertRaises(MergeError):
                    merge_memos(cursor, "m1", "m1")

    def test_merge_writes_are_visible_to_the_change_feed(self) -> None:
        """병합은 원격에서 실행되고 로컬은 pull 로 받는다 → change_log 에 남아야 한다."""
        self._insert_project("p1", "Project", BASE)
        self._insert_memo("m-win", "p1", "A", "winner", BASE)
        self._insert_memo("m-lose", "p1", "B", "loser", BASE)
        before = max_change_seq_now()

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                merge_memos(cursor, "m-lose", "m-win")

        new_tables = {
            row["table_name"] for row in self._change_log() if int(row["seq"]) > before
        }
        self.assertEqual(new_tables, {"memos", "memo_versions"})


def max_change_seq_now() -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            return max_change_seq(cursor)


if __name__ == "__main__":
    unittest.main()
