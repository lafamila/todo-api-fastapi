"""런타임 설정.

## TODO_MODE (root plan: TODO MODE SIMPLIFICATION)

한 레포가 **2×2 = 4모드**로 구동된다. 축은 직교한다:
**코드 신선도(dev=작업 중 신버전·핫리로드 / prod=검증된 고정 이미지) × 위치(local=클라이언트 / prod=서버)**.
모드만 선언하면 URL·DB·sync 역할·플래그가 **코드 프리셋**에서 파생되고, env 에는 비밀값만 남는다.

| `TODO_MODE` | 정체 | DB | auth | sync |
|---|---|---|---|---|
| `dev-local` | 개발 페어의 클라 (api :20023 / web :30333) | `teddynote_dev_local` @ host:33306 | 로컬 `:3032` | client → dev-prod |
| `dev-prod` | 개발 페어의 서버 (api :20024 / web :30334) | `teddynote_dev_prod` @ host:33306 | 로컬 `:3032` | server |
| `prod-local` | 노트북 실사용 (api :20022 / web :3030) | `teddynote` @ host:33306 | `auth.lafamila.xyz` | client → prod-prod |
| `prod-prod` | NAS 배포판 — 진실의 원천 | `teddynote` @ `teddy-mysql:3306` | 공개 auth / 내부 `auth-api-nest:3032` | server |
| 미설정 | **레거시** — 지금까지의 동작 그대로 | env 그대로 | env 그대로 | env 그대로 |

**dev 는 항상 페어**다 — sync 가 1급 기능이라 개발 환경 자체가 클라↔서버 실토폴로지의
축소판이다. "sync 꺼진 dev" 특수 모드는 없다 (`TODO_MODE` 미설정 레거시와 테스트가 그 역할을 한다).

**우선순위: 명시 env > 모드 프리셋 > 레거시 기본값.**

- `TODO_MODE` 가 없으면 이 파일은 예전과 **완전히 동일하게** 동작한다 (`_env` 의 레거시
  분기가 `os.getenv` 를 그대로 호출한다). 기존 `.env`/compose 조합은 그대로 살아 있다.
- `TODO_MODE` 가 있으면 **빈 문자열 = 미지정**으로 본다. 그래서 compose 가 `SYNC_PEER_URL: ""`
  처럼 비워 둔 키는 프리셋 값으로 채워진다. 빈 값으로 역할을 표현하던 트릭은 더 이상
  필요 없다 — 역할은 모드가 정한다. 명시적으로 다른 값을 쓰고 싶으면 **비어 있지 않은**
  값을 주면 프리셋을 이긴다.
- 비밀값(`*_SECRET`, `*_KEY_ID`, `DB_PASSWORD`, `SYNC_ACCOUNT_ID` 등)은 프리셋에 없다.
  모드별 필수 비밀 목록과 기동 검증은 `src/preflight.py` 가 담당한다.

`DB_*` 도 여기서 확정한다 — `connectors` 가 이 값을 읽는다 (예전에는 `connectors` 가
직접 `os.getenv` 했다).
"""

import json
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

try:
    from .preflight import PreflightError, run_preflight, validate_mode_name
except ImportError:  # pragma: no cover
    from preflight import PreflightError, run_preflight, validate_mode_name

load_dotenv()


try:
    TODO_MODE = validate_mode_name(os.getenv("TODO_MODE"))
except PreflightError as error:  # pragma: no cover - 기동 차단 경로
    raise SystemExit(str(error)) from error


# ---------------------------------------------------------------------------
# 모드 프리셋 — 정답지는 root plan 의 2×2 env 계약이다.
#   prod-local : `../.scripts/todo/compose.yml` 의 todo-api 블록 + `.env.sync-client`
#   prod-prod  : NAS `.env`
#   dev-local  : 기존 todo-api-dev 블록에서 scratch DB·sync client 로 파생
#   dev-prod   : dev-local 의 서버 짝 (신규 포트 :20024 / web :30334)
# `host.docker.internal` 은 컨테이너 스택 기준이다. 호스트에서 직접 uvicorn 을
# 띄우려면 `DB_HOST=127.0.0.1` 처럼 명시 env 로 덮으면 된다.
# ---------------------------------------------------------------------------

