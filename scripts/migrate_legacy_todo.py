#!/usr/bin/env python
"""레거시 todo DB(:3030 스택, `todo-fastapi` 백엔드) → todo-api-fastapi 스키마 마이그레이션.

소스 스키마 (legacy `todo` database — MariaDB :33306):
    project (project_id INT PK, project_name, project_icon, project_status 0|1, reg_dtm)
    task    (task_id INT PK, project_id, task_title, task_status 0|1, reg_dtm)
    detail  (task_id, content TEXT, reg_dtm)  -- append-only 컨텐츠 히스토리 (task 당 여러 행)

매핑:
    project        → projects        (id = legacy-project-{id}, icon 은 동일 SVG 이름 어휘라 그대로)
    task           → memos           (id = legacy-memo-{id}, status = task_status 그대로)
    detail 최신본   → memos.content   (updated_at 은 task 생성시간과 동일하게 저장)
    detail 과거본   → memo_versions   (version 1..N-1 — 서버의 MAX(version)+1 증가와 정합)
    프로젝트별 owner → project_members (role='owner' — 신규 서비스의 프로젝트 가시성은 멤버십 기반)

동작 원칙:
    - 소스는 읽기 전용.
    - 기본 동작은 **추가형(additive) upsert** — 대상의 기존 데이터를 삭제하지 않으며,
      결정적 id(legacy-*) 로 재실행해도 안전(idempotent)하다.
    - `--replace` 를 명시한 경우에만 대상 todo 테이블을 비우고 새로 적재한다 (확인 문자열 필요).
    - 대상 스키마 드리프트 자동 감지: 실DB 에 `projects.owner_id` / `memos.created_by` 가
      있으면 채우고(현 로컬 DB), 없으면(순정 init_db 스키마) 해당 컬럼 없이 INSERT 한다.
    - 날짜/시간: 생성시간은 변환 없이 옮기고, 레거시 데이터의 마지막 편집시간은 알 수
      없으므로 projects/memos 의 updated_at 을 각각 created_at 과 동일하게 저장한다.
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

    --dry-run  : 소스/대상 메타데이터와 건수를 SELECT 로만 읽어 실행 계획을 리포트.
    --replace  : 대상 todo 테이블(articles/memo_versions/memos/project_members/projects)을
                 비우고 적재. 반드시 --confirm-replace <target database 이름> 을 함께 요구한다.

모드 (--mode):
    legacy (기본) : 소스 = 레거시 스키마(project/task/detail). 위 매핑으로 변환 적재.
    mirror        : 소스 = 이 레포 스키마의 DB (예: 로컬 teddynote). projects/project_members/
                    memos/memo_versions/articles 를 id 그대로 복사(upsert)한다. 컬럼은
                    소스∩대상 교집합만 사용해 스키마 드리프트(owner_id 등)에 안전하다.

사용 예 (prod 를 현재 로컬 데이터로 전체 교체 — "지우고 이 데이터로만"):
    python scripts/migrate_legacy_todo.py --mode mirror \
        --source-host 127.0.0.1 --source-port 33306 --source-user root \
        --source-password '...' --source-database teddynote \
        --target-host <prod-host> --target-port 3306 --target-user <user> \
        --target-password '...' --target-database <prod-db> \
        --replace --confirm-replace <prod-db>
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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

# owner 계정 id 는 반드시 명시한다 (--owner-user-id 또는 TODO_MIGRATION_OWNER_ID env).
# 하드코딩 기본값은 두지 않는다 — 과거 스크립트의 89ef19ed-... 처럼 낡은 값이 조용히 쓰이는 사고 방지.
DEFAULT_OWNER_ID = os.getenv("TODO_MIGRATION_OWNER_ID")
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

    parser.add_argument(
        "--mode",
        choices=("legacy", "mirror"),
        default="legacy",
        help="legacy: 레거시 스키마 변환 적재 / mirror: 현행 스키마 DB 를 그대로 복사",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="대상에는 SELECT만 실행해 적재 계획과 건수를 리포트",
    )
    parser.add_argument(
        "--wipe-daily-tasks",
        action="store_true",
        help="--replace 시 daily_task_types/completions 도 함께 비운다 (오프라인 동기화 부트스트랩)",
    )
    parser.add_argument(
        "--sync-applying",
        action="store_true",
        help="대상 커넥션에 SET @sync_applying = 1 — 적재분이 change_log 에 남지 않게 한다",
    )
    parser.add_argument(
        "--allow-missing-utc",
        action="store_true",
        help="mirror 모드에서 소스의 updated_at_utc 가 비어 있어도 진행",
    )
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
    if args.mode == "legacy" and not args.owner_user_id:
        parser.error(
            "legacy 모드에는 --owner-user-id (또는 TODO_MIGRATION_OWNER_ID env) 가 필요합니다. "
            "마이그레이션된 프로젝트의 소유자/멤버십 계정 id 입니다."
        )
    return args


KST = timezone(timedelta(hours=9))


def to_utc_naive(value):
    """레거시 naive 시각(Asia/Seoul 벽시계) → naive UTC.

    Asia/Seoul 은 1988년 이후 DST 가 없으므로 고정 +09:00 오프셋이 정확하다.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=KST)
        return moment.astimezone(timezone.utc).replace(tzinfo=None)
    return value


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


