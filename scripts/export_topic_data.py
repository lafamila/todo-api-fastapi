#!/usr/bin/env python3
"""Export legacy topic tables from the todo DB.

This script is intentionally read-only. It writes a JSON artifact that
`topic-api-fastapi` can import after its own schema exists.
"""

import argparse
import base64
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from connectors import DB_CONFIG, get_db_connection  # noqa: E402


TOPIC_TABLES = [
    "topic_sources",
    "topics",
    "topic_references",
    "topic_hashtags",
    "topic_questions",
    "topic_answers",
    "topic_blog_posts",
    "topic_insight_exchanges",
]


def to_json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "__type": "base64",
            "value": base64.b64encode(bytes(value)).decode("ascii"),
        }
    return value


def table_exists(cursor, table_name):
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (DB_CONFIG["database"], table_name),
    )
    return cursor.fetchone()["count"] > 0


def export_table(cursor, table_name):
    if not table_exists(cursor, table_name):
        return []
    safe_table = table_name.replace("`", "``")
    cursor.execute(f"SELECT * FROM `{safe_table}`")
    rows = cursor.fetchall()
    return [
        {key: to_json_value(value) for key, value in row.items()}
        for row in rows
    ]


def main():
    parser = argparse.ArgumentParser(description="Export legacy todo topic tables as JSON.")
    parser.add_argument(
        "--output",
        default="migration_artifacts/topic-data-export.json",
        help="Output JSON path relative to repo root unless absolute.",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / output_path

    payload = {
        "source": "todo-api-fastapi",
        "database": DB_CONFIG["database"],
        "exportedAt": datetime.utcnow().isoformat() + "Z",
        "tables": {},
    }

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            for table_name in TOPIC_TABLES:
                payload["tables"][table_name] = export_table(cursor, table_name)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    counts = {
        table_name: len(rows)
        for table_name, rows in payload["tables"].items()
    }
    print(json.dumps({"output": str(output_path), "counts": counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
