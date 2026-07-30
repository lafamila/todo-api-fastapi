"""`TODO_MODE` 기동 검증 (preflight).

**`TODO_MODE` 가 설정된 프로세스에서만 동작한다.** 레거시(미설정) 기동은 `config.py` 가
이 모듈을 아예 호출하지 않으므로 동작이 100% 그대로다.

검증하는 것:
  1. `TODO_MODE` 오타 (`dev-local|dev-prod|prod-local|prod-prod` 외)
  2. 모드별 필수 비밀값 누락 — 프리셋이 채울 수 없는 값들
  3. 값 오염 — docker env-file 은 인라인 주석을 지원하지 않아 `KEY=value # 설명` 이
     값 그대로 들어온다 (2026-07-29 prod 장애). 공백+`#` 조합을 전 키에서 잡는다
  4. URL 형식 — scheme/host 가 없는 값
  5. 역할 정합성 — 모드가 정한 sync 역할을 명시 env 가 뒤집지 않았는가

실패 메시지는 한 줄에 "어느 키가 · 왜 · 어떻게 고치는지" 를 담는다.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

# 2×2 = 코드 신선도(dev/prod) × 위치(local=클라이언트 / prod=서버)
VALID_MODES: tuple[str, ...] = ("dev-local", "dev-prod", "prod-local", "prod-prod")


class PreflightError(RuntimeError):
    """기동을 막아야 하는 설정 오류. `config.py` 가 `SystemExit` 로 바꿔 던진다."""


# 모드별 필수 비밀 — 프리셋(코드)이 알 수 없고 알아서도 안 되는 값들.
# 프리셋이 채우는 값(URL·DB 호스트·sync 역할 등)은 여기 없다.
#
# `AUTH_SERVICE_*` 는 **서버 역할에서만 필수**다 — 피어 credential 을 auth 에 되물어
# 검증하는 경로(`sync_auth`)가 이 값 없이는 동작하지 않는다. 클라이언트 역할에서는
# 계정 검색(멤버 초대)에서만 쓰이고 없으면 그 호출만 503 으로 degrade 한다.
# 다만 prod-local 은 계획이 명시적으로 요구하므로 필수로 유지한다.
REQUIRED_SECRETS: dict[str, tuple[str, ...]] = {
    "dev-local": (
        "DB_PASSWORD",
        "TODO_OIDC_CLIENT_SECRET",
        "SYNC_KEY_ID",
        "SYNC_SECRET",
        "SYNC_ACCOUNT_ID",
    ),
    "dev-prod": (
        "DB_PASSWORD",
        "TODO_OIDC_CLIENT_SECRET",
        "AUTH_SERVICE_KEY_ID",
        "AUTH_SERVICE_SECRET",
        "SYNC_ALLOWED_KEY_IDS",
        "SYNC_ACCOUNT_ID",
    ),
    "prod-local": (
        "DB_PASSWORD",
        "TODO_OIDC_CLIENT_SECRET",
        "AUTH_SERVICE_KEY_ID",
        "AUTH_SERVICE_SECRET",
        "SYNC_KEY_ID",
        "SYNC_SECRET",
        "SYNC_ACCOUNT_ID",
    ),
    "prod-prod": (
        "DB_PASSWORD",
        "TODO_OIDC_CLIENT_SECRET",
        "AUTH_SERVICE_KEY_ID",
        "AUTH_SERVICE_SECRET",
        "SYNC_ALLOWED_KEY_IDS",
        "SYNC_ACCOUNT_ID",
    ),
}

# 모드가 정하는 sync 역할 (위치 축). 명시 env 로 뒤집혔으면 기동을 막는다.
# "sync 꺼진 dev" 특수 모드는 없다 — dev 는 항상 페어로 돈다.
EXPECTED_ROLE: dict[str, str] = {
    "dev-local": "client",
    "dev-prod": "server",
    "prod-local": "client",
    "prod-prod": "server",
}

_SECRET_FIX = {
    "DB_PASSWORD": "MySQL 계정 비밀번호를 넣으세요",
    "TODO_OIDC_CLIENT_SECRET": "auth 관리화면의 todo OIDC client secret 을 넣으세요",
    "AUTH_SERVICE_KEY_ID": "이 서비스 자신의 auth service credential keyId 를 넣으세요",
    "AUTH_SERVICE_SECRET": "이 서비스 자신의 auth service credential secret 을 넣으세요",
    "SYNC_KEY_ID": "이 노드가 제시할 sync credential 의 keyId (scope `sync`) 를 넣으세요",
    "SYNC_SECRET": "이 노드가 제시할 sync credential 의 secret 을 넣으세요",
    "SYNC_ACCOUNT_ID": "동기화 대상 auth 계정 id 를 넣으세요",
    "SYNC_ALLOWED_KEY_IDS": "수락할 피어 keyId 를 쉼표로 나열하세요 (server 역할 필수)",
}

# 값 자체가 비밀이라 `#`·공백이 정당할 수 있는 키. 인라인 주석 형태(공백+`#`)만 잡는다.
_SECRET_SHAPED_KEYS = frozenset(
    {
        "DB_PASSWORD",
        "TODO_OIDC_CLIENT_SECRET",
        "AUTH_SERVICE_SECRET",
        "SYNC_SECRET",
        "LIVEKIT_API_SECRET",
    }
)

# 이 접두사를 가진 env 는 전부 오염 검사를 받는다.
_MANAGED_PREFIXES = ("DB_", "AUTH_", "TODO_", "SYNC_", "LIVEKIT_")

# 쉼표 구분 값 — 항목 사이 공백은 허용한다.
_CSV_KEYS = frozenset({"TODO_ALLOWED_ORIGINS", "SYNC_ALLOWED_KEY_IDS"})

# 항상 형식을 확인하는 URL 키
_REQUIRED_URL_KEYS = (
    "AUTH_ISSUER_URL",
    "AUTH_PUBLIC_BASE_URL",
    "AUTH_API_BASE_URL",
    "AUTH_JWKS_URL",
    "TODO_OIDC_REDIRECT_URI",
    "TODO_WEB_BASE_URL",
)

# 비어 있으면 넘어가고, 값이 있으면 형식을 확인하는 URL 키
_OPTIONAL_URL_KEYS = ("SYNC_PEER_URL", "LIVEKIT_URL", "SYNC_VERIFY_URL")

_URL_SCHEMES = {"http", "https", "ws", "wss"}


def validate_mode_name(raw: str | None) -> str:
    """`TODO_MODE` 를 정규화한다. 오타는 즉시 거부, 미설정은 `""`(레거시)."""
    mode = (raw or "").strip().lower()
    if not mode:
        return ""
    if mode not in VALID_MODES:
        raise PreflightError(
            f"TODO_MODE: '{raw}' 는 알 수 없는 모드입니다 — "
            f"{'|'.join(VALID_MODES)} 중 하나를 쓰거나, 레거시 동작을 원하면 키를 지우세요"
        )
    return mode


def _inline_comment_hit(value: str) -> bool:
    """docker env-file 인라인 주석 흔적(공백 뒤 `#`)인가."""
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1].isspace():
            return True
    return False


def _has_internal_whitespace(key: str, value: str) -> bool:
    stripped = value.strip()
    if key in _CSV_KEYS:
        return any(
            any(char.isspace() for char in part.strip())
            for part in stripped.split(",")
        )
    return any(char.isspace() for char in stripped)


def scan_raw_env(env: Mapping[str, str]) -> list[str]:
    """env 값 오염(인라인 주석·공백 혼입)을 찾는다."""
    problems: list[str] = []
    for key in sorted(env):
        if not key.startswith(_MANAGED_PREFIXES):
            continue
        value = env[key]
        if not value:
            continue
        if _inline_comment_hit(value):
            problems.append(
                f"{key}: 값에 ' #' 이 들어 있습니다 — docker env-file 은 인라인 주석을 "
                "지원하지 않습니다. 주석은 값 위 줄로 옮기세요"
            )
            continue
        if key in _SECRET_SHAPED_KEYS:
            continue
        if "#" in value:
            problems.append(
                f"{key}: 값에 '#' 이 들어 있습니다 — 주석이 값으로 섞였는지 확인하세요"
            )
            continue
        if _has_internal_whitespace(key, value):
            problems.append(
                f"{key}: 값 안에 공백이 있습니다 — 따옴표 누락이나 주석 혼입을 확인하세요"
            )
    return problems


def check_required_secrets(mode: str, env: Mapping[str, str]) -> list[str]:
    """모드별 필수 비밀값이 있는지 확인한다."""
    problems: list[str] = []
    for key in REQUIRED_SECRETS.get(mode, ()):
        if (env.get(key) or "").strip():
            continue
        fix = _SECRET_FIX.get(key, "값을 넣으세요")
        problems.append(f"{key}: TODO_MODE={mode} 에 필수인데 비어 있습니다 — {fix}")
    return problems


def _url_problem(key: str, value: str) -> str | None:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return (
            f"{key}: '{value}' 는 URL 이 아닙니다 — "
            "scheme 과 host 를 모두 포함한 절대 URL 로 적으세요 (예: https://example.com)"
        )
    if parsed.scheme not in _URL_SCHEMES:
        return (
            f"{key}: scheme '{parsed.scheme}' 은 지원하지 않습니다 — "
            f"{'/'.join(sorted(_URL_SCHEMES))} 중 하나를 쓰세요"
        )
    return None


def check_urls(resolved: Mapping[str, object]) -> list[str]:
    """프리셋·명시 env 로 확정된 URL 값들의 형식을 확인한다."""
    problems: list[str] = []
    for key in _REQUIRED_URL_KEYS:
        value = str(resolved.get(key) or "").strip()
        if not value:
            problems.append(f"{key}: 비어 있습니다 — 절대 URL 이 필요합니다")
            continue
        problem = _url_problem(key, value)
        if problem:
            problems.append(problem)

    for key in _OPTIONAL_URL_KEYS:
        value = str(resolved.get(key) or "").strip()
        if not value:
            continue
        problem = _url_problem(key, value)
        if problem:
            problems.append(problem)

    origins = resolved.get("TODO_ALLOWED_ORIGINS") or ()
    if isinstance(origins, str):
        origins = [item.strip() for item in origins.split(",") if item.strip()]
    for origin in origins:
        problem = _url_problem("TODO_ALLOWED_ORIGINS", str(origin))
        if problem:
            problems.append(problem)

    return problems


def check_role(mode: str, role: str) -> list[str]:
    """모드가 정한 sync 역할을 명시 env 가 뒤집지 않았는지 확인한다."""
    expected = EXPECTED_ROLE.get(mode)
    if expected is None or role == expected:
        return []
    return [
        f"SYNC_ENABLED/SYNC_PEER_URL: TODO_MODE={mode} 는 sync 역할이 '{expected}' 여야 "
        f"하는데 '{role}' 로 확정되었습니다 — 명시 env 로 프리셋을 덮어썼는지 확인하세요"
    ]


def run_preflight(
    mode: str,
    resolved: Mapping[str, object],
    role: str,
    env: Mapping[str, str] | None = None,
) -> None:
    """모드 기동 검증. 문제가 하나라도 있으면 전부 모아 `PreflightError` 로 던진다."""
    if not mode:
        return
    raw_env = os.environ if env is None else env

    problems: list[str] = []
    problems += scan_raw_env(raw_env)
    problems += check_required_secrets(mode, raw_env)
    problems += check_urls(resolved)
    problems += check_role(mode, role)

    if problems:
        raise PreflightError(format_problems(mode, problems))


def format_problems(mode: str, problems: Iterable[str]) -> str:
    lines = [f"TODO_MODE={mode} 설정 검증 실패 — 다음을 고치고 다시 기동하세요:"]
    lines += [f"  - {problem}" for problem in problems]
    return "\n".join(lines)
