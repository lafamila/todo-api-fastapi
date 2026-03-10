import pymysql
import os
from dotenv import load_dotenv
from contextlib import contextmanager

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "todo"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}


@contextmanager
def get_db_connection():
    connection = pymysql.connect(**DB_CONFIG)
    try:
        yield connection
        connection.commit()
    except Exception as e:
        connection.rollback()
        raise e
    finally:
        connection.close()


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

            # projects 테이블 생성
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id VARCHAR(50) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    icon VARCHAR(10) NOT NULL,
                    is_secret BOOLEAN DEFAULT FALSE,
                    password VARCHAR(255),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
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
                    deleted_at DATETIME NULL DEFAULT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
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
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
                    title VARCHAR(255) NOT NULL,
                    content LONGTEXT,
                    published_version INT NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (memo_id) REFERENCES memos(id) ON DELETE CASCADE,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    UNIQUE KEY uk_memo_id (memo_id),
                    INDEX idx_project_id (project_id),
                    INDEX idx_published_at (published_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id VARCHAR(50) PRIMARY KEY,
                    username VARCHAR(100) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    display_name VARCHAR(255) NOT NULL,
                    is_admin BOOLEAN DEFAULT FALSE,
                    auth_provider VARCHAR(20) DEFAULT 'local',
                    auth_provider_id VARCHAR(255) NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS project_members (
                    id VARCHAR(50) PRIMARY KEY,
                    project_id VARCHAR(50) NOT NULL,
                    user_id VARCHAR(50) NOT NULL,
                    role VARCHAR(20) NOT NULL DEFAULT 'member',
                    invited_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
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
                cursor.execute("ALTER TABLE projects ADD COLUMN owner_id VARCHAR(50) NULL AFTER id")
            except Exception:
                pass

            try:
                cursor.execute("ALTER TABLE memos ADD COLUMN created_by VARCHAR(50) NULL AFTER project_id")
            except Exception:
                pass

            connection.commit()
            print("Database initialized successfully")
    finally:
        connection.close()
