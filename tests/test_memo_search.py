"""Ranked current-memo search and its HTTP contract."""

import asyncio
import json
import threading
import unicodedata
import unittest
from contextlib import contextmanager
from urllib.parse import urlencode
from unittest.mock import patch

from fastapi import FastAPI, HTTPException

from src.auth_utils import get_current_user
from src.routers import memos
from src.services.memo_search import (
    MAX_SNIPPET_CHARS,
    _minimum_covering_span,
    search_memos,
)


def _row(memo_id: str, title: str, content: str) -> dict:
    return {"id": memo_id, "title": title, "content": content}


def _segment_text(segments: list[dict]) -> str:
    return "".join(segment["text"] for segment in segments)


class MemoSearchRankingTests(unittest.TestCase):
    def test_title_weight_beats_same_term_in_body(self) -> None:
        response = search_memos(
            [
                _row("body", "회의 기록", "배포 배포 배포 배포 절차"),
                _row("title", "배포 계획", "간단한 일정"),
            ],
            "배포",
            20,
        )

        self.assertEqual([item["memoId"] for item in response["results"]], ["title", "body"])
        self.assertGreater(response["results"][0]["score"], response["results"][1]["score"])

    def test_query_coverage_and_proximity_affect_ranking(self) -> None:
        response = search_memos(
            [
                _row("partial", "기록", "검색 검색 검색 검색만 반복"),
                _row("far", "기록", "검색 " + ("중간 " * 100) + "알고리즘"),
                _row("near", "기록", "검색 알고리즘을 적용한 문서"),
            ],
            "검색 알고리즘",
            20,
        )

        ids = [item["memoId"] for item in response["results"]]
        self.assertEqual(ids[0], "near")
        self.assertLess(ids.index("partial"), len(ids))
        self.assertGreater(
            next(item["score"] for item in response["results"] if item["memoId"] == "far"),
            next(item["score"] for item in response["results"] if item["memoId"] == "partial"),
        )

    def test_proximity_finds_late_rare_term_after_many_common_hits(self) -> None:
        late_near = ("검색 " * 600) + "검색-알고리즘"
        early_far = "검색 " + ("중간 " * 200) + "알고리즘"

        self.assertLess(
            _minimum_covering_span(late_near, ["검색", "알고리즘"]),
            _minimum_covering_span(early_far, ["검색", "알고리즘"]),
        )

    def test_korean_partial_and_canonical_unicode_match_are_highlighted(self) -> None:
        decomposed = unicodedata.normalize("NFD", "한글 검색문서")
        response = search_memos(
            [
                _row(
                    "korean",
                    decomposed,
                    "통합검색알고리즘에서 중요한 문장입니다.",
                )
            ],
            "한글 알고리즘",
            20,
        )

        result = response["results"][0]
        self.assertEqual(_segment_text(result["titleSegments"]), decomposed)
        self.assertTrue(any(segment["match"] for segment in result["titleSegments"]))
        self.assertIn("알고리즘", _segment_text(result["snippetSegments"]))
        self.assertTrue(any(segment["match"] for segment in result["snippetSegments"]))

    def test_compatibility_sequences_use_same_normalization_for_highlight(self) -> None:
        cases = [
            ("ㄱㅏ나다", "가"),
            ("ﾊﾞナナ", "バ"),
        ]
        for index, (title, query) in enumerate(cases):
            with self.subTest(title=title, query=query):
                response = search_memos([_row(str(index), title, "본문")], query, 20)
                result = response["results"][0]
                self.assertEqual(_segment_text(result["titleSegments"]), title)
                self.assertTrue(any(segment["match"] for segment in result["titleSegments"]))

    def test_snippet_is_bounded_context_with_ellipsis_and_safe_text_segments(self) -> None:
        content = (
            "앞 문맥 "
            + ("가" * 300)
            + " <script>검색값</script> 뒤 문맥 "
            + ("나" * 300)
        )
        response = search_memos(
            [_row("safe", "<b>제목</b>", content)], "script 검색값", 20
        )

        result = response["results"][0]
        snippet = _segment_text(result["snippetSegments"])
        self.assertLessEqual(len(snippet), MAX_SNIPPET_CHARS + 2)
        self.assertEqual(result["snippetSegments"][0], {"text": "…", "match": False})
        self.assertEqual(result["snippetSegments"][-1], {"text": "…", "match": False})
        self.assertIn("<script>", snippet)
        self.assertNotIn("html", result)
        self.assertTrue(
            any(
                segment["match"] and "검색값" in segment["text"]
                for segment in result["snippetSegments"]
            )
        )

    def test_snippet_prefers_sentence_covering_more_query_terms(self) -> None:
        content = (
            ("검색 " * 100)
            + ". 여기에는 검색 알고리즘이 함께 있습니다. 뒤 문장"
        )
        response = search_memos(
            [_row("coverage", "문서", content)], "검색 알고리즘", 20
        )

        snippet = _segment_text(response["results"][0]["snippetSegments"])
        self.assertIn("검색 알고리즘", snippet)
        self.assertIn("함께 있습니다", snippet)

    def test_late_rare_term_anchors_snippet_after_many_common_hits(self) -> None:
        content = ("검색 " * 600) + "가장 중요한 검색 알고리즘 문장입니다."
        response = search_memos(
            [_row("late", "문서", content)], "검색 알고리즘", 20
        )

        snippet = _segment_text(response["results"][0]["snippetSegments"])
        self.assertIn("검색 알고리즘", snippet)
        self.assertIn("가장 중요한", snippet)

    def test_character_length_normalizes_long_unspaced_korean_body(self) -> None:
        response = search_memos(
            [
                _row("long", "기록", ("가" * 5000) + "검색" + ("나" * 5000)),
                _row("short", "기록", "짧은 검색 문장"),
            ],
            "검색",
            20,
        )

        self.assertEqual(response["results"][0]["memoId"], "short")

    def test_empty_lexical_query_returns_no_results(self) -> None:
        self.assertEqual(
            search_memos([_row("memo", "제목", "본문")], " --- ", 20),
            {"query": "---", "total": 0, "results": []},
        )