_COMMON_PRESET: dict[str, str] = {
    "DB_USER": "root",
    "AUTH_AUDIENCE": "service:todo",
    "AUTH_JWKS_CACHE_SECONDS": "300",
    "TODO_OIDC_CLIENT_ID": "todo-web",
    "TODO_SESSION_COOKIE_SAMESITE": "lax",
    "TODO_SESSION_COOKIE_DOMAIN": "",
    "TODO_SESSION_MAX_AGE_SECONDS": str(7 * 24 * 60 * 60),
    "SYNC_POLL_SECONDS": "60",
    "SYNC_PUSH_DEBOUNCE_MS": "1000",
    "SYNC_OFFLINE_BACKOFF_SECONDS": "30",
    "SYNC_CLOCK_SKEW_LIMIT_SECONDS": "5",
    "SYNC_VERIFY_CACHE_SECONDS": "300",
    "SYNC_BATCH_LIMIT": "500",
    "SYNC_ALLOW_SCHEMA_DRIFT": "false",
    "SYNC_LOCK_TTL_SECONDS": "120",
    "SYNC_HTTP_TIMEOUT_SECONDS": "15",
    "SYNC_REQUIRED_SCOPE": "sync",
    "SYNC_BACKUP_DIR": "../.backups/db",
}

# dev 페어는 로컬 auth(:3032)를 공유한다. 브라우저가 가는 곳은 `localhost`,
# 서버-서버 호출과 JWKS 조회만 `host.docker.internal` 이다.
_DEV_AUTH_PRESET: dict[str, str] = {
    "AUTH_ISSUER_URL": "http://localhost:3032",
    "AUTH_PUBLIC_BASE_URL": "http://localhost:3032",
    "AUTH_API_BASE_URL": "http://host.docker.internal:3032",
    "AUTH_JWKS_URL": "http://host.docker.internal:3032/oauth/jwks",
}

_DEV_LOCAL_PRESET: dict[str, str] = {
    **_DEV_AUTH_PRESET,
    "DB_HOST": "host.docker.internal",
    "DB_PORT": "33306",
    "DB_NAME": "teddynote_dev_local",
    "TODO_ALLOWED_ORIGINS": "http://localhost:30333,http://127.0.0.1:30333",
    "TODO_OIDC_REDIRECT_URI": "http://localhost:20023/api/todo/session/callback",
    "TODO_WEB_BASE_URL": "http://localhost:30333",
    # localhost 는 포트가 달라도 쿠키를 공유한다 — 페어의 두 web 이 서로 로그인을
    # 덮어쓰지 않도록 모드마다 쿠키 이름을 분리한다.
    "TODO_SESSION_COOKIE_NAME": "teddy_todo_dev_local_session",
    "TODO_SESSION_COOKIE_SECURE": "false",
    "LIVEKIT_URL": "ws://host.docker.internal:7880",
    "SYNC_ENABLED": "true",
    # 같은 compose 네트워크 안이라 서비스 DNS 로 직접 부른다.
    "SYNC_PEER_URL": "http://todo-api-dev-prod:8000",
    "SYNC_CLIENT_ID": "laptop",
    "SYNC_ALLOWED_KEY_IDS": "",
    "SYNC_DAEMON_AUTOSTART": "true",
    # prod-local 의 거울 — 오프라인 세션 경로를 dev 에서 개발/테스트한다.
    "TODO_LOCAL_SESSION_ENABLED": "true",
    # 영속화는 prod-local 만 켠다 (고정 빌드 갱신 시 로그인 유지가 목적).
    "TODO_SESSION_DB_PERSISTENCE": "false",
}

