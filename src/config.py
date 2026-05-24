import os


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


AUTH_ISSUER_URL = os.getenv("AUTH_ISSUER_URL", "http://localhost:3032").rstrip("/")
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
    "http://localhost:3031/api/todo/session/callback",
)
TODO_SESSION_COOKIE_NAME = os.getenv("TODO_SESSION_COOKIE_NAME", "teddy_todo_session")
TODO_SESSION_COOKIE_SECURE = _get_bool("TODO_SESSION_COOKIE_SECURE", False)
TODO_SESSION_COOKIE_SAMESITE = os.getenv("TODO_SESSION_COOKIE_SAMESITE", "lax")
TODO_SESSION_COOKIE_DOMAIN = os.getenv("TODO_SESSION_COOKIE_DOMAIN")
TODO_SESSION_MAX_AGE_SECONDS = int(
    os.getenv("TODO_SESSION_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60))
)

AUTH_SERVICE_KEY_ID = os.getenv("AUTH_SERVICE_KEY_ID", "").strip()
AUTH_SERVICE_SECRET = os.getenv("AUTH_SERVICE_SECRET", "").strip()

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip()
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip()
