import unittest
from argparse import Namespace
from contextlib import nullcontext
from datetime import datetime
from unittest.mock import Mock, patch

from scripts import migrate_legacy_todo
from scripts import backfill_updated_at_utc


class LegacyTodoMigrationTimestampTests(unittest.TestCase):
    def test_migrated_rows_use_creation_time_as_updated_time(self) -> None:
        project_created_at = datetime(2025, 1, 2, 3, 4, 5)
        task_created_at = datetime(2025, 2, 3, 4, 5, 6)
        detail_created_at = datetime(2025, 3, 4, 5, 6, 7)
        projects = [
            {
                "project_id": 1,
                "project_name": "Project",
                "project_icon": "Beer",
                "project_status": 0,
                "reg_dtm": project_created_at,
            }
        ]
        tasks = [
            {
                "task_id": 2,
                "project_id": 1,
                "task_title": "Memo",
                "task_status": 0,
                "reg_dtm": task_created_at,
            }
        ]
        details = {
            2: [
                {
                    "task_id": 2,
                    "content": "Latest content",
                    "reg_dtm": detail_created_at,
                }
            ]
        }
        source = Mock()
        target = Mock()
        target.cursor.return_value = nullcontext(Mock())
        args = Namespace(
            source_host="source",
            source_port=3306,
            source_user="source",
            source_password="source",
            source_database="source",
            target_host="target",
            target_port=3306,
            target_user="target",
            target_password="target",
            target_database="target",
            owner_user_id="account-1",
            owner_username="lafamila",
            owner_display_name="lafamila",
            owner_email="lafamila@example.com",
            replace=False,
            dry_run=False,
        )

        with (
            patch.object(
                migrate_legacy_todo,
                "connect",
                side_effect=[source, target],
            ),
            patch.object(
                migrate_legacy_todo,
                "load_source",
                return_value=(projects, tasks, details),
            ),
            patch.object(migrate_legacy_todo, "ensure_status_columns"),
            patch.object(
                migrate_legacy_todo,
                "table_columns",
                side_effect=[
                    ["id", "owner_id"],
                    ["id", "created_by"],
                ],
            ),
            patch.object(migrate_legacy_todo, "upsert") as upsert,
        ):
            migrate_legacy_todo.migrate_legacy(args)

        project_call = next(
            call for call in upsert.call_args_list if call.args[1] == "projects"
        )
        project_columns = project_call.args[2]
        project_values = project_call.args[3]
        project_updates = project_call.args[4]
        self.assertEqual(
            project_values[project_columns.index("updated_at")],
            project_values[project_columns.index("created_at")],
        )
        self.assertIn("updated_at", project_updates)

        memo_call = next(
            call for call in upsert.call_args_list if call.args[1] == "memos"
        )
        memo_columns = memo_call.args[2]
        memo_values = memo_call.args[3]
        self.assertEqual(
            memo_values[memo_columns.index("updated_at")],
            memo_values[memo_columns.index("created_at")],
        )
        self.assertNotEqual(
            memo_values[memo_columns.index("updated_at")],
            detail_created_at,
        )

        member_call = next(
            call for call in upsert.call_args_list if call.args[1] == "project_members"
        )
        self.assertIn("updated_at_utc", member_call.args[4])