_DEV_PROD_PRESET: dict[str, str] = {
    **_DEV_AUTH_PRESET,
    "DB_HOST": "host.docker.internal",
    "DB_PORT": "33306",
    "DB_NAME": "teddynote_dev_prod",
    "TODO_ALLOWED_ORIGINS": "http://localhost:30334,http://127.0.0.1:30334",
    "TODO_OIDC_REDIRECT_URI": "http://localhost:20024/api/todo/session/callback",
    "TODO_WEB_BASE_URL": "http://localhost:30334",
    "TODO_SESSION_COOKIE_NAME": "teddy_todo_dev_prod_session",
    "TODO_SESSION_COOKIE_SECURE": "false",
    "LIVEKIT_URL": "ws://host.docker.internal:7880",
    "SYNC_ENABLED": "true",
    # 서버 역할 — 피어 API 를 서빙하고 데몬은 돌리지 않는다.
    "SYNC_PEER_URL": "",
    "SYNC_CLIENT_ID": "",
    "SYNC_DAEMON_AUTOSTART": "false",
    "TODO_LOCAL_SESSION_ENABLED": "false",
    "TODO_SESSION_DB_PERSISTENCE": "false",
}

_PROD_LOCAL_PRESET: dict[str, str] = {
    "DB_HOST": "host.docker.internal",
    "DB_PORT": "33306",
    "DB_NAME": "teddynote",
    # 실사용 스택은 **prod auth** 를 쓴다 — 신원 캐시(오프라인 세션의 근거)가
    # SYNC_ACCOUNT_ID(prod 계정) + AUTH_ISSUER_URL 로 대조되므로 로컬 auth 로그인은
    # 영원히 불일치한다. 로컬 auth(:3032)는 dev 모드 전용이다.
    "AUTH_ISSUER_URL": "https://auth.lafamila.xyz",
    "AUTH_PUBLIC_BASE_URL": "https://auth.lafamila.xyz",
    "AUTH_API_BASE_URL": "https://auth.lafamila.xyz",
    "AUTH_JWKS_URL": "https://auth.lafamila.xyz/oauth/jwks",
    "TODO_ALLOWED_ORIGINS": "http://localhost:3030,http://127.0.0.1:3030",
    "TODO_OIDC_REDIRECT_URI": "http://localhost:20022/api/todo/session/callback",
    "TODO_WEB_BASE_URL": "http://localhost:3030",
    "TODO_SESSION_COOKIE_NAME": "teddy_todo_session",
    "TODO_SESSION_COOKIE_SECURE": "false",
    "LIVEKIT_URL": "ws://host.docker.internal:7880",
    "SYNC_ENABLED": "true",
    "SYNC_PEER_URL": "https://todo.lafamila.xyz",
    "SYNC_CLIENT_ID": "laptop",
    "SYNC_ALLOWED_KEY_IDS": "",
    "SYNC_DAEMON_AUTOSTART": "true",
    "TODO_LOCAL_SESSION_ENABLED": "true",
    # 리로드·재부팅을 넘어 로그인 유지 (고정 빌드 갱신의 전제)
    "TODO_SESSION_DB_PERSISTENCE": "true",
}