class _Cursor:
    def __init__(self, rows: list[dict], *, live: bool = True, member: bool = True) -> None:
        self.rows = rows
        self.live = live
        self.member = member
        self.current = None
        self.statements: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str, params=None) -> None:
        normalized = " ".join(query.split())
        self.statements.append((normalized, params))
        if normalized.startswith("SELECT id FROM projects"):
            self.current = {"id": params[0]} if self.live else None
        elif normalized.startswith("SELECT id FROM project_members"):
            self.current = {"id": "member-1"} if self.member else None
        elif normalized.startswith("SELECT id, title, content FROM memos"):
            self.current = list(self.rows)
        else:  # pragma: no cover - catches contract drift explicitly
            raise AssertionError(f"Unexpected query: {normalized}")

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class MemoSearchRouteTests(unittest.IsolatedAsyncioTestCase):
    def _connection(self, cursor: _Cursor):
        @contextmanager
        def connection():
            yield _Connection(cursor)

        return connection

    async def test_route_reads_all_current_rows_before_applying_limit(self) -> None:
        cursor = _Cursor(
            [
                _row("first", "기록", "검색"),
                _row("second", "기록", "검색 검색"),
                _row("best", "검색 제목", "검색"),
            ]
        )
        with patch.object(memos, "get_db_connection", self._connection(cursor)):
            response = await memos.search_project_memos(
                "project-1",
                q="검색",
                limit=1,
                user={"id": "user-1", "permission": "user"},
            )

        self.assertEqual(response["total"], 3)
        self.assertEqual(response["results"][0]["memoId"], "best")
        search_sql = cursor.statements[-1][0]
        self.assertIn("deleted_at IS NULL", search_sql)
        self.assertNotIn("memo_versions", search_sql)
        self.assertNotIn("LIMIT", search_sql.upper())

    async def test_non_member_is_forbidden_before_documents_are_read(self) -> None:
        cursor = _Cursor([_row("hidden", "비밀", "검색")], member=False)
        with (
            patch.object(memos, "get_db_connection", self._connection(cursor)),
            self.assertRaises(HTTPException) as raised,
        ):
            await memos.search_project_memos(
                "project-1",
                q="검색",
                limit=20,
                user={"id": "outsider", "permission": "user"},
            )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertFalse(any("FROM memos" in query for query, _params in cursor.statements))

    async def test_deleted_project_is_not_found_before_membership_lookup(self) -> None:
        cursor = _Cursor([], live=False)
        with (
            patch.object(memos, "get_db_connection", self._connection(cursor)),
            self.assertRaises(HTTPException) as raised,
        ):
            await memos.search_project_memos(
                "deleted",
                q="검색",
                limit=20,
                user={"id": "user-1", "permission": "user"},
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(len(cursor.statements), 1)

    async def test_full_search_capacity_rejects_before_loading_corpus(self) -> None:
        class _FullCapacity:
            @staticmethod
            def locked() -> bool:
                return True

        cursor = _Cursor([_row("memo", "검색", "본문")])
        with (
            patch.object(memos, "get_db_connection", self._connection(cursor)),
            patch.object(memos, "_SEARCH_CAPACITY", _FullCapacity()),
            self.assertRaises(HTTPException) as raised,
        ):
            await memos.search_project_memos(
                "project-1",
                q="검색",
                limit=20,
                user={"id": "owner", "permission": "owner"},
            )

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers, {"Retry-After": "1"})
        self.assertEqual(cursor.statements, [])

    async def test_cancelled_request_holds_capacity_until_worker_finishes(self) -> None:
        capacity = asyncio.BoundedSemaphore(1)
        started = threading.Event()
        finish = threading.Event()

        def blocking_search(*_args):
            started.set()
            finish.wait(timeout=2)
            return {"query": "검색", "total": 0, "results": []}

        with (
            patch.object(memos, "_SEARCH_CAPACITY", capacity),
            patch.object(memos, "_search_project_memos_sync", blocking_search),
        ):
            first = asyncio.create_task(
                memos.search_project_memos(
                    "project-1",
                    q="검색",
                    limit=20,
                    user={"id": "owner", "permission": "owner"},
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            self.assertTrue(capacity.locked())
            with self.assertRaises(HTTPException) as raised:
                await memos.search_project_memos(
                    "project-1",
                    q="검색",
                    limit=20,
                    user={"id": "owner", "permission": "owner"},
                )
            self.assertEqual(raised.exception.status_code, 429)

            finish.set()
            for _ in range(20):
                if not capacity.locked():
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(capacity.locked())


async def _asgi_get(app: FastAPI, path: str, params: dict) -> tuple[int, dict]:
    messages: list[dict] = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": urlencode(params).encode(),
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body)


class MemoSearchHttpContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cursor = _Cursor([_row("memo-1", "검색 문서", "중요한 검색 문장")])
        app = FastAPI()
        app.include_router(memos.router)
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "owner-1",
            "permission": "owner",
        }
        self.connection_patch = patch.object(
            memos, "get_db_connection", self._connection
        )
        self.connection_patch.start()
        self.app = app

    def tearDown(self) -> None:
        self.connection_patch.stop()

    @contextmanager
    def _connection(self):
        yield _Connection(self.cursor)

    async def test_http_route_contract(self) -> None:
        status, body = await _asgi_get(
            self.app,
            "/api/projects/project-1/memos/search",
            {"q": "검색", "limit": 1},
        )

        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"query", "total", "results"})
        self.assertEqual(
            set(body["results"][0]),
            {"memoId", "title", "score", "titleSegments", "snippetSegments"},
        )

    async def test_query_and_limit_validation(self) -> None:
        too_long_status, _body = await _asgi_get(
            self.app,
            "/api/projects/project-1/memos/search",
            {"q": "x" * 201},
        )
        too_many_status, _body = await _asgi_get(
            self.app,
            "/api/projects/project-1/memos/search",
            {"q": "검색", "limit": 51},
        )
        zero_status, _body = await _asgi_get(
            self.app,
            "/api/projects/project-1/memos/search",
            {"q": "검색", "limit": 0},
        )
        too_many_terms_status, too_many_terms_body = await _asgi_get(
            self.app,
            "/api/projects/project-1/memos/search",
            {"q": " ".join(f"단어{index}" for index in range(17))},
        )
        self.assertEqual(too_long_status, 422)
        self.assertEqual(too_many_status, 422)
        self.assertEqual(zero_status, 422)
        self.assertEqual(too_many_terms_status, 422)
        self.assertIn("16개 이하", too_many_terms_body["detail"])


if __name__ == "__main__":
    unittest.main()