def table_columns(cursor, table: str) -> list[str]:
    """테이블 컬럼 이름 목록 (정의 순서 유지)."""
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    return [row["Field"] for row in cursor.fetchall()]


def ensure_status_columns(cursor) -> None:
    """구버전 대상 스키마 보강.

    존재하는 컬럼에 ALTER 를 시도하지 않는다. MySQL DDL 은 auto-commit 이므로 호출자가
    트랜잭션 안에 있더라도 불필요한 DDL 시도 자체를 피해야 한다.
    """
    for table, sql in (
        (
            "projects",
            "ALTER TABLE projects ADD COLUMN status INT NOT NULL DEFAULT 0 AFTER icon",
        ),
        (
            "memos",
            "ALTER TABLE memos ADD COLUMN status INT NOT NULL DEFAULT 0 AFTER content",
        ),
    ):
        if "status" in table_columns(cursor, table):
            continue
        try:
            cursor.execute(sql)
        except pymysql.err.OperationalError as exc:
            if exc.args[0] != 1060:
                raise


# 오프라인 동기화 부트스트랩에서 함께 비우는 테이블.
# `articles` 는 projects/memos FK 의 ON DELETE CASCADE 로 자동 삭제되지만 명시해 둔다.
# `daily_task_*` 는 독립 테이블이라 CASCADE 를 타지 않으므로 **명시적으로** 지운다
# (원격에서도 daily task 를 쓰지 않기로 확정 — root plan).
DAILY_TASK_TABLES = ("daily_task_completions", "daily_task_types")


def clear_target(cursor, wipe_daily_tasks: bool = False) -> None:
    for table in ("articles", "memo_versions", "memos", "project_members", "projects"):
        cursor.execute(f"DELETE FROM {table}")
    if wipe_daily_tasks:
        for table in DAILY_TASK_TABLES:
            try:
                cursor.execute(f"DELETE FROM {table}")
            except pymysql.err.ProgrammingError:
                pass  # 테이블이 없는 대상 (1146)


def check_updated_at_utc(cursor) -> dict[str, int]:
    """동기화 대상 테이블에서 updated_at_utc 가 비어 있는 행 수 (0 이면 백필 완료)."""
    missing: dict[str, int] = {}
    for table in ("projects", "memos", "memo_versions", "project_members"):
        try:
            cursor.execute(
                f"SELECT COUNT(*) AS total FROM `{table}` WHERE updated_at_utc IS NULL"
            )
            total = int(cursor.fetchone()["total"])
        except pymysql.err.OperationalError as exc:
            if exc.args[0] == 1054:  # 컬럼 없음 = 동기화 스키마 이전 DB
                total = 0
            else:
                raise
        except pymysql.err.ProgrammingError:
            total = 0
        if total:
            missing[table] = total
    return missing


def target_row_counts(cursor, include_daily_tasks: bool = False) -> dict[str, int]:
    """wipe 대상 건수 리포트용."""
    counts: dict[str, int] = {}
    tables = list(MIRROR_TABLES)
    if include_daily_tasks:
        tables.extend(DAILY_TASK_TABLES)
    for table in tables:
        try:
            cursor.execute(f"SELECT COUNT(*) AS total FROM `{table}`")
            counts[table] = int(cursor.fetchone()["total"])
        except pymysql.err.ProgrammingError:
            counts[table] = 0
    return counts


def upsert(cursor, table: str, columns: list[str], values: list, update_columns: list[str]) -> None:
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(columns)
    update_sql = ", ".join(f"{c} = VALUES({c})" for c in update_columns)
    cursor.execute(
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {update_sql}",
        values,
    )


