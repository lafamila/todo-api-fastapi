import pymysql
from dotenv import load_dotenv
from contextlib import contextmanager

try:
    from ..sync_schema import SYNC_TABLE_ORDER, declared_tables
except ImportError:  # pragma: no cover
    from sync_schema import SYNC_TABLE_ORDER, declared_tables

# 접속값은 `config.py` 가 확정한다 — `TODO_MODE` 프리셋(dev→teddynote_dev,
# local→teddynote, prod→teddy-mysql)이 여기까지 닿아야 하기 때문이다.
# `TODO_MODE` 미설정이면 예전과 동일한 `os.getenv` 결과가 그대로 들어온다.
try:
    from ..config import (
        DB_HOST,
        DB_NAME,
        DB_PASSWORD,
        DB_PORT,
        DB_USER,
        TODO_SESSION_DB_PERSISTENCE,
    )
except ImportError:  # pragma: no cover
    from config import (
        DB_HOST,
        DB_NAME,
        DB_PASSWORD,
        DB_PORT,
        DB_USER,
        TODO_SESSION_DB_PERSISTENCE,
    )

load_dotenv()

DB_CONFIG = {
    "host": DB_HOST,
    "port": DB_PORT,
    "user": DB_USER,
    "password": DB_PASSWORD,
    "database": DB_NAME,
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

# 동기화 적용 커넥션이 실행하는 가드. 트리거 본문이 `@sync_applying IS NULL` 을 보고
# change_log 기록을 건너뛴다 → 동기화로 들어온 쓰기가 다시 push 되는 핑퐁을 막는다.
SYNC_APPLYING_SQL = "SET @sync_applying = 1"
SYNC_APPLYING_OFF_SQL = "SET @sync_applying = NULL"


@contextmanager
def get_db_connection(sync_applying: bool = False):
    """DB 커넥션. `sync_applying=True` 면 이 커넥션의 쓰기를 change_log 에서 제외한다."""
    connection = pymysql.connect(**DB_CONFIG)
    try:
        if sync_applying:
            with connection.cursor() as cursor:
                cursor.execute(SYNC_APPLYING_SQL)
        yield connection
        connection.commit()
    except Exception as e:
        connection.rollback()
        raise e
    finally:
        connection.close()


@contextmanager
def change_log_enabled(cursor):
    """`sync_applying` 커넥션 안에서 **의도적으로** change_log 에 남길 쓰기를 감싼다.

    충돌 패자 내용을 `memo_versions` 에 보존하는 쓰기가 여기에 해당한다. 이 행은 상대 노드가
    받아야 하므로(그렇지 않으면 보존 버전이 한쪽에만 존재한다) 로그에서 제외하지 않는다.
    """
    cursor.execute("SELECT @sync_applying AS flag")
    previous = cursor.fetchone()["flag"]
    cursor.execute(SYNC_APPLYING_OFF_SQL)
    try:
        yield cursor
    finally:
        if previous is not None:
            cursor.execute(SYNC_APPLYING_SQL)


def ensure_session_table(cursor) -> None:
    """세션 영속화 테이블 — **노드 로컬** 이다 (동기화 화이트리스트·트리거 대상 아님).

    `TODO_SESSION_DB_PERSISTENCE` 가 켜진 스택(노트북)에서만 생성/사용된다.
    """
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS todo_sessions (
            id_hash CHAR(64) PRIMARY KEY,
            payload LONGTEXT NOT NULL,
            updated_at_utc DATETIME(3) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def init_db():
    """데이터베이스 초기화 - 테이블 생성"""
    connection = pymysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        charset="utf8mb4",
    )

    try:
        with connection.cursor() as cursor:
            # 데이터베이스 생성
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {DB_CONFIG['database']} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.execute(f"USE {DB_CONFIG['database']}")

            # 스키마 보정/백필은 양쪽 노드에서 동일하게 일어나는 결정적 작업이므로
            # change_log 에 남기지 않는다 (남기면 재기동만으로 전 이력이 push 된다).
            cursor.execute(SYNC_APPLYING_SQL)

            # projects 테이블 생성
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id VARCHAR(50) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    icon VARCHAR(10) NOT NULL,
                    status INT NOT NULL DEFAULT 0,
                    is_secret BOOLEAN DEFAULT FALSE,
                    password VARCHAR(255),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    updated_at_utc DATETIME(3) NOT NULL,
                    deleted_at DATETIME(3) NULL DEFAULT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            )

            # memos 테이블 생성
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memos (
                    id VARCHAR(50) PRIMARY KEY,
                    project_id VARCHAR(50) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content LONGTEXT,
                    status INT NOT NULL DEFAULT 0,
                    deleted_at DATETIME(3) NULL DEFAULT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    updated_at_utc DATETIME(3) NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    INDEX idx_project_id (project_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            )

            # memo_versions 테이블 생성 (버전 히스토리)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memo_versions (
                    id VARCHAR(50) PRIMARY KEY,
                    memo_id VARCHAR(50) NOT NULL,
                    content LONGTEXT,
                    version INT NOT NULL,
                    note VARCHAR(255) NULL DEFAULT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at_utc DATETIME(3) NOT NULL,
                    FOREIGN KEY (memo_id) REFERENCES memos(id) ON DELETE CASCADE,
                    INDEX idx_memo_id (memo_id),
                    INDEX idx_memo_version (memo_id, version)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            )

            # articles 테이블 생성 (게시된 메모)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id VARCHAR(50) PRIMARY KEY,
                    memo_id VARCHAR(50) NOT NULL,
                    project_id VARCHAR(50) NOT NULL,
                    author_id VARCHAR(50) NOT NULL,
                    author_slug VARCHAR(100) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content LONGTEXT,
                    published_version INT NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (memo_id) REFERENCES memos(id) ON DELETE CASCADE,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    UNIQUE KEY uk_memo_id (memo_id),
                    INDEX idx_author_slug (author_slug),
                    INDEX idx_author_id (author_id),
                    INDEX idx_project_id (project_id),
                    INDEX idx_published_at (published_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS project_members (
                    id VARCHAR(50) PRIMARY KEY,
                    project_id VARCHAR(50) NOT NULL,
                    user_id VARCHAR(50) NOT NULL,
                    username VARCHAR(255) NULL,
                    display_name VARCHAR(255) NULL,
                    email VARCHAR(255) NULL,
                    role VARCHAR(20) NOT NULL DEFAULT 'viewer',
                    invited_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at_utc DATETIME(3) NOT NULL,
                    deleted_at DATETIME(3) NULL DEFAULT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    UNIQUE KEY uk_project_user (project_id, user_id),
                    INDEX idx_user_id (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            )

            # 기존 memos 테이블에 deleted_at 컬럼 추가 (이미 존재하면 무시)
            try:
                cursor.execute("ALTER TABLE memos ADD COLUMN deleted_at DATETIME NULL DEFAULT NULL AFTER content")
            except Exception:
                pass  # 이미 컬럼이 존재하는 경우

            try:
                cursor.execute("ALTER TABLE projects ADD COLUMN status INT NOT NULL DEFAULT 0 AFTER icon")
            except Exception:
                pass

            try:
                cursor.execute("ALTER TABLE memos ADD COLUMN status INT NOT NULL DEFAULT 0 AFTER content")
            except Exception:
                pass

            try:
                cursor.execute("ALTER TABLE projects ADD COLUMN owner_id VARCHAR(50) NULL AFTER id")
            except Exception:
                pass

            try:
                cursor.execute("ALTER TABLE memos ADD COLUMN created_by VARCHAR(50) NULL AFTER project_id")
            except Exception:
                pass

            for column_sql in (
                "ALTER TABLE project_members ADD COLUMN username VARCHAR(255) NULL AFTER user_id",
                "ALTER TABLE project_members ADD COLUMN display_name VARCHAR(255) NULL AFTER username",
                "ALTER TABLE project_members ADD COLUMN email VARCHAR(255) NULL AFTER display_name",
                "ALTER TABLE articles ADD COLUMN author_id VARCHAR(50) NULL AFTER project_id",
                "ALTER TABLE articles ADD COLUMN author_slug VARCHAR(100) NULL AFTER author_id",
                "ALTER TABLE articles ADD INDEX idx_author_slug (author_slug)",
                "ALTER TABLE articles ADD INDEX idx_author_id (author_id)",
            ):
                try:
                    cursor.execute(column_sql)
                except Exception:
                    pass

            try:
                cursor.execute(
                    """
                    SELECT DISTINCT TABLE_NAME, CONSTRAINT_NAME
                    FROM information_schema.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = %s
                      AND REFERENCED_TABLE_NAME = 'users'
                    """,
                    (DB_CONFIG["database"],),
                )
                for constraint in cursor.fetchall():
                    table_name = constraint[0]
                    constraint_name = constraint[1]
                    safe_table = table_name.replace("`", "``")
                    safe_constraint = constraint_name.replace("`", "``")
                    cursor.execute(
                        f"ALTER TABLE `{safe_table}` DROP FOREIGN KEY `{safe_constraint}`"
                    )
            except Exception:
                pass

            try:
                cursor.execute(
                    "ALTER TABLE project_members MODIFY role VARCHAR(20) NOT NULL DEFAULT 'viewer'"
                )
                cursor.execute(
                    "UPDATE project_members SET role = 'viewer' WHERE role = 'member'"
                )
                cursor.execute(
                    "UPDATE project_members SET role = 'owner' WHERE role = 'admin'"
                )
            except Exception:
                pass

            try:
                cursor.execute("UPDATE articles SET author_id = '' WHERE author_id IS NULL")
                cursor.execute("UPDATE articles SET author_slug = 'legacy' WHERE author_slug IS NULL")
            except Exception:
                pass

            # ============ Daily Task Tracker 테이블 ============

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_task_types (
                    id VARCHAR(50) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    icon VARCHAR(10) DEFAULT '',
                    color VARCHAR(20) DEFAULT '#3994ef',
                    display_order INT DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_task_completions (
                    id VARCHAR(50) PRIMARY KEY,
                    task_type_id VARCHAR(50) NOT NULL,
                    completed_date DATE NOT NULL,
                    total_active_count INT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (task_type_id) REFERENCES daily_task_types(id) ON DELETE CASCADE,
                    UNIQUE KEY uk_task_date (task_type_id, completed_date),
                    INDEX idx_completed_date (completed_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            )

            # ============ 동기화 스키마 (root plan: TODO OFFLINE SYNC) ============

            _init_sync_schema(cursor)

            # 세션 DB 영속화는 local 모드 전용 opt-in — 기본값(dev/prod)에서는 테이블도 만들지 않는다.
            if TODO_SESSION_DB_PERSISTENCE:
                ensure_session_table(cursor)

            connection.commit()
            print("Database initialized successfully")
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 동기화 스키마
# ---------------------------------------------------------------------------

# 기존 naive 컬럼을 UTC 로 백필할 때 쓰는 원본 컬럼.
# Asia/Seoul 은 1988년 이후 DST 가 없으므로 고정 +09:00 오프셋이 정확하다
# (`CONVERT_TZ` 에 이름 대신 오프셋을 주면 tz 테이블 적재가 필요 없다).
_UTC_BACKFILL_SOURCE = {
    "projects": "updated_at",
    "memos": "updated_at",
    "memo_versions": "created_at",
    "project_members": "invited_at",
}

_SYNC_ADDITIVE_DDL = (
    # updated_at_utc — 기존 DB 에는 NULL 허용으로 추가한 뒤 백필하고 NOT NULL 로 조인다
    "ALTER TABLE projects ADD COLUMN updated_at_utc DATETIME(3) NULL AFTER updated_at",
    "ALTER TABLE memos ADD COLUMN updated_at_utc DATETIME(3) NULL AFTER updated_at",
    "ALTER TABLE memo_versions ADD COLUMN updated_at_utc DATETIME(3) NULL AFTER created_at",
    "ALTER TABLE project_members ADD COLUMN updated_at_utc DATETIME(3) NULL AFTER invited_at",
    # tombstone — memos.deleted_at 은 이미 존재한다 (정밀도만 올린다)
    "ALTER TABLE projects ADD COLUMN deleted_at DATETIME(3) NULL DEFAULT NULL AFTER updated_at_utc",
    "ALTER TABLE project_members ADD COLUMN deleted_at DATETIME(3) NULL DEFAULT NULL AFTER updated_at_utc",
    "ALTER TABLE memos MODIFY deleted_at DATETIME(3) NULL DEFAULT NULL",
    # 충돌 보존 버전 표시용
    "ALTER TABLE memo_versions ADD COLUMN note VARCHAR(255) NULL DEFAULT NULL AFTER version",
    # 조회 인덱스
    "ALTER TABLE memos ADD INDEX idx_memos_updated_at_utc (updated_at_utc)",
    "ALTER TABLE projects ADD INDEX idx_projects_updated_at_utc (updated_at_utc)",
)

_IDEMPOTENT_DDL_ERROR_CODES = {
    1060,  # ER_DUP_FIELDNAME
    1061,  # ER_DUP_KEYNAME
}


def _try_ddl(cursor, statement: str) -> None:
    """멱등 DDL — 이미 반영된 경우(중복 컬럼/인덱스)는 무시한다.

    MySQL 8.0 은 `ALTER TABLE ... IF NOT EXISTS` 를 지원하지 않으므로
    (MariaDB 는 지원) 예외 무시가 양쪽에서 동작하는 유일한 방법이다.
    """
    try:
        cursor.execute(statement)
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args else None
        if code not in _IDEMPOTENT_DDL_ERROR_CODES:
            raise


def _init_sync_schema(cursor) -> None:
    """동기화용 컬럼·테이블·트리거를 멱등하게 반영한다."""
    for statement in _SYNC_ADDITIVE_DDL:
        _try_ddl(cursor, statement)

    # naive → UTC 백필. 이미 값이 있는 행은 건드리지 않는다 (재실행 안전).
    for table, source_column in _UTC_BACKFILL_SOURCE.items():
        _try_ddl(
            cursor,
            f"UPDATE {table} SET updated_at_utc = "
            f"CONVERT_TZ({source_column}, '+09:00', '+00:00') WHERE updated_at_utc IS NULL",
        )
        # CONVERT_TZ 가 NULL 을 돌려준 예외적 행(원본이 NULL)까지 막는다
        _try_ddl(
            cursor,
            f"UPDATE {table} SET updated_at_utc = UTC_TIMESTAMP(3) WHERE updated_at_utc IS NULL",
        )
        _try_ddl(cursor, f"ALTER TABLE {table} MODIFY updated_at_utc DATETIME(3) NOT NULL")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS change_log (
            seq BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            table_name VARCHAR(64) NOT NULL,
            row_id VARCHAR(50) NOT NULL,
            op ENUM('insert','update','delete') NOT NULL,
            changed_at_utc DATETIME(3) NOT NULL,
            INDEX idx_change_log_row (table_name, row_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            peer VARCHAR(64) NOT NULL PRIMARY KEY,
            last_pushed_seq BIGINT NOT NULL DEFAULT 0,
            last_pulled_seq BIGINT NOT NULL DEFAULT 0,
            client_epoch VARCHAR(50) NOT NULL,
            last_ok_at DATETIME(3) NULL DEFAULT NULL,
            last_error TEXT NULL,
            paused TINYINT(1) NOT NULL DEFAULT 0,
            updated_at_utc DATETIME(3) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_issues (
            id VARCHAR(50) NOT NULL PRIMARY KEY,
            kind VARCHAR(32) NOT NULL,
            ref_table VARCHAR(64) NULL,
            ref_id VARCHAR(50) NULL,
            peer_ref_id VARCHAR(50) NULL,
            detail JSON NULL,
            detected_at DATETIME(3) NOT NULL,
            resolved_at DATETIME(3) NULL DEFAULT NULL,
            INDEX idx_sync_issues_kind (kind, resolved_at),
            INDEX idx_sync_issues_ref (ref_table, ref_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    # 오프라인 세션용 신원 캐시 (노드 로컬 — 동기화 대상이 아니다)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS local_identity (
            account_id VARCHAR(50) NOT NULL PRIMARY KEY,
            login_id VARCHAR(255) NULL,
            display_name VARCHAR(255) NULL,
            email VARCHAR(255) NULL,
            permission VARCHAR(20) NOT NULL DEFAULT 'visitor',
            issuer VARCHAR(255) NULL,
            verified_at_utc DATETIME(3) NOT NULL,
            updated_at_utc DATETIME(3) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_row_state (
            peer VARCHAR(64) NOT NULL,
            table_name VARCHAR(64) NOT NULL,
            row_id VARCHAR(50) NOT NULL,
            last_seen_updated_at_utc DATETIME(3) NULL,
            last_seen_peer_seq BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (peer, table_name, row_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_retry_queue (
            peer VARCHAR(64) NOT NULL,
            direction VARCHAR(8) NOT NULL,
            seq BIGINT NOT NULL,
            table_name VARCHAR(64) NOT NULL,
            row_id VARCHAR(50) NOT NULL,
            payload JSON NOT NULL,
            payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            reason TEXT NULL,
            attempts INT NOT NULL DEFAULT 0,
            first_seen_at DATETIME(3) NOT NULL,
            last_attempt_at DATETIME(3) NULL,
            PRIMARY KEY (peer, direction, seq)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_receipts (
            account_id VARCHAR(50) NOT NULL,
            client_id VARCHAR(128) NOT NULL,
            source_seq BIGINT NOT NULL,
            payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            outcome JSON NOT NULL,
            created_at DATETIME(3) NOT NULL,
            PRIMARY KEY (account_id, client_id, source_seq)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    # 위 CREATE TABLE 은 기존 테이블에는 컬럼을 더하지 않으므로 additive upgrade가 필요하다.
    _try_ddl(
        cursor,
        "ALTER TABLE sync_state ADD COLUMN client_epoch VARCHAR(50) NULL "
        "AFTER last_pulled_seq",
    )
    cursor.execute(
        "UPDATE sync_state SET client_epoch = UUID() "
        "WHERE client_epoch IS NULL OR client_epoch = ''"
    )
    cursor.execute(
        "ALTER TABLE sync_state MODIFY client_epoch VARCHAR(50) NOT NULL"
    )
    _try_ddl(
        cursor,
        "ALTER TABLE sync_row_state "
        "ADD COLUMN last_seen_peer_seq BIGINT NOT NULL DEFAULT 0 "
        "AFTER last_seen_updated_at_utc",
    )
    cursor.execute(
        "UPDATE sync_row_state SET last_seen_peer_seq = 0 "
        "WHERE last_seen_peer_seq IS NULL"
    )
    cursor.execute(
        "ALTER TABLE sync_row_state "
        "MODIFY last_seen_peer_seq BIGINT NOT NULL DEFAULT 0"
    )
    _try_ddl(
        cursor,
        "ALTER TABLE sync_retry_queue "
        "ADD COLUMN payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL "
        "AFTER payload",
    )
    cursor.execute(
        "UPDATE sync_retry_queue SET payload_hash = SHA2(CAST(payload AS CHAR), 256) "
        "WHERE payload_hash IS NULL"
    )
    cursor.execute(
        "ALTER TABLE sync_retry_queue "
        "MODIFY payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL"
    )
    _ensure_receipts_account_key(cursor)

    _init_change_log_triggers(cursor)
    _validate_sync_schema(cursor)


_REQUIRED_SYNC_COLUMNS = {
    "projects": {"updated_at_utc", "deleted_at"},
    "memos": {"updated_at_utc", "deleted_at"},
    "memo_versions": {"updated_at_utc", "note"},
    "project_members": {"updated_at_utc", "deleted_at"},
    "change_log": {"seq", "table_name", "row_id", "op", "changed_at_utc"},
    "sync_state": {
        "peer",
        "last_pushed_seq",
        "last_pulled_seq",
        "client_epoch",
        "last_ok_at",
        "last_error",
        "paused",
        "updated_at_utc",
    },
    "sync_issues": {
        "id",
        "kind",
        "ref_table",
        "ref_id",
        "peer_ref_id",
        "detail",
        "detected_at",
        "resolved_at",
    },
    "local_identity": {
        "account_id",
        "login_id",
        "display_name",
        "email",
        "permission",
        "issuer",
        "verified_at_utc",
        "updated_at_utc",
    },
    "sync_row_state": {
        "peer",
        "table_name",
        "row_id",
        "last_seen_updated_at_utc",
        "last_seen_peer_seq",
    },
    "sync_retry_queue": {
        "peer",
        "direction",
        "seq",
        "table_name",
        "row_id",
        "payload",
        "payload_hash",
        "reason",
        "attempts",
        "first_seen_at",
        "last_attempt_at",
    },
    "sync_receipts": {
        "account_id",
        "client_id",
        "source_seq",
        "payload_hash",
        "outcome",
        "created_at",
    },
}


def _ensure_receipts_account_key(cursor) -> None:
    """초기 개발 스키마의 account 없는 receipt PK를 안전한 복합키로 승격한다."""
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'sync_receipts'
        """,
        (DB_CONFIG["database"],),
    )
    columns = {
        str(_field(row, "COLUMN_NAME", 0)) for row in cursor.fetchall()
    }
    if "account_id" not in columns:
        # 기존 receipt는 어느 계정인지 증명할 수 없으므로 실제 account와 절대 일치하지 않는
        # 격리 namespace로 보존한다. receipt 자체는 업무 데이터가 아닌 멱등성 메타데이터다.
        cursor.execute(
            "ALTER TABLE sync_receipts "
            "ADD COLUMN account_id VARCHAR(50) NOT NULL DEFAULT '__legacy__' FIRST"
        )
    cursor.execute(
        "ALTER TABLE sync_receipts "
        "MODIFY account_id VARCHAR(50) NOT NULL, "
        "MODIFY payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL"
    )

    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = 'sync_receipts'
          AND INDEX_NAME = 'PRIMARY'
        ORDER BY SEQ_IN_INDEX
        """,
        (DB_CONFIG["database"],),
    )
    primary = [
        str(_field(row, "COLUMN_NAME", 0)) for row in cursor.fetchall()
    ]
    expected = ["account_id", "client_id", "source_seq"]
    if primary != expected:
        cursor.execute(
            "ALTER TABLE sync_receipts DROP PRIMARY KEY, "
            "ADD PRIMARY KEY (account_id, client_id, source_seq)"
        )


def _field(row, name: str, index: int):
    return row[name] if isinstance(row, dict) else row[index]


def _validate_sync_schema(cursor) -> None:
    """동기화가 요구하는 테이블·컬럼·트리거가 실제로 존재하는지 검증한다.

    `init_db()` 가 성공 메시지를 출력한 뒤 업무 API에서 뒤늦게 500이 나는 상태를 막기
    위해, 하나라도 빠졌으면 시작 자체를 실패시킨다.
    """
    cursor.execute(
        """
        SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
        """,
        (DB_CONFIG["database"],),
    )
    actual_columns: dict[str, dict[str, str]] = {}
    for row in cursor.fetchall():
        table = str(_field(row, "TABLE_NAME", 0))
        column = str(_field(row, "COLUMN_NAME", 1))
        nullable = str(_field(row, "IS_NULLABLE", 2))
        actual_columns.setdefault(table, {})[column] = nullable

    required_columns = {
        table: set(columns)
        for table, columns in _REQUIRED_SYNC_COLUMNS.items()
    }
    for table, columns in declared_tables().items():
        required_columns.setdefault(table, set()).update(columns)

    missing: list[str] = []
    for table, required in required_columns.items():
        present = actual_columns.get(table, {})
        if not present:
            missing.append(f"table:{table}")
            continue
        missing.extend(
            f"column:{table}.{column}" for column in sorted(required - set(present))
        )

    required_not_null = {
        (table, "updated_at_utc") for table in _UTC_BACKFILL_SOURCE
    } | {
        ("sync_state", "client_epoch"),
        ("sync_row_state", "last_seen_peer_seq"),
        ("sync_retry_queue", "payload_hash"),
        ("sync_receipts", "account_id"),
        ("sync_receipts", "payload_hash"),
    }
    for table, column in sorted(required_not_null):
        if actual_columns.get(table, {}).get(column) != "NO":
            missing.append(f"not-null:{table}.{column}")

    cursor.execute(
        """
        SELECT TRIGGER_NAME
        FROM information_schema.TRIGGERS
        WHERE TRIGGER_SCHEMA = %s
        """,
        (DB_CONFIG["database"],),
    )
    actual_triggers = {
        str(_field(row, "TRIGGER_NAME", 0)) for row in cursor.fetchall()
    }
    expected_triggers = {
        f"trg_{table}_{_TRIGGER_SUFFIX[op]}_change_log"
        for table in SYNC_TABLE_ORDER
        for op, _timing, _row_alias in _TRIGGER_OPS
    }
    missing.extend(
        f"trigger:{name}" for name in sorted(expected_triggers - actual_triggers)
    )

    if missing:
        raise RuntimeError(
            "sync schema initialization incomplete: " + ", ".join(missing)
        )


_TRIGGER_OPS = (
    ("insert", "AFTER INSERT", "NEW"),
    ("update", "AFTER UPDATE", "NEW"),
    ("delete", "AFTER DELETE", "OLD"),
)
_TRIGGER_SUFFIX = {"insert": "ai", "update": "au", "delete": "ad"}


def _trigger_body(table: str, op: str, row_alias: str) -> str:
    return (
        "BEGIN "
        "IF @sync_applying IS NULL THEN "
        "INSERT INTO change_log (table_name, row_id, op, changed_at_utc) "
        f"VALUES ('{table}', {row_alias}.id, '{op}', UTC_TIMESTAMP(3)); "
        "END IF; "
        "END"
    )


def _normalize_sql(statement: str) -> str:
    return " ".join((statement or "").split())


def _init_change_log_triggers(cursor) -> None:
    """change_log 를 채우는 트리거를 멱등하게 만든다.

    앱의 20여 개 쓰기 지점을 모두 고치는 것보다 누락이 없고, 마이그레이션 스크립트나
    손으로 실행한 SQL 까지 잡힌다. `CREATE TRIGGER IF NOT EXISTS` 는 MySQL 8.0 에 없으므로
    information_schema 로 존재 여부와 **본문 동일성**까지 확인해 필요할 때만 재생성한다.
    """
    # init_db() 는 기본(tuple) 커서를 쓰고 나머지 경로는 DictCursor 를 쓴다 — 양쪽 모두 지원한다
    cursor.execute(
        """
        SELECT TRIGGER_NAME, ACTION_STATEMENT
        FROM information_schema.TRIGGERS
        WHERE TRIGGER_SCHEMA = %s
        """,
        (DB_CONFIG["database"],),
    )
    existing = {}
    for row in cursor.fetchall():
        if isinstance(row, dict):
            existing[row["TRIGGER_NAME"]] = row["ACTION_STATEMENT"]
        else:
            existing[row[0]] = row[1]

    for table in SYNC_TABLE_ORDER:
        for op, timing, row_alias in _TRIGGER_OPS:
            name = f"trg_{table}_{_TRIGGER_SUFFIX[op]}_change_log"
            body = _trigger_body(table, op, row_alias)
            current = existing.get(name)
            if current is not None and _normalize_sql(current) == _normalize_sql(body):
                continue
            if current is not None:
                cursor.execute(f"DROP TRIGGER IF EXISTS `{name}`")
            cursor.execute(
                f"CREATE TRIGGER `{name}` {timing} ON `{table}` FOR EACH ROW {body}"
            )
