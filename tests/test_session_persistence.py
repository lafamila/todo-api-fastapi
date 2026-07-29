"""세션 DB 영속화 — 로컬 opt-in (기본 off = prod 무변경).

핵심 계약:
    1. 플래그 off(기본)면 init_db 는 테이블조차 만들지 않는다 (prod DB 무변경).
    2. 플래그 on 이면 세션이 프로세스 재시작(새 서비스 인스턴스)을 넘어 복원된다 —
       "한번 로그인해서 무기한" 약속의 마지막 조각.
    3. 로그아웃은 영속 행도 지운다 (메모리에 없던 세션 포함).
"""

import unittest

from tests.scratch_db import init_scratch_database, use_scratch_database

SCRATCH_DB = use_scratch_database()

from src.connectors import DB_CONFIG, ensure_session_table, get_db_connection, init_db  # noqa: E402
from src.services import session_auth as session_auth_module  # noqa: E402
from src.services.session_auth import TodoSession, TodoSessionService  # noqa: E402

REACHABLE = init_scratch_database()


def _table_exists(cursor) -> bool:
    cursor.execute(
        "SELECT COUNT(*) AS c FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = 'todo_sessions'",
        (DB_CONFIG["database"],),
    )
    return cursor.fetchone()["c"] > 0


def _make_session(session_id: str = "sess-1", offline: bool = False) -> TodoSession:
    return TodoSession(
        id=session_id,
        access_token="" if offline else "at",
        refresh_token="" if offline else "rt",
        access_token_expires_at=float("inf") if offline else 12345.0,
        user={"id": "acc-1", "username": "lafamila", "permission": "superadmin"},
        issuer="https://auth.example",
        offline=offline,
    )


@unittest.skipUnless(REACHABLE, f"MySQL scratch DB unavailable ({DB_CONFIG['database']})")
class SessionPersistenceOffTests(unittest.TestCase):
    def test_default_init_db_does_not_create_table(self) -> None:
        # 기본값(off)에서 init_db 를 다시 돌려도 테이블이 생기면 안 된다.
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DROP TABLE IF EXISTS todo_sessions")
        init_db()
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self.assertFalse(_table_exists(cursor))

    def test_store_is_memory_only_when_disabled(self) -> None:
        service = TodoSessionService()
        service._store_session(_make_session())
        # off 상태에서는 조회 경로도 DB 를 보지 않는다 (테이블이 없어도 예외 없음).
        fresh = TodoSessionService()
        self.assertIsNone(fresh._get_session_by_id("sess-1"))


@unittest.skipUnless(REACHABLE, f"MySQL scratch DB unavailable ({DB_CONFIG['database']})")
class SessionPersistenceOnTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original = session_auth_module.TODO_SESSION_DB_PERSISTENCE
        session_auth_module.TODO_SESSION_DB_PERSISTENCE = True
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                ensure_session_table(cursor)
                cursor.execute("DELETE FROM todo_sessions")

    def tearDown(self) -> None:
        session_auth_module.TODO_SESSION_DB_PERSISTENCE = self._original

    def test_session_survives_process_restart(self) -> None:
        TodoSessionService()._store_session(_make_session())

        restarted = TodoSessionService()  # 새 인스턴스 = 프로세스 재시작
        restored = restarted._get_session_by_id("sess-1")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.user["username"], "lafamila")
        self.assertEqual(restored.access_token, "at")
        self.assertFalse(restored.offline)
        # 복원본은 메모리에 안착해 다음 조회는 DB 를 거치지 않는다.
        self.assertIs(restarted._get_session_by_id("sess-1"), restored)

    def test_offline_indefinite_session_round_trips(self) -> None:
        TodoSessionService()._store_session(_make_session("sess-off", offline=True))
        restored = TodoSessionService()._get_session_by_id("sess-off")
        self.assertIsNotNone(restored)
        self.assertTrue(restored.offline)
        self.assertEqual(restored.access_token_expires_at, float("inf"))

    def test_logout_erases_persisted_row_even_after_restart(self) -> None:
        TodoSessionService()._store_session(_make_session())
        # 재시작 직후(메모리에 없음) 로그아웃해도 영속 행이 지워져야 한다.
        restarted = TodoSessionService()
        restarted._delete_session("sess-1")
        self.assertIsNone(TodoSessionService()._get_session_by_id("sess-1"))
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS c FROM todo_sessions")
                self.assertEqual(cursor.fetchone()["c"], 0)

    def test_raw_session_id_is_not_stored(self) -> None:
        TodoSessionService()._store_session(_make_session("very-secret-session-id"))
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id_hash FROM todo_sessions")
                (row,) = cursor.fetchall()
        self.assertNotIn("very-secret-session-id", row["id_hash"])
        self.assertEqual(len(row["id_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
