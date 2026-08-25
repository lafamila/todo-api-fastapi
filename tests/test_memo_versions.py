"""메모 버전 목록/상세 직렬화 계약."""

import unittest
from datetime import datetime

from src.routers.memos import _serialize_version, _serialize_version_summary


class MemoVersionSerializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = {
            "id": "v1",
            "memo_id": "m1",
            "content": "large historical body",
            "version": 1,
            "note": None,
            "created_at": datetime(2026, 8, 25, 12, 0, 0),
            "updated_at_utc": datetime(2026, 8, 25, 3, 0, 0),
        }

    def test_list_summary_omits_content(self) -> None:
        summary = _serialize_version_summary(self.row)
        self.assertNotIn("content", summary)
        self.assertEqual(summary["version"], 1)
        self.assertEqual(summary["memoId"], "m1")

    def test_detail_keeps_content(self) -> None:
        detail = _serialize_version(self.row)
        self.assertEqual(detail["content"], "large historical body")


if __name__ == "__main__":
    unittest.main()
