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

            connection.commit()
            print("Database initialized successfully")
    finally:
        connection.close()