_PROD_PROD_PRESET: dict[str, str] = {
    "DB_HOST": "teddy-mysql",
    "DB_PORT": "3306",
    "DB_NAME": "teddynote",
    # 브라우저가 가는 곳은 공개 URL, 서버-서버 호출과 JWKS 는 같은 docker network 내부 주소.
    "AUTH_ISSUER_URL": "https://auth.lafamila.xyz",
    "AUTH_PUBLIC_BASE_URL": "https://auth.lafamila.xyz",
    "AUTH_API_BASE_URL": "http://auth-api-nest:3032",
    "AUTH_JWKS_URL": "http://auth-api-nest:3032/oauth/jwks",
    "TODO_ALLOWED_ORIGINS": "https://todo.lafamila.xyz",
    "TODO_OIDC_REDIRECT_URI": "https://todo.lafamila.xyz/api/todo/session/callback",
    "TODO_WEB_BASE_URL": "https://todo.lafamila.xyz",
    "TODO_SESSION_COOKIE_NAME": "teddy_todo_session",
    "TODO_SESSION_COOKIE_SECURE": "true",
    "LIVEKIT_URL": "ws://teddy-livekit:7880",
    "SYNC_ENABLED": "true",
    # 서버 역할 — 피어 API 를 서빙하고 데몬은 돌리지 않는다.
    "SYNC_PEER_URL": "",
    "SYNC_CLIENT_ID": "",
    "SYNC_DAEMON_AUTOSTART": "false",
    "TODO_LOCAL_SESSION_ENABLED": "false",
    "TODO_SESSION_DB_PERSISTENCE": "false",
}

MODE_PRESETS: dict[str, dict[str, str]] = {
    "dev-local": {**_COMMON_PRESET, **_DEV_LOCAL_PRESET},
    "dev-prod": {**_COMMON_PRESET, **_DEV_PROD_PRESET},
    "prod-local": {**_COMMON_PRESET, **_PROD_LOCAL_PRESET},
    "prod-prod": {**_COMMON_PRESET, **_PROD_PROD_PRESET},
}

# 위치 축이 `*-local`(클라이언트 측)인 모드가 prod 전용 표면을 숨긴다 (feature_flags 참조).
# dev-local 은 prod-local 의 거울이므로 함께 숨긴다 (2026-07-31 사용자 확정 — 숨김 UX 자체를
# dev 페어에서 그대로 확인하기 위함. 숨겨진 기능의 테스트는 dev-prod web 에서 한다).
MODES_HIDING_PROD_ONLY_SURFACES = ("dev-local", "prod-local")

# 프리셋이 관리하는 키 전체 (루트 compose/todoctl 이 "이 키들은 env 에서 뺄 수 있다"를
# 판단하는 근거).
PRESET_KEYS: tuple[str, ...] = tuple(
    sorted(set().union(*(preset.keys() for preset in MODE_PRESETS.values())))
)


def _preset(name: str) -> str | None:
    if not TODO_MODE:
        return None
    return MODE_PRESETS[TODO_MODE].get(name)


def _env(name: str, default):
    """명시 env > 모드 프리셋 > 레거시 기본값.

    `TODO_MODE` 미설정이면 `os.getenv(name, default)` 와 **완전히 동일**하다.
    모드가 설정되면 빈 문자열은 "미지정"으로 보고 프리셋에 넘긴다.
    """
    raw = os.getenv(name)
    if not TODO_MODE:
        return default if raw is None else raw
    if raw is not None and raw.strip() != "":
        return raw
    preset = _preset(name)
    return default if preset is None else preset


def _get_bool(name: str, default: bool) -> bool:
    raw = _env(name, None)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: str) -> int:
    return int(_env(name, default))


def _get_csv(name: str, default: str) -> list[str]:
    raw = _env(name, default)
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


# ---------------------------------------------------------------------------
# 데이터베이스 — `connectors` 가 이 값으로 DB_CONFIG 를 만든다.
# ---------------------------------------------------------------------------

DB_HOST = _env("DB_HOST", "localhost")
DB_PORT = int(_env("DB_PORT", 3306))
DB_USER = _env("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")  # 비밀 — 프리셋 대상 아님
DB_NAME = _env("DB_NAME", "todo")

