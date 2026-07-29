"""DB 를 지우는 테스트용 **스크래치 DB 가드**.

배경: `DB_CONFIG` 는 `src.connectors` 를 **import 하는 순간** `.env` 의 `DB_NAME` 으로
고정된다. 그래서 테스트 모듈이 import 시점에 `os.environ["DB_NAME"]` 만 바꿔도,
다른 테스트 모듈이 먼저 `src.connectors` 를 끌어왔다면(예: `src.services.sync_apply` →
`..connectors`) 이미 늦었다 — 테스트의 DELETE 가 **실사용 DB** 로 날아간다.

그래서 이 모듈은 두 겹으로 막는다:
  1. `DB_CONFIG["database"]` 를 import 순서와 무관하게 **직접** 덮어쓴다.
  2. 이름이 스크래치 규칙에 맞지 않거나 `.env` 의 `DB_NAME` 과 같으면 **실행을 거부**한다.
     조용히 지우는 대신 요란하게 실패하는 쪽이 항상 옳다.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCRATCH_DB = "teddynote_sync_t"
SCRATCH_PREFIXES = ("teddynote_sync_", "todo_sync_")
SCRATCH_SUFFIXES = ("_test", "_scratch")

# 이름이 규칙에 맞아도 절대 지우지 않는 DB (실사용/개발 스택)
FORBIDDEN_DATABASES = frozenset({"teddynote", "teddynote_dev", "todo"})


def _dotenv_database() -> str | None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("DB_NAME="):
            return stripped.split("=", 1)[1].strip() or None
    return None


def _looks_like_scratch(name: str) -> bool:
    return name.startswith(SCRATCH_PREFIXES) or name.endswith(SCRATCH_SUFFIXES)


def assert_scratch_database(name: str) -> str:
    """스크래치 DB 이름인지 검증한다. 아니면 즉시 예외."""
    if not name:
        raise RuntimeError("scratch database name is empty")
    if name in FORBIDDEN_DATABASES:
        raise RuntimeError(
            f"'{name}' 은 실사용/개발 DB 입니다. 테스트가 이 DB 를 지우려고 했습니다 — 중단합니다."
        )
    configured = _dotenv_database()
    if configured and name == configured:
        raise RuntimeError(
            f"'{name}' 은 .env 의 DB_NAME 과 같습니다 (실사용 DB). 테스트가 이 DB 를 "
            "지우려고 했습니다 — 중단합니다."
        )
    if not _looks_like_scratch(name):
        raise RuntimeError(
            f"'{name}' 은 스크래치 DB 이름 규칙에 맞지 않습니다 "
            f"(접두사 {SCRATCH_PREFIXES} 또는 접미사 {SCRATCH_SUFFIXES})."
        )
    return name


def use_scratch_database(name: str | None = None) -> str:
    """이 테스트 프로세스의 DB 를 스크래치로 **확정**한다 (import 순서와 무관).

    반드시 `src.connectors` 를 쓰는 코드보다 먼저 호출해야 하는 것은 아니다 — 이미
    import 되어 있어도 `DB_CONFIG` 를 직접 고쳐서 바로잡는다.
    """
    resolved = assert_scratch_database(
        name or os.environ.get("TODO_SYNC_TEST_DB", DEFAULT_SCRATCH_DB)
    )
    os.environ["DB_NAME"] = resolved

    from src.connectors import DB_CONFIG

    DB_CONFIG["database"] = resolved
    return resolved


TRUNCATE_ORDER = (
    "articles",
    "memo_versions",
    "memos",
    "project_members",
    "projects",
    "change_log",
    "sync_issues",
    "sync_retry_queue",
    "sync_row_state",
    "sync_receipts",
    "sync_state",
    "local_identity",
)


def truncate_scratch_tables(extra: tuple[str, ...] = ()) -> None:
    """스크래치 DB 를 비운다. 매 호출마다 대상 DB 이름을 다시 검증한다."""
    from src.connectors import DB_CONFIG, get_db_connection

    assert_scratch_database(DB_CONFIG["database"])
    # 트리거를 우회해 change_log 까지 깨끗하게 초기화한다
    with get_db_connection(sync_applying=True) as conn:
        with conn.cursor() as cursor:
            for table in (*TRUNCATE_ORDER, *extra):
                cursor.execute(f"DELETE FROM `{table}`")
            cursor.execute("ALTER TABLE change_log AUTO_INCREMENT = 1")


def init_scratch_database() -> bool:
    """스크래치 DB 에 스키마를 반영한다. 접속 실패 시 False (테스트는 스킵)."""
    from src.connectors import DB_CONFIG, init_db

    assert_scratch_database(DB_CONFIG["database"])
    try:
        init_db()
        return True
    except Exception:  # noqa: BLE001 - MySQL 이 없는 환경에서는 스킵한다
        return False