# mirror 모드에서 복사하는 프로젝트 계열 테이블 — FK 삽입 순서 그대로.
MIRROR_TABLES = ("projects", "project_members", "memos", "memo_versions", "articles")


def migrate_mirror(args: argparse.Namespace) -> int:
    """현행 스키마 DB(예: 로컬 teddynote)를 대상 DB 로 그대로 복사(upsert)한다.

    컬럼은 소스∩대상 교집합만 사용 — owner_id/created_by 같은 드리프트 컬럼이
    한쪽에만 있어도 안전하다. --replace 와 함께 쓰면 대상을 소스와 동일하게 만든다.
    """
    source = connect(
        args.source_host, args.source_port, args.source_user, args.source_password, args.source_database
    )
    rows_by_table: dict[str, tuple[list[str], list[dict]]] = {}
    with source.cursor() as cursor:
        for table in MIRROR_TABLES:
            columns = table_columns(cursor, table)
            cursor.execute(f"SELECT * FROM `{table}`")
            rows_by_table[table] = (columns, cursor.fetchall())
        missing_utc = check_updated_at_utc(cursor)
    source.close()

    if missing_utc and not getattr(args, "allow_missing_utc", False):
        detail = ", ".join(f"{table}={count}" for table, count in missing_utc.items())
        raise SystemExit(
            "소스의 updated_at_utc 가 비어 있는 행이 있습니다 "
            f"({detail}). 먼저 `python scripts/backfill_updated_at_utc.py` 를 실행하거나 "
            "--allow-missing-utc 를 지정하세요. 이 값이 없으면 충돌 판정(LWW)이 성립하지 않습니다."
        )
    print("source:", " ".join(f"{t}={len(rows_by_table[t][1])}" for t in MIRROR_TABLES))

    target = connect(
        args.target_host, args.target_port, args.target_user, args.target_password, args.target_database
    )
    stats = defaultdict(int)
    try:
        with target.cursor() as cursor:
            if args.dry_run:
                before = target_row_counts(
                    cursor, getattr(args, "wipe_daily_tasks", False)
                )
                print(
                    "[dry-run] wipe 대상 건수:",
                    " ".join(f"{table}={count}" for table, count in before.items()),
                )
                for table in MIRROR_TABLES:
                    source_columns, rows = rows_by_table[table]
                    target_columns = table_columns(cursor, table)
                    copied = [column for column in source_columns if column in target_columns]
                    skipped = [
                        column for column in source_columns if column not in target_columns
                    ]
                    print(
                        f"  {table}: 적재 예정 {len(rows)}행, "
                        f"공통 컬럼 {len(copied)}개"
                        + (f", 대상에 없는 컬럼 {skipped}" if skipped else "")
                    )
                    stats[table] = len(rows)
                print("[dry-run] 대상 DB에는 SELECT만 실행했습니다 (DB 무변경).")
                print(
                    "upserted:",
                    " ".join(f"{table}=0 (예정 {stats[table]})" for table in MIRROR_TABLES),
                )
                return 0

            if getattr(args, "sync_applying", False):
                cursor.execute("SET @sync_applying = 1")
            ensure_status_columns(cursor)
            if args.replace:
                before = target_row_counts(cursor, getattr(args, "wipe_daily_tasks", False))
                print("[replace] wipe 대상 건수:", " ".join(f"{t}={n}" for t, n in before.items()))
                clear_target(cursor, getattr(args, "wipe_daily_tasks", False))
                print("[replace] 대상 todo 테이블을 비웠습니다.")
            for table in MIRROR_TABLES:
                source_columns, rows = rows_by_table[table]
                target_columns = table_columns(cursor, table)
                columns = [c for c in source_columns if c in target_columns]
                skipped = [c for c in source_columns if c not in target_columns]
                if skipped:
                    print(f"  {table}: 대상에 없는 컬럼 제외 — {skipped}")
                update_columns = [c for c in columns if c != "id"]
                for row in rows:
                    upsert(cursor, table, columns, [row[c] for c in columns], update_columns)
                    stats[table] += 1

        target.commit()
        print("커밋 완료.")
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()

    print("upserted:", " ".join(f"{t}={stats[t]}" for t in MIRROR_TABLES))
    return 0


def migrate(args: argparse.Namespace) -> int:
    if args.mode == "mirror":
        return migrate_mirror(args)
    return migrate_legacy(args)