AUTH_ISSUER_URL = _env("AUTH_ISSUER_URL", "http://localhost:3032").rstrip("/")
AUTH_PUBLIC_BASE_URL = _env("AUTH_PUBLIC_BASE_URL", AUTH_ISSUER_URL).rstrip("/")
AUTH_API_BASE_URL = _env("AUTH_API_BASE_URL", AUTH_ISSUER_URL).rstrip("/")
AUTH_JWKS_URL = _env("AUTH_JWKS_URL", f"{AUTH_ISSUER_URL}/oauth/jwks")
AUTH_AUDIENCE = _env("AUTH_AUDIENCE", "service:todo")
AUTH_JWKS_CACHE_SECONDS = _get_int("AUTH_JWKS_CACHE_SECONDS", "300")

TODO_ALLOWED_ORIGINS = _get_csv(
    "TODO_ALLOWED_ORIGINS",
    "http://localhost:3034,http://127.0.0.1:3034",
)
TODO_OIDC_CLIENT_ID = _env("TODO_OIDC_CLIENT_ID", "todo-web")
TODO_OIDC_CLIENT_SECRET = os.getenv("TODO_OIDC_CLIENT_SECRET")  # 비밀
TODO_OIDC_REDIRECT_URI = _env(
    "TODO_OIDC_REDIRECT_URI",
    "http://localhost:8000/api/todo/session/callback",
)
TODO_OIDC_CALLBACK_ROUTE_PATH = _get_callback_route_path(TODO_OIDC_REDIRECT_URI)
TODO_WEB_BASE_URL = _env(
    "TODO_WEB_BASE_URL",
    "http://localhost:3034",
).rstrip("/")
TODO_SESSION_COOKIE_NAME = _env("TODO_SESSION_COOKIE_NAME", "teddy_todo_session")
TODO_SESSION_COOKIE_SECURE = _get_bool("TODO_SESSION_COOKIE_SECURE", False)
TODO_SESSION_COOKIE_SAMESITE = _env("TODO_SESSION_COOKIE_SAMESITE", "lax")
TODO_SESSION_COOKIE_DOMAIN = _env("TODO_SESSION_COOKIE_DOMAIN", None)
TODO_SESSION_MAX_AGE_SECONDS = _get_int(
    "TODO_SESSION_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60)
)

AUTH_SERVICE_KEY_ID = os.getenv("AUTH_SERVICE_KEY_ID", "").strip()  # 비밀
AUTH_SERVICE_SECRET = os.getenv("AUTH_SERVICE_SECRET", "").strip()  # 비밀

# ---------------------------------------------------------------------------
# 오프라인 동기화 (root plan: TODO OFFLINE SYNC)
#
# 같은 코드베이스가 양쪽에 배포되고 **역할은 설정이 갈린다**:
#   SYNC_ENABLED=false                     → disabled  (레거시 dev 스택에서만 쓰인다)
#   SYNC_ENABLED=true  + SYNC_PEER_URL 있음 → client    (*-local — 데몬을 돌린다)
#   SYNC_ENABLED=true  + SYNC_PEER_URL 없음 → server    (*-prod  — /api/sync/* 피어 수신)
#
# `TODO_MODE` 가 설정되면 이 조합을 프리셋이 정한다. 빈 `SYNC_PEER_URL` 로 서버 역할을
# 표현하던 트릭은 모드가 대신하므로 더 이상 필요 없다 (명시 env 는 여전히 이긴다).
# ---------------------------------------------------------------------------

SYNC_ROLE_DISABLED = "disabled"
SYNC_ROLE_CLIENT = "client"
SYNC_ROLE_SERVER = "server"

SYNC_ENABLED = _get_bool("SYNC_ENABLED", False)
SYNC_PEER_URL = _env("SYNC_PEER_URL", "").strip().rstrip("/")
SYNC_CLIENT_ID = _env("SYNC_CLIENT_ID", "").strip() or "laptop"

