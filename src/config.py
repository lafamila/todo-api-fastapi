import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _get_origin(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid URL for origin parsing: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _get_callback_route_path(redirect_uri: str) -> str:
    path = urlsplit(redirect_uri).path or "/api/todo/session/callback"
    if path == "/api":
        return "/"
    if path.startswith("/api/"):
        return path[len("/api") :]
    raise ValueError("TODO_OIDC_REDIRECT_URI path must start with /api/")


AUTH_ISSUER_URL = os.getenv("AUTH_ISSUER_URL", "http://localhost:3032").rstrip("/")
AUTH_PUBLIC_BASE_URL = os.getenv("AUTH_PUBLIC_BASE_URL", AUTH_ISSUER_URL).rstrip("/")
AUTH_API_BASE_URL = os.getenv("AUTH_API_BASE_URL", AUTH_ISSUER_URL).rstrip("/")
AUTH_JWKS_URL = os.getenv("AUTH_JWKS_URL", f"{AUTH_ISSUER_URL}/oauth/jwks")
AUTH_AUDIENCE = os.getenv("AUTH_AUDIENCE", "service:todo")
AUTH_JWKS_CACHE_SECONDS = int(os.getenv("AUTH_JWKS_CACHE_SECONDS", "300"))

TODO_ALLOWED_ORIGINS = _get_csv(
    "TODO_ALLOWED_ORIGINS",
    "http://localhost:3034,http://127.0.0.1:3034",
)
TODO_OIDC_CLIENT_ID = os.getenv("TODO_OIDC_CLIENT_ID", "todo-web")
TODO_OIDC_CLIENT_SECRET = os.getenv("TODO_OIDC_CLIENT_SECRET")
TODO_OIDC_REDIRECT_URI = os.getenv(
    "TODO_OIDC_REDIRECT_URI",
    "http://localhost:8000/api/todo/session/callback",
)
TODO_OIDC_CALLBACK_ROUTE_PATH = _get_callback_route_path(TODO_OIDC_REDIRECT_URI)
TODO_WEB_BASE_URL = os.getenv(
    "TODO_WEB_BASE_URL",
    "http://localhost:3034",
).rstrip("/")
TODO_SESSION_COOKIE_NAME = os.getenv("TODO_SESSION_COOKIE_NAME", "teddy_todo_session")
TODO_SESSION_COOKIE_SECURE = _get_bool("TODO_SESSION_COOKIE_SECURE", False)
TODO_SESSION_COOKIE_SAMESITE = os.getenv("TODO_SESSION_COOKIE_SAMESITE", "lax")
TODO_SESSION_COOKIE_DOMAIN = os.getenv("TODO_SESSION_COOKIE_DOMAIN")
TODO_SESSION_MAX_AGE_SECONDS = int(
    os.getenv("TODO_SESSION_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60))
)

AUTH_SERVICE_KEY_ID = os.getenv("AUTH_SERVICE_KEY_ID", "").strip()
AUTH_SERVICE_SECRET = os.getenv("AUTH_SERVICE_SECRET", "").strip()

# ---------------------------------------------------------------------------
# 오프라인 동기화 (root plan: TODO OFFLINE SYNC)
#
# 같은 코드베이스가 양쪽에 배포되고 **역할은 env 로 갈린다**:
#   SYNC_ENABLED=false                     → disabled  (개발용 2번째 스택)
#   SYNC_ENABLED=true  + SYNC_PEER_URL 있음 → client    (노트북 — 데몬을 돌린다)
#   SYNC_ENABLED=true  + SYNC_PEER_URL 없음 → server    (NAS  — /api/sync/* 피어 수신)
# ---------------------------------------------------------------------------

SYNC_ROLE_DISABLED = "disabled"
SYNC_ROLE_CLIENT = "client"
SYNC_ROLE_SERVER = "server"

SYNC_ENABLED = _get_bool("SYNC_ENABLED", False)
SYNC_PEER_URL = os.getenv("SYNC_PEER_URL", "").strip().rstrip("/")
SYNC_CLIENT_ID = os.getenv("SYNC_CLIENT_ID", "").strip() or "laptop"

# 노트북이 제시하는 auth 발급 service credential (scope `sync`)
SYNC_KEY_ID = os.getenv("SYNC_KEY_ID", "").strip()
SYNC_SECRET = os.getenv("SYNC_SECRET", "").strip()
# 서버 역할이 auth 검증 이후에도 수락할 peer keyId. 클라이언트의 SYNC_KEY_ID를
# 암묵적으로 재사용하지 않는다 — 같은 todo 서비스의 다른 sync credential과 혼동하면
# 그 credential이 로컬 owner 계정 권한을 얻는다.
SYNC_ALLOWED_KEY_IDS = tuple(_get_csv("SYNC_ALLOWED_KEY_IDS", ""))

SYNC_POLL_SECONDS = int(os.getenv("SYNC_POLL_SECONDS", "60"))
SYNC_PUSH_DEBOUNCE_MS = int(os.getenv("SYNC_PUSH_DEBOUNCE_MS", "1000"))
SYNC_OFFLINE_BACKOFF_SECONDS = int(os.getenv("SYNC_OFFLINE_BACKOFF_SECONDS", "30"))
SYNC_CLOCK_SKEW_LIMIT_SECONDS = int(os.getenv("SYNC_CLOCK_SKEW_LIMIT_SECONDS", "5"))
SYNC_VERIFY_CACHE_SECONDS = int(os.getenv("SYNC_VERIFY_CACHE_SECONDS", "300"))
SYNC_BATCH_LIMIT = int(os.getenv("SYNC_BATCH_LIMIT", "500"))
SYNC_ALLOW_SCHEMA_DRIFT = _get_bool("SYNC_ALLOW_SCHEMA_DRIFT", False)
SYNC_DAEMON_AUTOSTART = _get_bool("SYNC_DAEMON_AUTOSTART", True)
SYNC_LOCK_TTL_SECONDS = int(os.getenv("SYNC_LOCK_TTL_SECONDS", "120"))
SYNC_HTTP_TIMEOUT_SECONDS = int(os.getenv("SYNC_HTTP_TIMEOUT_SECONDS", "15"))

# auth 의 service credential 검증 엔드포인트. 비우면 AUTH_API_BASE_URL 에서 조립한다.
SYNC_VERIFY_URL = (
    os.getenv("AUTH_VERIFY_URL", "").strip().rstrip("/")
    or f"{AUTH_API_BASE_URL}/api/internal/service-credentials/verify"
)
SYNC_REQUIRED_SCOPE = os.getenv("SYNC_REQUIRED_SCOPE", "sync").strip() or "sync"

# credential 은 기계 신원이라 사용자를 증명하지 않는다. 어느 계정 데이터를 다룰지는
# 서비스 소유자 계정으로 고정한다. 비우면 로컬 데이터의 distinct owner id 가
# 정확히 1개일 때 자동 해석하고, 0개/2개 이상이면 handshake 가 실패한다.
SYNC_ACCOUNT_ID = os.getenv("SYNC_ACCOUNT_ID", "").strip()

# 오프라인 로컬 세션 — 원격 auth 가 닿지 않을 때 캐시된 신원으로 무기한 세션을 발급한다.
# 127.0.0.1 바인딩을 전제로 한 선택이므로 노트북 스택에서만 켠다.
TODO_LOCAL_SESSION_ENABLED = _get_bool("TODO_LOCAL_SESSION_ENABLED", False)

# 세션 DB 영속화 — 프로세스 재시작(코드 리로드·재부팅)을 넘어 세션이 유지된다.
# 기본 false: prod(배포본)는 인메모리 유지가 보수적이라 무변경 — 테이블 생성 DDL 도
# 이 플래그 뒤에 있어 prod DB 에는 테이블조차 생기지 않는다. 노트북 스택에서만 켠다.
TODO_SESSION_DB_PERSISTENCE = _get_bool("TODO_SESSION_DB_PERSISTENCE", False)

SYNC_BACKUP_DIR = os.getenv("SYNC_BACKUP_DIR", "../.backups/db")


def sync_role() -> str:
    """이 프로세스의 동기화 역할."""
    if not SYNC_ENABLED:
        return SYNC_ROLE_DISABLED
    return SYNC_ROLE_CLIENT if SYNC_PEER_URL else SYNC_ROLE_SERVER

def feature_flags() -> dict:
    """스택 성격에 따라 웹에서 숨길 prod 전용 표면 (`/api/session/me` 로 내려간다).

    로컬 실사용(sync client) 스택은 단일 사용자 오프라인 복제본이다:
    - articles 게시: articles 는 동기화 제외 테이블 — 로컬에서 게시하면 로컬 DB 에만 남는다
    - 화면공유(LiveKit)·멤버 초대: 멀티유저 기능이라 무의미하다
    dev 스택(disabled)은 기능을 테스트하는 곳이므로 전부 노출한다. prod(server)도 전부 노출.
    """
    local_client = sync_role() == "client"
    return {
        "screenShare": not local_client,
        "articles": not local_client,
        "memberInvite": not local_client,
    }



def serves_sync_peer_api() -> bool:
    """`/api/sync/{handshake,changes,push,locks}` 를 서빙하는가 (서버 역할)."""
    return sync_role() == SYNC_ROLE_SERVER


def runs_sync_daemon() -> bool:
    """동기화 데몬(push/pull/소켓 구독)을 돌리는가 (클라이언트 역할)."""
    return sync_role() == SYNC_ROLE_CLIENT

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip()
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip()
