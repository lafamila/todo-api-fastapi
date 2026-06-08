import os
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime

import pymysql


OWNER_ID = os.getenv("TODO_MIGRATION_OWNER_ID", "89ef19ed-265f-4ed8-bffb-9a65f9df3c02")
OWNER_LOGIN = os.getenv("TODO_MIGRATION_OWNER_LOGIN", "lafamila")
OWNER_NAME = os.getenv("TODO_MIGRATION_OWNER_NAME", "lafamila")
OWNER_EMAIL = os.getenv("TODO_MIGRATION_OWNER_EMAIL", "lafamila325@gmail.com")


def db_config(prefix: str, default_port: int) -> dict:
    return {
        "host": os.getenv(f"{prefix}_DB_HOST", "127.0.0.1"),
        "port": int(os.getenv(f"{prefix}_DB_PORT", default_port)),
        "user": os.getenv(f"{prefix}_DB_USER", "root"),
        "password": os.getenv(f"{prefix}_DB_PASSWORD", "P@ssw0rd"),
        "database": os.getenv(f"{prefix}_DB_NAME", "todo"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
    }


@contextmanager
def connection(config: dict):
    conn = pymysql.connect(**config)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_status_columns(cursor) -> None:
    for sql in (
        "ALTER TABLE projects ADD COLUMN status INT NOT NULL DEFAULT 0 AFTER icon",
        "ALTER TABLE memos ADD COLUMN status INT NOT NULL DEFAULT 0 AFTER content",
    ):
        try:
            cursor.execute(sql)
        except pymysql.err.OperationalError as exc:
            if exc.args[0] != 1060:
                raise


def clear_target(cursor) -> None:
    for table in ("articles", "memo_versions", "memos", "project_members", "projects"):
        cursor.execute(f"DELETE FROM {table}")


def fetch_source_data(source_cursor):
    source_cursor.execute(
        """
        SELECT project_id, project_name, project_icon, project_status, reg_dtm
        FROM project
        ORDER BY project_id
        """
    )
    projects = source_cursor.fetchall()

    source_cursor.execute(
        """
        SELECT task_id, project_id, task_title, task_status, reg_dtm
        FROM task
        ORDER BY task_id
        """
    )
    tasks = source_cursor.fetchall()

    source_cursor.execute(
        """
        SELECT task_id, content, reg_dtm
        FROM detail
        ORDER BY task_id, reg_dtm, content
        """
    )
    details_by_task = defaultdict(list)
    for detail in source_cursor.fetchall():
        details_by_task[detail["task_id"]].append(detail)

    return projects, tasks, details_by_task


def migrate_projects(cursor, projects) -> None:
    now = datetime.now()
    for project in projects:
        project_id = f"legacy-project-{project['project_id']}"
        created_at = project["reg_dtm"] or now
        cursor.execute(
            """
            INSERT INTO projects
                (id, owner_id, name, icon, status, is_secret, password, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                project_id,
                OWNER_ID,
                project["project_name"],
                project["project_icon"],
                project["project_status"],
                0,
                None,
                created_at,
                created_at,
            ),
        )
        cursor.execute(
            """
            INSERT INTO project_members
                (id, project_id, user_id, username, display_name, email, role, invited_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                f"legacy-project-member-{project['project_id']}",
                project_id,
                OWNER_ID,
                OWNER_LOGIN,
                OWNER_NAME,
                OWNER_EMAIL,
                "owner",
                created_at,
            ),
        )


def migrate_tasks(cursor, tasks, details_by_task) -> None:
    now = datetime.now()
    for task in tasks:
        memo_id = f"legacy-memo-{task['task_id']}"
        project_id = f"legacy-project-{task['project_id']}"
        details = details_by_task.get(task["task_id"], [])
        latest_detail = details[-1] if details else None
        previous_details = details[:-1]
        created_at = task["reg_dtm"] or now
        updated_at = latest_detail["reg_dtm"] if latest_detail else created_at
        content = latest_detail["content"] if latest_detail else ""

        cursor.execute(
            """
            INSERT INTO memos
                (id, project_id, created_by, title, content, status, deleted_at, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                memo_id,
                project_id,
                OWNER_ID,
                task["task_title"],
                content,
                task["task_status"],
                None,
                created_at,
                updated_at,
            ),
        )

        for version, detail in enumerate(previous_details, start=1):
            cursor.execute(
                """
                INSERT INTO memo_versions
                    (id, memo_id, content, version, created_at)
                VALUES
                    (%s, %s, %s, %s, %s)
                """,
                (
                    f"legacy-version-{task['task_id']}-{version}",
                    memo_id,
                    detail["content"],
                    version,
                    detail["reg_dtm"] or created_at,
                ),
            )


def main() -> None:
    source_config = db_config("SOURCE", 33306)
    target_config = db_config("TARGET", 33307)

    with connection(source_config) as source, connection(target_config) as target:
        with source.cursor() as source_cursor, target.cursor() as target_cursor:
            projects, tasks, details_by_task = fetch_source_data(source_cursor)
            ensure_status_columns(target_cursor)
            clear_target(target_cursor)
            migrate_projects(target_cursor, projects)
            migrate_tasks(target_cursor, tasks, details_by_task)

            target_cursor.execute("SELECT COUNT(*) AS count FROM projects")
            project_count = target_cursor.fetchone()["count"]
            target_cursor.execute("SELECT COUNT(*) AS count FROM memos")
            memo_count = target_cursor.fetchone()["count"]
            target_cursor.execute("SELECT COUNT(*) AS count FROM memo_versions")
            version_count = target_cursor.fetchone()["count"]
            print(
                f"Migrated {project_count} projects, {memo_count} memos, "
                f"{version_count} memo_versions."
            )


if __name__ == "__main__":
    main()
