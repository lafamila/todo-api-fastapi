#!/usr/bin/env python
"""레거시 todo DB(:3030 스택, `todo-fastapi` 백엔드) → todo-api-fastapi 스키마 마이그레이션.

소스 스키마 (legacy `todo` database — MariaDB :33306):
    project (project_id INT PK, project_name, project_icon, project_status 0|1, reg_dtm)
    task    (task_id INT PK, project_id, task_title, task_status 0|1, reg_dtm)
    detail  (task_id, content TEXT, reg_dtm)  -- append-only 컨텐츠 히스토리 (task 당 여러 행)

매핑:
    project        → projects        (id = legacy-project-{id}, icon 은 동일 SVG 이름 어휘라 그대로)
    task           → memos           (id = legacy-memo-{id}, status = task_status 그대로)
    detail 최신본   → memos.content   (updated_at = 해당 detail 의 reg_dtm)
    detail 과거본   → memo_versions   (version 1..N-1 — 서버의 MAX(version)+1 증가와 정합)
    프로젝트별 owner → project_members (role='owner' — 신규 서비스의 프로젝트 가시성은 멤버십 기반)

동작 원칙:
    - 소스는 읽기 전용.
    - 기본 동작은 **추가형(additive) upsert** — 대상의 기존 데이터를 삭제하지 않으며,
      결정적 id(legacy-*) 로 재실행해도 안전(idempotent)하다.
    - `--replace` 를 명시한 경우에만 대상 todo 테이블을 비우고 새로 적재한다 (확인 문자열 필요).
    - 대상 스키마 드리프트 자동 감지: 실DB 에 `projects.owner_id` / `memos.created_by` 가
      있으면 채우고(현 로컬 DB), 없으면(순정 init_db 스키마) 해당 컬럼 없이 INSERT 한다.
    - 날짜/시간: 소스·대상 모두 Asia/Seoul naive DATETIME — 변환 없이 그대로 옮긴다 (원칙 8).
    - detail 히스토리 정렬은 (reg_dtm, content). UNIQUE(task_id, content) 라 순서가 결정적이다.

사용 예 (로컬 — 대상 기본값은 레포 .env 의 DB_*):
    venv/bin/python scripts/migrate_legacy_todo.py --source-password 'P@ssw0rd' --dry-run
    venv/bin/python scripts/migrate_legacy_todo.py --source-password 'P@ssw0rd'

사용 예 (운영 DB 로 — 대상 접속 정보를 파라미터로 지정):
    venv/bin/python scripts/migrate_legacy_todo.py \
        --source-host 127.0.0.1 --source-port 33306 --source-password '...' \
        --target-host <prod-host> --target-port 3306 --target-user <user> \
        --target-password '...' --target-database <db> \
        --owner-user-id '<auth account id>'

    --dry-run  : 대상에 SQL 을 실제 실행해 제약 위반까지 검증한 뒤 커밋 대신 롤백.
    --replace  : 대상 todo 테이블(articles/memo_versions/memos/project_members/projects)을
                 비우고 적재. 반드시 --confirm-replace <target database 이름> 을 함께 요구한다.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

import pymysql

try:  # dotenv 는 편의 기능 — 없으면 CLI 파라미터/환경변수만으로 동작한다
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

UNTITLED = "(제목 없음)"
DEFAULT_ICON = "Beer"  # 신규 앱 AllIcons 기본값 — 소스 icon 이 비어있을 때만 사용
ICON_MAX_LEN = 10  # projects.icon VARCHAR(10)

# 단일 사용자 서비스의 owner 기본 identity — env 또는 CLI 로 덮어쓸 수 있다.
# (기본 id 는 현행 auth 계정 — 과거 스크립트의 89ef19ed-... 는 구 auth DB 시절 값이라 폐기)
DEFAULT_OWNER_ID = os.getenv("TODO_MIGRATION_OWNER_ID", "e1ecab2f-32ed-4590-81bd-e9975cf3667f")
DEFAULT_OWNER_LOGIN = os.getenv("TODO_MIGRATION_OWNER_LOGIN", "lafamila")
DEFAULT_OWNER_NAME = os.getenv("TODO_MIGRATION_OWNER_NAME", "lafamila")
DEFAULT_OWNER_EMAIL = os.getenv("TODO_MIGRATION_OWNER_EMAIL", "lafamila325@gmail.com")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="레거시 todo DB → todo-api-fastapi 마이그레이션 (기본: additive upsert)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = parser.add_argument_group("source (legacy todo DB)")
    src.add_argument("--source-host", default=os.getenv("SOURCE_DB_HOST", "127.0.0.1"))
    src.add_argument("--source-port", type=int, default=int(os.getenv("SOURCE_DB_PORT", "33306")))
    src.add_argument("--source-user", default=os.getenv("SOURCE_DB_USER", "root"))
    src.add_argument("--source-password", default=os.getenv("SOURCE_DB_PASSWORD", ""))
    src.add_argument("--source-database", default=os.getenv("SOURCE_DB_NAME", "todo"))

    dst = parser.add_argument_group("target (todo-api-fastapi DB — 기본값은 레포 .env 의 DB_*)")
    dst.add_argument("--target-host", default=os.getenv("DB_HOST", "localhost"))
    dst.add_argument("--target-port", type=int, default=int(os.getenv("DB_PORT", "3306")))
    dst.add_argument("--target-user", default=os.getenv("DB_USER", "root"))
    dst.add_argument("--target-password", default=os.getenv("DB_PASSWORD", ""))
    dst.add_argument("--target-database", default=os.getenv("DB_NAME", "teddynote"))

    owner = parser.add_argument_group("owner membership (프로젝트 가시성에 필수)")
    owner.add_argument("--owner-user-id", default=DEFAULT_OWNER_ID)
    owner.add_argument("--owner-username", default=DEFAULT_OWNER_LOGIN)
    owner.add_argument("--owner-display-name", default=DEFAULT_OWNER_NAME)
    owner.add_argument("--owner-email", default=DEFAULT_OWNER_EMAIL)

    parser.add_argument("--dry-run", action="store_true", help="SQL 실행 후 커밋 대신 롤백")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="대상 todo 테이블을 비우고 적재 (파괴적 — --confirm-replace 필요)",
    )
    parser.add_argument(
        "--confirm-replace",
        default=None,
        metavar="TARGET_DB_NAME",
        help="--replace 확인용 — 대상 database 이름을 그대로 입력해야 동작",
    )
    args = parser.parse_args()

    if args.replace and args.confirm_replace != args.target_database:
        parser.error(
            "--replace 는 파괴적입니다. --confirm-replace "
            f"'{args.target_database}' 를 함께 지정해야 실행됩니다."
        )
    return args


def connect(host: str, port: int, user: str, password: str, database: str):
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def load_source(conn):
    """소스를 읽기 전용으로 로드. detail 은 task 별 (reg_dtm, content) 정렬 히스토리."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT project_id, project_name, project_icon, project_status, reg_dtm "
            "FROM project ORDER BY project_id"
        )
        projects = cursor.fetchall()

        cursor.execute(
            """
            SELECT t.task_id, t.project_id, t.task_title, t.task_status, t.reg_dtm
            FROM task t JOIN project p ON p.project_id = t.project_id
            ORDER BY t.task_id
            """
        )
        tasks = cursor.fetchall()

        cursor.execute(
            """
            SELECT d.task_id, d.content, d.reg_dtm
            FROM detail d JOIN task t ON t.task_id = d.task_id
            ORDER BY d.task_id, d.reg_dtm, d.content
            """
        )
        details = defaultdict(list)
        for row in cursor.fetchall():
            details[row["task_id"]].append(row)
    return projects, tasks, details