class MigrationDryRunSafetyTests(unittest.TestCase):
    def test_mirror_dry_run_executes_no_target_write_or_ddl(self) -> None:
        source = Mock()
        source_cursor = Mock()
        source.cursor.return_value = nullcontext(source_cursor)
        source_cursor.fetchall.side_effect = [[] for _ in migrate_legacy_todo.MIRROR_TABLES]
        target = Mock()
        target_cursor = Mock()
        target.cursor.return_value = nullcontext(target_cursor)
        args = Namespace(
            source_host="source",
            source_port=3306,
            source_user="source",
            source_password="source",
            source_database="source",
            target_host="target",
            target_port=3306,
            target_user="target",
            target_password="target",
            target_database="target",
            replace=True,
            dry_run=True,
            wipe_daily_tasks=True,
            sync_applying=True,
            allow_missing_utc=False,
        )

        with (
            patch.object(
                migrate_legacy_todo, "connect", side_effect=[source, target]
            ),
            patch.object(
                migrate_legacy_todo,
                "table_columns",
                side_effect=[["id"] for _ in range(len(migrate_legacy_todo.MIRROR_TABLES) * 2)],
            ),
            patch.object(
                migrate_legacy_todo, "check_updated_at_utc", return_value={}
            ),
            patch.object(
                migrate_legacy_todo,
                "target_row_counts",
                return_value={"projects": 3, "memos": 7},
            ),
            patch.object(migrate_legacy_todo, "ensure_status_columns") as ensure,
            patch.object(migrate_legacy_todo, "clear_target") as clear,
            patch.object(migrate_legacy_todo, "upsert") as upsert,
        ):
            result = migrate_legacy_todo.migrate_mirror(args)

        self.assertEqual(result, 0)
        target_cursor.execute.assert_not_called()
        ensure.assert_not_called()
        clear.assert_not_called()
        upsert.assert_not_called()
        target.commit.assert_not_called()

    def test_legacy_dry_run_executes_no_target_write_or_ddl(self) -> None:
        source = Mock()
        target = Mock()
        target_cursor = Mock()
        target.cursor.return_value = nullcontext(target_cursor)
        args = Namespace(
            source_host="source",
            source_port=3306,
            source_user="source",
            source_password="source",
            source_database="source",
            target_host="target",
            target_port=3306,
            target_user="target",
            target_password="target",
            target_database="target",
            owner_user_id="owner",
            owner_username="owner",
            owner_display_name="Owner",
            owner_email="owner@example.com",
            replace=True,
            dry_run=True,
            wipe_daily_tasks=True,
        )

        with (
            patch.object(
                migrate_legacy_todo, "connect", side_effect=[source, target]
            ),
            patch.object(
                migrate_legacy_todo, "load_source", return_value=([], [], {})
            ),
            patch.object(
                migrate_legacy_todo,
                "table_columns",
                side_effect=[
                    ["id", "owner_id"],
                    ["id", "created_by"],
                ],
            ),
            patch.object(
                migrate_legacy_todo,
                "target_row_counts",
                return_value={"projects": 3, "memos": 7},
            ),
            patch.object(migrate_legacy_todo, "ensure_status_columns") as ensure,
            patch.object(migrate_legacy_todo, "clear_target") as clear,
            patch.object(migrate_legacy_todo, "upsert") as upsert,
        ):
            result = migrate_legacy_todo.migrate_legacy(args)

        self.assertEqual(result, 0)
        target_cursor.execute.assert_not_called()
        ensure.assert_not_called()
        clear.assert_not_called()
        upsert.assert_not_called()
        target.commit.assert_not_called()

    def test_backfill_dry_run_is_select_only(self) -> None:
        connection = Mock()
        cursor = Mock()
        connection.cursor.return_value = nullcontext(cursor)
        cursor.fetchone.side_effect = [
            {"n": 2},
            {"n": 0},
            {"n": 1},
            {"n": 0},
            {"n": 0},
            {"n": 0},
            {"n": 0},
            {"n": 0},
        ]
        args = Namespace(
            host="target",
            port=3306,
            user="root",
            password="secret",
            database="todo",
            dry_run=True,
        )

        with (
            patch.object(backfill_updated_at_utc, "parse_args", return_value=args),
            patch.object(
                backfill_updated_at_utc.pymysql, "connect", return_value=connection
            ),
        ):
            result = backfill_updated_at_utc.main()

        self.assertEqual(result, 0)
        statements = [call.args[0].lstrip().upper() for call in cursor.execute.call_args_list]
        self.assertTrue(statements)
        self.assertTrue(all(statement.startswith("SELECT") for statement in statements))
        connection.commit.assert_not_called()
        connection.rollback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
