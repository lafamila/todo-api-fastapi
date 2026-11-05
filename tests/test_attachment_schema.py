from __future__ import annotations

import unittest

from tests.scratch_db import assert_scratch_database, use_scratch_database

from src.connectors import DB_CONFIG, get_db_connection, init_db
from src.services.attachment_schema import (
    ATTACHMENT_COLUMNS,
    ensure_attachment_schema,
)


SCRATCH_DB = assert_scratch_database("todo_sync_media_storage_test")


class AttachmentSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        use_scratch_database(SCRATCH_DB)
        init_db()
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                ensure_attachment_schema(cursor)

    def test_schema_contains_exact_sync_metadata_columns(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COLUMN_NAME
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'memo_attachments'
                    ORDER BY ORDINAL_POSITION
                    """,
                    (DB_CONFIG["database"],),
                )
                actual = tuple(row["COLUMN_NAME"] for row in cursor.fetchall())
        self.assertEqual(actual, ATTACHMENT_COLUMNS)

    def test_schema_enforces_unique_object_keys(self) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'memo_attachments'
                      AND INDEX_NAME = 'uk_memo_attachments_object_key'
                      AND NON_UNIQUE = 0
                    """,
                    (DB_CONFIG["database"],),
                )
                count = int(cursor.fetchone()["count"])
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