def migrate_legacy(args: argparse.Namespace) -> int:
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
            # 스키마 드리프트 감지: 실DB 에만 존재하는 소유자 컬럼은 있을 때만 채운다
            has_project_owner = "owner_id" in table_columns(cursor, "projects")
            has_memo_creator = "created_by" in table_columns(cursor, "memos")
            print(
                f"target schema: projects.owner_id={'있음' if has_project_owner else '없음'}, "
                f"memos.created_by={'있음' if has_memo_creator else '없음'}"
            )

            if args.dry_run:
                before = target_row_counts(
                    cursor, getattr(args, "wipe_daily_tasks", False)
                )
                print(
                    "[dry-run] 대상 현재 건수:",
                    " ".join(f"{table}={count}" for table, count in before.items()),
                )
                print(
                    "[dry-run] 적재 예정: "
                    f"projects={len(projects)} members={len(projects)} "
                    f"memos={len(tasks)} "
                    f"versions={sum(max(len(rows) - 1, 0) for rows in details.values())}"
                )
                print("[dry-run] 대상 DB에는 SELECT만 실행했습니다 (DB 무변경).")
                return 0

            ensure_status_columns(cursor)
            if args.replace:
                clear_target(cursor, getattr(args, "wipe_daily_tasks", False))
                print("[replace] 대상 todo 테이블을 비웠습니다.")

            for project in projects:
                project_uuid = f"legacy-project-{project['project_id']}"
                icon = (project["project_icon"] or DEFAULT_ICON)[:ICON_MAX_LEN]
                columns = [
                    "id", "name", "icon", "status", "is_secret",
                    "created_at", "updated_at", "updated_at_utc",
                ]
                values = [
                    project_uuid,
                    project["project_name"] or UNTITLED,
                    icon,
                    project["project_status"] or 0,
                    False,
                    project["reg_dtm"],
                    project["reg_dtm"],
                    to_utc_naive(project["reg_dtm"]),
                ]
                if has_project_owner:
                    columns.insert(1, "owner_id")
                    values.insert(1, args.owner_user_id)
                upsert(
                    cursor,
                    "projects",
                    columns,
                    values,
                    ["name", "icon", "status", "created_at", "updated_at", "updated_at_utc"],
                )
                stats["projects"] += 1

                upsert(
                    cursor,
                    "project_members",
                    [
                        "id", "project_id", "user_id", "username", "display_name",
                        "email", "role", "invited_at", "updated_at_utc",
                    ],
                    [
                        f"legacy-project-member-{project['project_id']}",
                        project_uuid,
                        args.owner_user_id,
                        args.owner_username,
                        args.owner_display_name,
                        args.owner_email,
                        "owner",
                        project["reg_dtm"],
                        to_utc_naive(project["reg_dtm"]),
                    ],
                    [
                        "role",
                        "username",
                        "display_name",
                        "email",
                        "invited_at",
                        "updated_at_utc",
                    ],
                )
                stats["members"] += 1

            for task in tasks:
                memo_uuid = f"legacy-memo-{task['task_id']}"
                history = details.get(task["task_id"], [])
                latest = history[-1] if history else None
                content = latest["content"] if latest else ""
                created_at = task["reg_dtm"]

                columns = [
                    "id", "project_id", "title", "content", "status",
                    "created_at", "updated_at", "updated_at_utc",
                ]
                values = [
                    memo_uuid,
                    f"legacy-project-{task['project_id']}",
                    (task["task_title"] or UNTITLED)[:255],
                    content,
                    task["task_status"] or 0,
                    created_at,
                    created_at,
                    to_utc_naive(created_at),
                ]
                if has_memo_creator:
                    columns.insert(2, "created_by")
                    values.insert(2, args.owner_user_id)
                upsert(
                    cursor,
                    "memos",
                    columns,
                    values,
                    ["title", "content", "status", "created_at", "updated_at", "updated_at_utc"],
                )
                stats["memos"] += 1

                # 과거 히스토리(최신 제외) → memo_versions v1..N-1
                for version, row in enumerate(history[:-1], start=1):
                    upsert(
                        cursor,
                        "memo_versions",
                        ["id", "memo_id", "content", "version", "created_at", "updated_at_utc"],
                        [
                            f"legacy-version-{task['task_id']}-{version}",
                            memo_uuid,
                            row["content"],
                            version,
                            row["reg_dtm"],
                            to_utc_naive(row["reg_dtm"]),
                        ],
                        ["content", "version", "created_at", "updated_at_utc"],
                    )
                    stats["versions"] += 1

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