# 노트북이 제시하는 auth 발급 service credential (scope `sync`)
SYNC_KEY_ID = os.getenv("SYNC_KEY_ID", "").strip()  # 비밀
SYNC_SECRET = os.getenv("SYNC_SECRET", "").strip()  # 비밀
# 서버 역할이 auth 검증 이후에도 수락할 peer keyId. 클라이언트의 SYNC_KEY_ID를
# 암묵적으로 재사용하지 않는다 — 같은 todo 서비스의 다른 sync credential과 혼동하면
# 그 credential이 로컬 owner 계정 권한을 얻는다.
SYNC_ALLOWED_KEY_IDS = tuple(_get_csv("SYNC_ALLOWED_KEY_IDS", ""))

SYNC_POLL_SECONDS = _get_int("SYNC_POLL_SECONDS", "60")
SYNC_PUSH_DEBOUNCE_MS = _get_int("SYNC_PUSH_DEBOUNCE_MS", "1000")
SYNC_OFFLINE_BACKOFF_SECONDS = _get_int("SYNC_OFFLINE_BACKOFF_SECONDS", "30")
SYNC_CLOCK_SKEW_LIMIT_SECONDS = _get_int("SYNC_CLOCK_SKEW_LIMIT_SECONDS", "5")
SYNC_VERIFY_CACHE_SECONDS = _get_int("SYNC_VERIFY_CACHE_SECONDS", "300")
SYNC_BATCH_LIMIT = _get_int("SYNC_BATCH_LIMIT", "500")
SYNC_ALLOW_SCHEMA_DRIFT = _get_bool("SYNC_ALLOW_SCHEMA_DRIFT", False)
SYNC_DAEMON_AUTOSTART = _get_bool("SYNC_DAEMON_AUTOSTART", True)
SYNC_LOCK_TTL_SECONDS = _get_int("SYNC_LOCK_TTL_SECONDS", "120")
SYNC_HTTP_TIMEOUT_SECONDS = _get_int("SYNC_HTTP_TIMEOUT_SECONDS", "15")

# auth 의 service credential 검증 엔드포인트. 비우면 AUTH_API_BASE_URL 에서 조립한다.
SYNC_VERIFY_URL = (
    _env("AUTH_VERIFY_URL", "").strip().rstrip("/")
    or f"{AUTH_API_BASE_URL}/api/internal/service-credentials/verify"
)
SYNC_REQUIRED_SCOPE = _env("SYNC_REQUIRED_SCOPE", "sync").strip() or "sync"

# credential 은 기계 신원이라 사용자를 증명하지 않는다. 어느 계정 데이터를 다룰지는
# 서비스 소유자 계정으로 고정한다. 비우면 로컬 데이터의 distinct owner id 가
# 정확히 1개일 때 자동 해석하고, 0개/2개 이상이면 handshake 가 실패한다.
SYNC_ACCOUNT_ID = os.getenv("SYNC_ACCOUNT_ID", "").strip()  # 비밀(계정 한정값)

# 오프라인 로컬 세션 — 원격 auth 가 닿지 않을 때 캐시된 신원으로 무기한 세션을 발급한다.
# 127.0.0.1 바인딩을 전제로 한 선택이므로 클라이언트 모드(`prod-local`, 그 거울인
# `dev-local`)에서만 켠다.
TODO_LOCAL_SESSION_ENABLED = _get_bool("TODO_LOCAL_SESSION_ENABLED", False)

# 세션 DB 영속화 — 프로세스 재시작(코드 리로드·재부팅)을 넘어 세션이 유지된다.
# `prod-local` 만 켠다 (고정 빌드를 갱신해도 로그인이 유지되는 것이 목적). 나머지는
# 인메모리 유지가 보수적이라 off — 테이블 생성 DDL 도 이 플래그 뒤에 있어 DB 에
# 테이블조차 생기지 않는다.
TODO_SESSION_DB_PERSISTENCE = _get_bool("TODO_SESSION_DB_PERSISTENCE", False)

SYNC_BACKUP_DIR = _env("SYNC_BACKUP_DIR", "../.backups/db")