def table_columns(cursor, table: str) -> set[str]:
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    return {row["Field"] for row in cursor.fetchall()}


def ensure_status_columns(cursor) -> None:
    """구버전 대상 스키마 보강 — 이미 있으면(1060) 무시."""
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


def upsert(cursor, table: str, columns: list[str], values: list, update_columns: list[str]) -> None:
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(columns)
    update_sql = ", ".join(f"{c} = VALUES({c})" for c in update_columns)
    cursor.execute(
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {update_sql}",
        values,
    )


def migrate(args: argparse.Namespace) -> int:
    source = connect(
        args.source_host, args.source_port, args.source_user, args.source_password, args.source_database
    )
    projects, tasks, details = load_source(source)
    source.close()
    print(
        f"source: projects={len(projects)} tasks={len(tasks)} "
        f"detail rows={sum(len(v) for v in details.values())}"
    )

    target = connect(
        args.target_host, args.target_port, args.target_user, args.target_password, args.target_database
    )
    stats = defaultdict(int)
    try:
        with target.cursor() as cursor:
            ensure_status_columns(cursor)
            # 스키마 드리프트 감지: 실DB 에만 존재하는 소유자 컬럼은 있을 때만 채운다
            has_project_owner = "owner_id" in table_columns(cursor, "projects")
            has_memo_creator = "created_by" in table_columns(cursor, "memos")
            print(
                f"target schema: projects.owner_id={'있음' if has_project_owner else '없음'}, "
                f"memos.created_by={'있음' if has_memo_creator else '없음'}"
            )

            if args.replace:
                clear_target(cursor)
                print("[replace] 대상 todo 테이블을 비웠습니다.")

            for project in projects:
                project_uuid = f"legacy-project-{project['project_id']}"
                icon = (project["project_icon"] or DEFAULT_ICON)[:ICON_MAX_LEN]
                columns = ["id", "name", "icon", "status", "is_secret", "created_at", "updated_at"]
                values = [
                    project_uuid,
                    project["project_name"] or UNTITLED,
                    icon,
                    project["project_status"] or 0,
                    False,
                    project["reg_dtm"],
                    project["reg_dtm"],
                ]
                if has_project_owner:
                    columns.insert(1, "owner_id")
                    values.insert(1, args.owner_user_id)
                upsert(cursor, "projects", columns, values, ["name", "icon", "status", "created_at"])
                stats["projects"] += 1

                upsert(
                    cursor,
                    "project_members",
                    ["id", "project_id", "user_id", "username", "display_name", "email", "role", "invited_at"],
                    [
                        f"legacy-project-member-{project['project_id']}",
                        project_uuid,
                        args.owner_user_id,
                        args.owner_username,
                        args.owner_display_name,
                        args.owner_email,
                        "owner",
                        project["reg_dtm"],
                    ],
                    ["role", "username", "display_name", "email"],
                )
                stats["members"] += 1

            for task in tasks:
                memo_uuid = f"legacy-memo-{task['task_id']}"
                history = details.get(task["task_id"], [])
                latest = history[-1] if history else None
                content = latest["content"] if latest else ""
                updated_at = latest["reg_dtm"] if latest else task["reg_dtm"]

                columns = ["id", "project_id", "title", "content", "status", "created_at", "updated_at"]
                values = [
                    memo_uuid,
                    f"legacy-project-{task['project_id']}",
                    (task["task_title"] or UNTITLED)[:255],
                    content,
                    task["task_status"] or 0,
                    task["reg_dtm"],
                    updated_at,
                ]
                if has_memo_creator:
                    columns.insert(2, "created_by")
                    values.insert(2, args.owner_user_id)
                upsert(
                    cursor,
                    "memos",
                    columns,
                    values,
                    ["title", "content", "status", "created_at", "updated_at"],
                )
                stats["memos"] += 1

                # 과거 히스토리(최신 제외) → memo_versions v1..N-1
                for version, row in enumerate(history[:-1], start=1):
                    upsert(
                        cursor,
                        "memo_versions",
                        ["id", "memo_id", "content", "version", "created_at"],
                        [
                            f"legacy-version-{task['task_id']}-{version}",
                            memo_uuid,
                            row["content"],
                            version,
                            row["reg_dtm"],
                        ],
                        ["content", "version", "created_at"],
                    )
                    stats["versions"] += 1

        if args.dry_run:
            target.rollback()
            print("[dry-run] 모든 SQL 실행 후 롤백했습니다 (대상 무변경).")
        else:
            target.commit()
            print("커밋 완료.")
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()

    print(
        f"upserted: projects={stats['projects']} members={stats['members']} "
        f"memos={stats['memos']} versions={stats['versions']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(migrate(parse_args()))
