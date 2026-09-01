import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException

from src.models.base import UpdateMemoRequest
from src.routers import memos


class MemoRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memo = {
            "id": "memo-1",
            "project_id": "project-1",
            "created_by": "user-1",
            "title": "Memo",
            "content": "first body",
            "status": 0,
            "created_at": datetime(2026, 9, 1, 9, 0, 0),
            "updated_at": datetime(2026, 9, 1, 9, 0, 0),
            "updated_at_utc": datetime(2026, 9, 1, 0, 0, 0, 123000),
        }

    def test_revision_is_stable_and_exposed_by_memo_serializer(self) -> None:
        first = memos._memo_revision(self.memo)
        second = memos._memo_revision(dict(reversed(list(self.memo.items()))))

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(memos._serialize_memo(self.memo)["revision"], first)

    def test_revision_changes_with_content_or_update_clock(self) -> None:
        original = memos._memo_revision(self.memo)
        changed_content = {**self.memo, "content": "second body"}
        changed_clock = {
            **self.memo,
            "updated_at_utc": datetime(2026, 9, 1, 0, 0, 1, 123000),
        }

        self.assertNotEqual(memos._memo_revision(changed_content), original)
        self.assertNotEqual(memos._memo_revision(changed_clock), original)


class _Cursor:
    def __init__(self, memo: dict) -> None:
        self.memo = memo.copy()
        self.statements: list[tuple[str, tuple | None]] = []
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str, params=None) -> None:
        normalized = " ".join(query.split())
        self.statements.append((normalized, params))
        if normalized.startswith("SELECT * FROM memos"):
            self._result = self.memo.copy()
        elif normalized.startswith("SELECT COALESCE(MAX(version)"):
            self._result = {"max_version": 2}
        elif normalized.startswith("UPDATE memos"):
            self.memo["content"] = params[0]
            self.memo["updated_at"] = params[1]
            self.memo["updated_at_utc"] = params[2]
            self._result = None
        elif normalized.startswith("SELECT id, project_id, created_by"):
            self._result = self.memo.copy()
        else:
            self._result = None

    def fetchone(self):
        return self._result


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class MemoCasUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.memo = {
            "id": "memo-1",
            "project_id": "project-1",
            "created_by": "user-1",
            "title": "Memo",
            "content": "server body",
            "status": 0,
            "created_at": datetime(2026, 9, 1, 9, 0, 0),
            "updated_at": datetime(2026, 9, 1, 9, 0, 0),
            "updated_at_utc": datetime(2026, 9, 1, 0, 0, 0),
        }
        self.cursor = _Cursor(self.memo)

    @contextmanager
    def _connection(self):
        yield _Connection(self.cursor)

    async def _update(self, base_revision: str | None):
        with (
            patch.object(memos, "get_db_connection", self._connection),
            patch.object(memos, "can_write_project", return_value=True),
            patch.object(memos, "_require_memo_write_lease") as require_lease,
            patch.object(
                memos,
                "localnow_naive",
                return_value=datetime(2026, 9, 1, 9, 1, 0),
            ),
            patch.object(
                memos,
                "utcnow_naive",
                return_value=datetime(2026, 9, 1, 0, 1, 0),
            ),
            patch.object(memos, "generate_id", return_value="version-3"),
        ):
            result = await memos.update_memo(
                "memo-1",
                UpdateMemoRequest(
                    content="client body", baseRevision=base_revision
                ),
                user={"id": "user-1"},
                lease_token="valid-token",
            )
        require_lease.assert_called_once_with(
            "memo-1", "user-1", "valid-token"
        )
        return result

    async def test_matching_revision_updates_and_returns_new_revision(self) -> None:
        result = await self._update(memos._memo_revision(self.memo))

        statements = [query for query, _params in self.cursor.statements]
        self.assertIn("FOR UPDATE", statements[0])
        self.assertTrue(
            any(
                query.startswith("INSERT INTO memo_versions")
                for query in statements
            )
        )
        self.assertTrue(
            any(query.startswith("UPDATE memos") for query in statements)
        )
        self.assertEqual(result["content"], "client body")
        self.assertEqual(result["revision"], memos._memo_revision(self.cursor.memo))
        self.assertNotEqual(result["revision"], memos._memo_revision(self.memo))

    async def test_mismatched_revision_returns_current_without_writes(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await self._update("stale-revision")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"], "memo_content_conflict"
        )
        self.assertEqual(
            raised.exception.detail["current"]["revision"],
            memos._memo_revision(self.memo),
        )
        statements = [query for query, _params in self.cursor.statements]
        self.assertFalse(
            any(
                query.startswith("INSERT INTO memo_versions")
                for query in statements
            )
        )
        self.assertFalse(
            any(query.startswith("UPDATE memos") for query in statements)
        )

    async def test_omitted_revision_remains_backward_compatible(self) -> None:
        result = await self._update(None)

        self.assertEqual(result["content"], "client body")


if __name__ == "__main__":
    unittest.main()