def sync_role() -> str:
    """이 프로세스의 동기화 역할."""
    if not SYNC_ENABLED:
        return SYNC_ROLE_DISABLED
    return SYNC_ROLE_CLIENT if SYNC_PEER_URL else SYNC_ROLE_SERVER

def hides_prod_only_surfaces() -> bool:
    """prod 전용 표면을 숨기는 스택인가.

    숨김 축은 **위치**다: `*-local`(클라이언트 측)은 숨기고 `*-prod`(서버 측)는 노출한다.
    dev-local 은 prod-local 의 거울이라 같은 숨김 UX 를 보여야 하고(2026-07-31 사용자 확정),
    숨겨진 기능(화면공유·게시·멤버초대)의 개발·테스트는 dev-prod web(:30334)에서 한다.
    미설정(레거시)은 예전 그대로 `sync_role() == client` — 위치 축과 사실상 같은 판정이다.
    """
    if TODO_MODE:
        return TODO_MODE in MODES_HIDING_PROD_ONLY_SURFACES
    return sync_role() == SYNC_ROLE_CLIENT


def feature_flags() -> dict:
    """위치별 웹 표면 (`/api/session/me` 로 내려간다).

    노트북 실사용(prod-local) 스택은 단일 사용자 오프라인 복제본이다:
    - articles 게시: articles 는 동기화 제외 테이블 — 로컬에서 게시하면 로컬 DB 에만 남는다
    - 화면공유(LiveKit)·멤버 초대: 멀티유저 기능이라 무의미하다
    - 메모 버전 기록: 개인 로컬 데이터 탐색용이므로 `*-local` 에서만 노출한다
    """
    local_side = hides_prod_only_surfaces()
    return {
        "screenShare": not local_side,
        "articles": not local_side,
        "memberInvite": not local_side,
        "memoVersionHistory": local_side,
    }



def serves_sync_peer_api() -> bool:
    """`/api/sync/{handshake,changes,push,locks}` 를 서빙하는가 (서버 역할)."""
    return sync_role() == SYNC_ROLE_SERVER


def runs_sync_daemon() -> bool:
    """동기화 데몬(push/pull/소켓 구독)을 돌리는가 (클라이언트 역할)."""
    return sync_role() == SYNC_ROLE_CLIENT

LIVEKIT_URL = _env("LIVEKIT_URL", "").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip()  # 비밀
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip()  # 비밀


