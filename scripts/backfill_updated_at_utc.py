#!/usr/bin/env python
"""`updated_at_utc` 백필 — 기존 naive 시각을 `Asia/Seoul` 로 해석해 UTC 로 변환한다.

`init_db()` 도 같은 백필을 멱등하게 수행하므로 평소에는 서버 재기동만으로 끝난다.
이 스크립트는 **부트스트랩 전에 명시적으로 확인/실행**하고 싶을 때 쓴다 (건수 리포트 포함).

    venv/bin/python scripts/backfill_updated_at_utc.py --dry-run
    venv/bin/python scripts/backfill_updated_at_utc.py

Asia/Seoul 은 1988년 이후 DST 가 없으므로 고정 `+09:00` 오프셋이 정확하고,
`CONVERT_TZ` 에 이름 대신 오프셋을 주면 MySQL tz 테이블 적재가 필요 없다.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pymysql

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass


# (테이블, 백필 원본 컬럼)
BACKFILL = (
    ("projects", "updated_at"),
    ("memos", "updated_at"),
    ("memo_versions", "created_at"),
    ("project_members", "invited_at"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="updated_at_utc 백필 (Asia/Seoul → UTC)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=os.getenv("DB_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DB_PORT", "3306")))
    parser.add_argument("--user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--password", default=os.getenv("DB_PASSWORD", ""))
    parser.add_argument("--database", default=os.getenv("DB_NAME", "teddynote"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="SELECT로 백필 대상 건수만 리포트 (DB 무변경)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    connection = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )

    try:
        with connection.cursor() as cursor:
            if args.dry_run:
                total = 0
                for table, source_column in BACKFILL:
                    cursor.execute(
                        f"SELECT COUNT(*) AS n FROM `{table}` "
                        "WHERE updated_at_utc IS NULL"
                    )
                    missing = int(cursor.fetchone()["n"])
                    cursor.execute(
                        f"SELECT COUNT(*) AS n FROM `{table}` "
                        f"WHERE updated_at_utc IS NULL AND `{source_column}` IS NULL"
                    )
                    without_source = int(cursor.fetchone()["n"])
                    total += missing
                    print(
                        f"  {table}: {missing}행 백필 예정"
                        + (
                            f" (원본 시각 NULL {without_source}행 — 실제 실행 전 정리 필요)"
                            if without_source
                            else ""
                        )
                    )
                print(
                    f"[dry-run] 총 {total}행 대상 — SELECT만 실행했습니다 (DB 무변경)."
                )
                return 0

            # 백필은 양쪽 노드에서 동일하게 일어나는 결정적 작업이라 change_log 에 남기지 않는다
            cursor.execute("SET @sync_applying = 1")

            total = 0
            for table, source_column in BACKFILL:
                cursor.execute(
                    f"SELECT COUNT(*) AS n FROM `{table}` WHERE updated_at_utc IS NULL"
                )
                missing = int(cursor.fetchone()["n"])
                if not missing:
                    print(f"  {table}: 백필 대상 없음")
                    continue
                cursor.execute(
                    f"UPDATE `{table}` SET updated_at_utc = "
                    f"CONVERT_TZ(`{source_column}`, '+09:00', '+00:00') "
                    "WHERE updated_at_utc IS NULL"
                )
                converted = cursor.rowcount
                cursor.execute(
                    f"UPDATE `{table}` SET updated_at_utc = UTC_TIMESTAMP(3) "
                    "WHERE updated_at_utc IS NULL"
                )
                fallback = cursor.rowcount
                total += converted + fallback
                print(
                    f"  {table}: {converted}행 변환 ({source_column} +09:00 → UTC)"
                    + (f", {fallback}행은 원본이 NULL 이라 현재 시각으로 채움" if fallback else "")
                )

        connection.commit()
        print(f"커밋 완료 — 총 {total}행 백필.")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
