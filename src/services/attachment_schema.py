"""Additive MySQL schema for private memo attachment metadata."""

from __future__ import annotations


ATTACHMENT_TABLE = "memo_attachments"
ATTACHMENT_COLUMNS = (
    "id",
    "memo_id",
    "origin_memo_id",
    "created_by",
    "file_name",
    "content_type",
    "byte_size",
    "is_image",
    "sha256",
    "object_key",
    "status",
    "created_at",
    "updated_at_utc",
    "deleted_at",
)


def ensure_attachment_schema(cursor) -> None:
    """Create the metadata table without accessing object storage.

    This helper is safe to call during every API startup. Attachment storage
    may remain disabled; existing Todo functionality must still boot normally.
    """
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS memo_attachments (
            id VARCHAR(50) PRIMARY KEY,
            memo_id VARCHAR(50) NOT NULL,
            origin_memo_id VARCHAR(50) NOT NULL,
            created_by VARCHAR(50) NOT NULL,
            file_name VARCHAR(255) NOT NULL,
            content_type VARCHAR(255) NOT NULL,
            byte_size BIGINT UNSIGNED NOT NULL,
            is_image BOOLEAN NOT NULL DEFAULT FALSE,
            sha256 CHAR(64) NOT NULL,
            object_key VARCHAR(512) NOT NULL,
            status VARCHAR(20) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at_utc DATETIME(3) NOT NULL,
            deleted_at DATETIME(3) NULL DEFAULT NULL,
            CONSTRAINT fk_memo_attachments_memo
                FOREIGN KEY (memo_id) REFERENCES memos(id) ON DELETE CASCADE,
            UNIQUE KEY uk_memo_attachments_object_key (object_key),
            INDEX idx_memo_attachments_memo_status (memo_id, status),
            INDEX idx_memo_attachments_creator (created_by),
            INDEX idx_memo_attachments_updated_at_utc (updated_at_utc)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        SELECT IS_NULLABLE AS is_nullable
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'memo_attachments'
          AND COLUMN_NAME = 'origin_memo_id'
        """
    )
    column_result = cursor.fetchone()
    if column_result is None:
        cursor.execute(
            "ALTER TABLE memo_attachments "
            "ADD COLUMN origin_memo_id VARCHAR(50) NULL AFTER memo_id"
        )
        is_nullable = "YES"
    else:
        is_nullable = (
            column_result["is_nullable"]
            if isinstance(column_result, dict)
            else column_result[0]
        )
    if is_nullable == "YES":
        cursor.execute(
            "UPDATE memo_attachments SET origin_memo_id = memo_id "
            "WHERE origin_memo_id IS NULL"
        )
        cursor.execute(
            "ALTER TABLE memo_attachments MODIFY origin_memo_id VARCHAR(50) NOT NULL"
        )
