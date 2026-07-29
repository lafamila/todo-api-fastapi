"""동기화 화이트리스트·정규화·시간 유틸 (DB 불필요)."""

import unittest
import unicodedata
from datetime import datetime, timedelta, timezone

from src import sync_schema, timeutil
from src.services.sync_apply import SIDE_CLIENT, SIDE_SERVER, conflict_note


class SyncSchemaTests(unittest.TestCase):
    def test_every_sync_table_declares_clock_and_pk(self) -> None:
        for table, spec in sync_schema.SYNC_TABLES.items():
            self.assertIn(spec["pk"], spec["columns"], table)
            self.assertIn(sync_schema.CLOCK_COLUMN, spec["columns"], table)

    def test_apply_order_matches_declared_tables(self) -> None:
        self.assertEqual(set(sync_schema.SYNC_TABLE_ORDER), set(sync_schema.SYNC_TABLES))
        # projects 가 memos 보다 먼저여야 FK 의존성이 성립한다
        order = list(sync_schema.SYNC_TABLE_ORDER)
        self.assertLess(order.index("projects"), order.index("memos"))
        self.assertLess(order.index("memos"), order.index("memo_versions"))

    def test_legacy_password_is_not_synced(self) -> None:
        self.assertNotIn("password", sync_schema.SYNC_TABLES["projects"]["columns"])

    def test_filter_row_drops_unknown_columns(self) -> None:
        filtered = sync_schema.filter_row(
            "memos", {"id": "m1", "title": "t", "secret_column": "nope"}
        )
        self.assertEqual(set(filtered), {"id", "title"})

    def test_intersect_columns_keeps_only_shared_columns(self) -> None:
        shared = sync_schema.intersect_columns("memos", ["id", "title", "updated_at_utc"])
        self.assertEqual(shared, ("id", "title", "updated_at_utc"))

    def test_intersect_columns_requires_pk_and_clock(self) -> None:
        with self.assertRaises(ValueError):
            sync_schema.intersect_columns("memos", ["title"])

    def test_normalize_name_applies_nfc_and_trim_but_keeps_case(self) -> None:
        decomposed = unicodedata.normalize("NFD", "  한글 ")
        self.assertEqual(sync_schema.normalize_name(decomposed), "한글")
        self.assertNotEqual(
            sync_schema.normalize_name("Todo"), sync_schema.normalize_name("todo")
        )


class TimeUtilTests(unittest.TestCase):
    def test_iso_utc_appends_z_with_millisecond_precision(self) -> None:
        self.assertEqual(
            timeutil.iso_utc(datetime(2026, 7, 29, 5, 0, 0, 123456)),
            "2026-07-29T05:00:00.123Z",
        )

    def test_parse_iso_utc_round_trip(self) -> None:
        parsed = timeutil.parse_iso_utc("2026-07-29T05:00:00.123Z")
        self.assertEqual(parsed, datetime(2026, 7, 29, 5, 0, 0, 123000))
        self.assertIsNone(parsed.tzinfo)

    def test_parse_iso_utc_honours_explicit_offset(self) -> None:
        self.assertEqual(
            timeutil.parse_iso_utc("2026-07-29T14:00:00+09:00"),
            datetime(2026, 7, 29, 5, 0, 0),
        )

    def test_as_utc_naive_assumes_seoul_for_naive_input(self) -> None:
        self.assertEqual(
            timeutil.as_utc_naive(datetime(2026, 7, 29, 14, 0, 0)),
            datetime(2026, 7, 29, 5, 0, 0),
        )

    def test_kst_label_renders_seoul_wall_clock(self) -> None:
        self.assertEqual(timeutil.kst_label(datetime(2026, 7, 29, 5, 2, 0)), "07-29 14:02")

    def test_utcnow_naive_is_close_to_real_utc(self) -> None:
        delta = abs(
            (timeutil.utcnow_naive() - datetime.now(timezone.utc).replace(tzinfo=None))
        )
        self.assertLess(delta, timedelta(seconds=5))


class ConflictNoteTests(unittest.TestCase):
    def test_side_labels_are_role_absolute(self) -> None:
        moment = datetime(2026, 7, 29, 5, 2, 0)
        self.assertEqual(conflict_note(SIDE_CLIENT, moment), "충돌 · 로컬 (07-29 14:02)")
        self.assertEqual(conflict_note(SIDE_SERVER, moment), "충돌 · 원격 (07-29 14:02)")

    def test_note_fits_column_width(self) -> None:
        self.assertLessEqual(len(conflict_note(SIDE_SERVER, datetime.now())), 255)


if __name__ == "__main__":
    unittest.main()
