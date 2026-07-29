"""동기화 페이로드의 **필드 화이트리스트** 와 스키마 버전.

모르는 컬럼은 절대 주고받지 않는다. 동기화 대상 컬럼을 추가/제거하면
`SYNC_TABLES` 와 `SCHEMA_VERSION` 을 **함께** 올려야 한다 (handshake 가 이 값을 비교한다).

동기화 대상이 아닌 것:
    - `articles` / `daily_task_types` / `daily_task_completions` — 원격 전용 (root plan Non-Goals)
    - `projects.password` — 레거시 비밀번호. 제거 대상이라 동기화하지 않는다
    - `change_log` / `sync_state` / `sync_issues` / `local_identity` — 노드 로컬 상태
"""

from __future__ import annotations

import unicodedata

# 동기화 대상 컬럼/테이블을 바꿀 때마다 올린다.
#   1 — (가상) 동기화 도입 이전 스키마
#   2 — updated_at_utc / tombstone / memo_versions.note / change_log 도입
SCHEMA_VERSION = 2

# FK 의존성 순서 — push 적용은 반드시 이 순서를 따른다.
# (오프라인에서 "프로젝트 생성 → 그 안에 메모 생성" 이 흔하다.)
SYNC_TABLE_ORDER: tuple[str, ...] = (
    "projects",
    "memos",
    "memo_versions",
    "project_members",
)

SYNC_TABLES: dict[str, dict] = {
    "projects": {
        "pk": "id",
        "columns": (
            "id",
            "owner_id",
            "name",
            "icon",
            "status",
            "is_secret",
            "created_at",
            "updated_at",
            "updated_at_utc",
            "deleted_at",
        ),
        # (컬럼, 부모 테이블) — 값이 있으면 부모 행이 먼저 존재해야 한다
        "parents": (),
        # 이름 중복 감지 축
        "duplicate_scope": None,
        "duplicate_column": "name",
        "duplicate_kind": "duplicate_project",
    },
    "memos": {
        "pk": "id",
        "columns": (
            "id",
            "project_id",
            "created_by",
            "title",
            "content",
            "status",
            "created_at",
            "updated_at",
            "updated_at_utc",
            "deleted_at",
        ),
        "parents": (("project_id", "projects"),),
        "duplicate_scope": "project_id",
        "duplicate_column": "title",
        "duplicate_kind": "duplicate_memo",
    },
    "memo_versions": {
        "pk": "id",
        "columns": (
            "id",
            "memo_id",
            "content",
            "version",
            "note",
            "created_at",
            "updated_at_utc",
        ),
        "parents": (("memo_id", "memos"),),
        "duplicate_scope": None,
        "duplicate_column": None,
        "duplicate_kind": None,
    },
    "project_members": {
        "pk": "id",
        "columns": (
            "id",
            "project_id",
            "user_id",
            "username",
            "display_name",
            "email",
            "role",
            "invited_at",
            "updated_at_utc",
            "deleted_at",
        ),
        "parents": (("project_id", "projects"),),
        "duplicate_scope": None,
        "duplicate_column": None,
        "duplicate_kind": None,
    },
}

# tombstone 을 가지는 테이블 (soft delete 대상)
SOFT_DELETE_TABLES: frozenset[str] = frozenset(
    name for name, spec in SYNC_TABLES.items() if "deleted_at" in spec["columns"]
)

# 시간 판정 컬럼 — 모든 동기화 대상 테이블이 가진다
CLOCK_COLUMN = "updated_at_utc"

# 와이어 직렬화 규칙.
#   UTC_COLUMNS        — naive UTC 저장 → `...Z` ISO 문자열
#   WALL_CLOCK_COLUMNS — 표시 전용 naive 벽시계(Asia/Seoul) → 오프셋 없는 ISO 문자열로 그대로 전달
UTC_COLUMNS: frozenset[str] = frozenset({"updated_at_utc", "deleted_at"})
WALL_CLOCK_COLUMNS: frozenset[str] = frozenset({"created_at", "updated_at", "invited_at"})

# 메모 본문처럼 "패자를 버전으로 보존해야 하는" 컬럼
CONTENT_COLUMN_BY_TABLE = {"memos": "content"}

SYNC_ISSUE_KINDS: frozenset[str] = frozenset(
    {
        "conflict",
        "duplicate_project",
        "duplicate_memo",
        "identity",
        "schema",
        "clock",
    }
)


def table_columns(table: str) -> tuple[str, ...]:
    return SYNC_TABLES[table]["columns"]


def is_sync_table(table: str) -> bool:
    return table in SYNC_TABLES


def filter_row(table: str, row: dict, allowed: tuple[str, ...] | None = None) -> dict:
    """화이트리스트(+선택적 peer 교집합)에 있는 컬럼만 남긴다."""
    columns = allowed if allowed is not None else table_columns(table)
    return {key: value for key, value in row.items() if key in columns}


def declared_tables() -> dict[str, list[str]]:
    """handshake 로 노출하는 테이블/컬럼 선언 (스키마 드리프트 교집합 계산용)."""
    return {name: list(spec["columns"]) for name, spec in SYNC_TABLES.items()}


def intersect_columns(table: str, peer_columns: list[str] | None) -> tuple[str, ...]:
    """`--allow-schema-drift` 경로 — 양쪽 모두 아는 컬럼만 남긴다.

    PK 와 시간 판정 컬럼은 교집합에서 빠질 수 없다 (빠지면 동기화가 성립하지 않는다).
    """
    own = table_columns(table)
    if not peer_columns:
        return own
    peer = set(peer_columns)
    required = {SYNC_TABLES[table]["pk"], CLOCK_COLUMN}
    missing = required - peer
    if missing:
        raise ValueError(
            f"peer 가 {table} 의 필수 컬럼을 선언하지 않았습니다: {sorted(missing)}"
        )
    return tuple(column for column in own if column in peer)


def normalize_name(value: str | None) -> str:
    """중복 판정 정규화 — `trim` + 유니코드 NFC. **대소문자는 구분 유지**.

    한글 위주 데이터라 대소문자 폴딩의 이득은 거의 없고, 의도적인 대소문자 구분을
    지워버리는 손해가 더 크다 (root plan 확정 결정).
    """
    if value is None:
        return ""
    return unicodedata.normalize("NFC", value.strip())