def settings_snapshot() -> dict:
    """확정된 설정 요약 (비밀은 존재 여부만). `todoctl status`·테스트·진단용."""
    return {
        "TODO_MODE": TODO_MODE or "(legacy)",
        "syncRole": sync_role(),
        "featureFlags": feature_flags(),
        "DB_HOST": DB_HOST,
        "DB_PORT": DB_PORT,
        "DB_USER": DB_USER,
        "DB_NAME": DB_NAME,
        "AUTH_ISSUER_URL": AUTH_ISSUER_URL,
        "AUTH_PUBLIC_BASE_URL": AUTH_PUBLIC_BASE_URL,
        "AUTH_API_BASE_URL": AUTH_API_BASE_URL,
        "AUTH_JWKS_URL": AUTH_JWKS_URL,
        "AUTH_AUDIENCE": AUTH_AUDIENCE,
        "AUTH_JWKS_CACHE_SECONDS": AUTH_JWKS_CACHE_SECONDS,
        "TODO_ALLOWED_ORIGINS": TODO_ALLOWED_ORIGINS,
        "TODO_OIDC_CLIENT_ID": TODO_OIDC_CLIENT_ID,
        "TODO_OIDC_REDIRECT_URI": TODO_OIDC_REDIRECT_URI,
        "TODO_OIDC_CALLBACK_ROUTE_PATH": TODO_OIDC_CALLBACK_ROUTE_PATH,
        "TODO_WEB_BASE_URL": TODO_WEB_BASE_URL,
        "TODO_SESSION_COOKIE_NAME": TODO_SESSION_COOKIE_NAME,
        "TODO_SESSION_COOKIE_SECURE": TODO_SESSION_COOKIE_SECURE,
        "TODO_SESSION_COOKIE_SAMESITE": TODO_SESSION_COOKIE_SAMESITE,
        "TODO_SESSION_COOKIE_DOMAIN": TODO_SESSION_COOKIE_DOMAIN,
        "TODO_SESSION_MAX_AGE_SECONDS": TODO_SESSION_MAX_AGE_SECONDS,
        "TODO_LOCAL_SESSION_ENABLED": TODO_LOCAL_SESSION_ENABLED,
        "TODO_SESSION_DB_PERSISTENCE": TODO_SESSION_DB_PERSISTENCE,
        "LIVEKIT_URL": LIVEKIT_URL,
        "SYNC_ENABLED": SYNC_ENABLED,
        "SYNC_PEER_URL": SYNC_PEER_URL,
        "SYNC_CLIENT_ID": SYNC_CLIENT_ID,
        "SYNC_ALLOWED_KEY_IDS": list(SYNC_ALLOWED_KEY_IDS),
        "SYNC_DAEMON_AUTOSTART": SYNC_DAEMON_AUTOSTART,
        "SYNC_POLL_SECONDS": SYNC_POLL_SECONDS,
        "SYNC_PUSH_DEBOUNCE_MS": SYNC_PUSH_DEBOUNCE_MS,
        "SYNC_OFFLINE_BACKOFF_SECONDS": SYNC_OFFLINE_BACKOFF_SECONDS,
        "SYNC_CLOCK_SKEW_LIMIT_SECONDS": SYNC_CLOCK_SKEW_LIMIT_SECONDS,
        "SYNC_VERIFY_CACHE_SECONDS": SYNC_VERIFY_CACHE_SECONDS,
        "SYNC_BATCH_LIMIT": SYNC_BATCH_LIMIT,
        "SYNC_ALLOW_SCHEMA_DRIFT": SYNC_ALLOW_SCHEMA_DRIFT,
        "SYNC_LOCK_TTL_SECONDS": SYNC_LOCK_TTL_SECONDS,
        "SYNC_HTTP_TIMEOUT_SECONDS": SYNC_HTTP_TIMEOUT_SECONDS,
        "SYNC_VERIFY_URL": SYNC_VERIFY_URL,
        "SYNC_REQUIRED_SCOPE": SYNC_REQUIRED_SCOPE,
        "SYNC_BACKUP_DIR": SYNC_BACKUP_DIR,
        "secretsPresent": {
            "DB_PASSWORD": bool(DB_PASSWORD),
            "TODO_OIDC_CLIENT_SECRET": bool(TODO_OIDC_CLIENT_SECRET),
            "AUTH_SERVICE_KEY_ID": bool(AUTH_SERVICE_KEY_ID),
            "AUTH_SERVICE_SECRET": bool(AUTH_SERVICE_SECRET),
            "SYNC_KEY_ID": bool(SYNC_KEY_ID),
            "SYNC_SECRET": bool(SYNC_SECRET),
            "SYNC_ACCOUNT_ID": bool(SYNC_ACCOUNT_ID),
            "LIVEKIT_API_KEY": bool(LIVEKIT_API_KEY),
            "LIVEKIT_API_SECRET": bool(LIVEKIT_API_SECRET),
        },
    }


# 모드가 선언된 기동만 검증한다 — 레거시(`TODO_MODE` 미설정)는 건드리지 않는다.
try:
    run_preflight(TODO_MODE, settings_snapshot(), sync_role())
except PreflightError as error:  # pragma: no cover - 기동 차단 경로
    raise SystemExit(str(error)) from error


if __name__ == "__main__":  # pragma: no cover - 진단용
    print(json.dumps(settings_snapshot(), indent=2, ensure_ascii=False))
