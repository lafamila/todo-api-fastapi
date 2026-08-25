"""`TODO_MODE` 2×2 프리셋 · 우선순위 · feature flags 축 · preflight 검증.

`src/config.py` 는 **import 시점에** 값을 확정한다. 그래서 모드별 파생값은
`python -m src.config`(JSON 덤프)를 별도 프로세스로 띄워 확인한다 — 테스트 프로세스의
이미 로드된 config 를 흔들지 않고, 실제 기동과 같은 경로를 그대로 밟는다.

기대값은 **정답지를 그대로 적는다** (root plan `TODO_MODE_SIMPLIFICATION_PLAN.md` 의
2×2 env 계약, `../.scripts/todo/compose.yml` 의 todo-api 블록). config 의 프리셋 dict 에서
역산하면 "코드와 코드를 비교"하는 무의미한 테스트가 된다.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import preflight  # noqa: E402
from src.preflight import PreflightError  # noqa: E402

# 이 프로세스가 아니라 **자식 프로세스**에서만 config 를 import 한다.
CONFIG_ENTRY = ("-m", "src.config")

ALL_MODES = ("dev-local", "dev-prod", "prod-local", "prod-prod")

# 정수로 파싱되는 키 — 빈 문자열은 레거시 경로에서 ValueError 다 (예전과 동일).
INT_KEYS = (
    "DB_PORT",
    "AUTH_JWKS_CACHE_SECONDS",
    "TODO_SESSION_MAX_AGE_SECONDS",
    "SYNC_POLL_SECONDS",
    "SYNC_PUSH_DEBOUNCE_MS",
    "SYNC_OFFLINE_BACKOFF_SECONDS",
    "SYNC_CLOCK_SKEW_LIMIT_SECONDS",
    "SYNC_VERIFY_CACHE_SECONDS",
    "SYNC_BATCH_LIMIT",
    "SYNC_LOCK_TTL_SECONDS",
    "SYNC_HTTP_TIMEOUT_SECONDS",
)

DUMMY_SECRETS = {
    "DB_PASSWORD": "dummy-db-password",
    "TODO_OIDC_CLIENT_SECRET": "dummy-oidc-secret",
    "AUTH_SERVICE_KEY_ID": "dummy-service-key",
    "AUTH_SERVICE_SECRET": "dummy-service-secret",
    "SYNC_KEY_ID": "dummy-sync-key",
    "SYNC_SECRET": "dummy-sync-secret",
    "SYNC_ACCOUNT_ID": "dummy-account-id",
    "SYNC_ALLOWED_KEY_IDS": "laptop-key",
}

LOCAL_FEATURES = {
    "screenShare": False,
    "articles": False,
    "memberInvite": False,
    "memoVersionHistory": True,
}
PROD_FEATURES = {
    "screenShare": True,
    "articles": True,
    "memberInvite": True,
    "memoVersionHistory": False,
}


def _managed_keys() -> set[str]:
    """자식 프로세스에서 비워야 하는 키 전체.

    `load_dotenv()` 는 **없는 키만** 채우므로, 레포 `.env`(개발자 로컬 값)가 프리셋을
    이기지 않게 하려면 모든 관리 키를 빈 값으로라도 넣어 두어야 한다.
    """
    keys: set[str] = set(DUMMY_SECRETS)
    keys.update(INT_KEYS)
    for name in (".env", ".env.example"):
        path = REPO_ROOT / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            keys.add(stripped.split("=", 1)[0].strip())
    keys.update(
        {
            "TODO_MODE",
            "AUTH_VERIFY_URL",
            "SYNC_REQUIRED_SCOPE",
            "SYNC_BACKUP_DIR",
            "SYNC_DAEMON_AUTOSTART",
            "SYNC_ALLOW_SCHEMA_DRIFT",
            "SYNC_ENABLED",
            "SYNC_PEER_URL",
            "SYNC_CLIENT_ID",
            "TODO_LOCAL_SESSION_ENABLED",
            "TODO_SESSION_DB_PERSISTENCE",
        }
    )
    return {
        key
        for key in keys
        if key.startswith(("DB_", "AUTH_", "TODO_", "SYNC_", "LIVEKIT_"))
    }


def _child_env(overrides: dict[str, str]) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "PYTHONIOENCODING": "utf-8",
    }
    for key in _managed_keys():
        env[key] = ""
    env.update(overrides)
    return env


def _run_config(overrides: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *CONFIG_ENTRY],
        cwd=REPO_ROOT,
        env=_child_env(overrides),
        capture_output=True,
        text=True,
    )


def _mode_overrides(mode: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    overrides = {"TODO_MODE": mode}
    for key in preflight.REQUIRED_SECRETS.get(mode, ()):
        overrides[key] = DUMMY_SECRETS[key]
    overrides.update(extra or {})
    return overrides


def _snapshot(mode: str, extra: dict[str, str] | None = None) -> dict:
    result = _run_config(_mode_overrides(mode, extra))
    if result.returncode != 0:
        raise AssertionError(
            f"TODO_MODE={mode} config import 실패\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return json.loads(result.stdout)


class ModePresetMatrixTests(unittest.TestCase):
    """네 모드의 파생값이 정답지와 일치하는가."""

    maxDiff = None

    def _assert_subset(self, snapshot: dict, expected: dict) -> None:
        actual = {key: snapshot[key] for key in expected}
        self.assertEqual(actual, expected)

    def test_dev_local_matches_contract(self) -> None:
        snapshot = _snapshot("dev-local")
        self._assert_subset(
            snapshot,
            {
                "TODO_MODE": "dev-local",
                "syncRole": "client",
                "DB_HOST": "host.docker.internal",
                "DB_PORT": 33306,
                "DB_USER": "root",
                "DB_NAME": "teddynote_dev_local",
                "AUTH_ISSUER_URL": "http://localhost:3032",
                "AUTH_PUBLIC_BASE_URL": "http://localhost:3032",
                "AUTH_API_BASE_URL": "http://host.docker.internal:3032",
                "AUTH_JWKS_URL": "http://host.docker.internal:3032/oauth/jwks",
                "AUTH_AUDIENCE": "service:todo",
                "TODO_ALLOWED_ORIGINS": [
                    "http://localhost:30333",
                    "http://127.0.0.1:30333",
                ],
                "TODO_OIDC_CLIENT_ID": "todo-web",
                "TODO_OIDC_REDIRECT_URI": "http://localhost:20023/api/todo/session/callback",
                "TODO_OIDC_CALLBACK_ROUTE_PATH": "/todo/session/callback",
                "TODO_WEB_BASE_URL": "http://localhost:30333",
                "TODO_SESSION_COOKIE_NAME": "teddy_todo_dev_local_session",
                "TODO_SESSION_COOKIE_SECURE": False,
                "TODO_SESSION_COOKIE_SAMESITE": "lax",
                "TODO_SESSION_COOKIE_DOMAIN": "",
                "TODO_SESSION_MAX_AGE_SECONDS": 604800,
                # prod-local 의 거울 — 오프라인 세션은 켜고 영속화는 끈다
                "TODO_LOCAL_SESSION_ENABLED": True,
                "TODO_SESSION_DB_PERSISTENCE": False,
                "LIVEKIT_URL": "ws://host.docker.internal:7880",
                "SYNC_ENABLED": True,
                # 같은 compose 네트워크의 서비스 DNS
                "SYNC_PEER_URL": "http://todo-api-dev-prod:8000",
                "SYNC_CLIENT_ID": "laptop",
                "SYNC_DAEMON_AUTOSTART": True,
                "SYNC_VERIFY_URL": (
                    "http://host.docker.internal:3032/api/internal/service-credentials/verify"
                ),
            },
        )
        # dev-local 은 prod-local 의 거울 — 숨김 UX 까지 동일 (2026-07-31 사용자 확정).
        self.assertEqual(snapshot["featureFlags"], LOCAL_FEATURES)

    def test_dev_prod_matches_contract(self) -> None:
        snapshot = _snapshot("dev-prod")
        self._assert_subset(
            snapshot,
            {
                "TODO_MODE": "dev-prod",
                "syncRole": "server",
                "DB_HOST": "host.docker.internal",
                "DB_PORT": 33306,
                "DB_NAME": "teddynote_dev_prod",
                "AUTH_ISSUER_URL": "http://localhost:3032",
                "AUTH_PUBLIC_BASE_URL": "http://localhost:3032",
                "AUTH_API_BASE_URL": "http://host.docker.internal:3032",
                "AUTH_JWKS_URL": "http://host.docker.internal:3032/oauth/jwks",
                "TODO_ALLOWED_ORIGINS": [
                    "http://localhost:30334",
                    "http://127.0.0.1:30334",
                ],
                "TODO_OIDC_REDIRECT_URI": "http://localhost:20024/api/todo/session/callback",
                "TODO_WEB_BASE_URL": "http://localhost:30334",
                "TODO_SESSION_COOKIE_NAME": "teddy_todo_dev_prod_session",
                "TODO_SESSION_COOKIE_SECURE": False,
                "TODO_LOCAL_SESSION_ENABLED": False,
                "TODO_SESSION_DB_PERSISTENCE": False,
                "LIVEKIT_URL": "ws://host.docker.internal:7880",
                "SYNC_ENABLED": True,
                "SYNC_PEER_URL": "",
                "SYNC_ALLOWED_KEY_IDS": ["laptop-key"],
                "SYNC_DAEMON_AUTOSTART": False,
                "SYNC_VERIFY_URL": (
                    "http://host.docker.internal:3032/api/internal/service-credentials/verify"
                ),
            },
        )
        self.assertEqual(snapshot["featureFlags"], PROD_FEATURES)

    def test_prod_local_matches_compose_todo_api_block(self) -> None:
        snapshot = _snapshot("prod-local")
        self._assert_subset(
            snapshot,
            {
                "TODO_MODE": "prod-local",
                "syncRole": "client",
                "DB_HOST": "host.docker.internal",
                "DB_PORT": 33306,
                "DB_USER": "root",
                "DB_NAME": "teddynote",
                "AUTH_ISSUER_URL": "https://auth.lafamila.xyz",
                "AUTH_PUBLIC_BASE_URL": "https://auth.lafamila.xyz",
                "AUTH_API_BASE_URL": "https://auth.lafamila.xyz",
                "AUTH_JWKS_URL": "https://auth.lafamila.xyz/oauth/jwks",
                "TODO_ALLOWED_ORIGINS": [
                    "http://localhost:3030",
                    "http://127.0.0.1:3030",
                ],
                "TODO_OIDC_REDIRECT_URI": "http://localhost:20022/api/todo/session/callback",
                "TODO_WEB_BASE_URL": "http://localhost:3030",
                "TODO_SESSION_COOKIE_NAME": "teddy_todo_session",
                "TODO_SESSION_COOKIE_SECURE": False,
                "TODO_SESSION_COOKIE_SAMESITE": "lax",
                "TODO_SESSION_COOKIE_DOMAIN": "",
                "TODO_LOCAL_SESSION_ENABLED": True,
                "TODO_SESSION_DB_PERSISTENCE": True,
                "LIVEKIT_URL": "ws://host.docker.internal:7880",
                "SYNC_ENABLED": True,
                "SYNC_PEER_URL": "https://todo.lafamila.xyz",
                "SYNC_CLIENT_ID": "laptop",
                "SYNC_DAEMON_AUTOSTART": True,
                "SYNC_VERIFY_URL": (
                    "https://auth.lafamila.xyz/api/internal/service-credentials/verify"
                ),
            },
        )
        # 단일 사용자 오프라인 복제본 — prod 전용 표면은 숨기고 로컬 버전 기록은 노출한다.
        self.assertEqual(snapshot["featureFlags"], LOCAL_FEATURES)

    def test_prod_prod_matches_root_plan_contract(self) -> None:
        snapshot = _snapshot("prod-prod")
        self._assert_subset(
            snapshot,
            {
                "TODO_MODE": "prod-prod",
                "syncRole": "server",
                "DB_HOST": "teddy-mysql",
                "DB_PORT": 3306,
                "DB_USER": "root",
                "DB_NAME": "teddynote",
                # 브라우저는 공개 URL, 서버-서버/JWKS 는 docker network 내부 주소
                "AUTH_ISSUER_URL": "https://auth.lafamila.xyz",
                "AUTH_PUBLIC_BASE_URL": "https://auth.lafamila.xyz",
                "AUTH_API_BASE_URL": "http://auth-api-nest:3032",
                "AUTH_JWKS_URL": "http://auth-api-nest:3032/oauth/jwks",
                "TODO_ALLOWED_ORIGINS": ["https://todo.lafamila.xyz"],
                "TODO_OIDC_REDIRECT_URI": "https://todo.lafamila.xyz/api/todo/session/callback",
                "TODO_WEB_BASE_URL": "https://todo.lafamila.xyz",
                "TODO_SESSION_COOKIE_NAME": "teddy_todo_session",
                "TODO_SESSION_COOKIE_SECURE": True,
                "TODO_SESSION_COOKIE_SAMESITE": "lax",
                "TODO_LOCAL_SESSION_ENABLED": False,
                "TODO_SESSION_DB_PERSISTENCE": False,
                "LIVEKIT_URL": "ws://teddy-livekit:7880",
                "SYNC_ENABLED": True,
                "SYNC_PEER_URL": "",
                "SYNC_ALLOWED_KEY_IDS": ["laptop-key"],
                "SYNC_DAEMON_AUTOSTART": False,
                "SYNC_VERIFY_URL": (
                    "http://auth-api-nest:3032/api/internal/service-credentials/verify"
                ),
            },
        )
        self.assertEqual(snapshot["featureFlags"], PROD_FEATURES)

    def test_session_cookie_names_are_unique_per_dev_mode(self) -> None:
        """localhost 는 포트가 달라도 쿠키를 공유한다 — 페어가 서로 로그인을 덮으면 안 된다."""
        names = {mode: _snapshot(mode)["TODO_SESSION_COOKIE_NAME"] for mode in ALL_MODES}
        self.assertEqual(names["dev-local"], "teddy_todo_dev_local_session")
        self.assertEqual(names["dev-prod"], "teddy_todo_dev_prod_session")
        self.assertNotEqual(names["dev-local"], names["dev-prod"])
        self.assertNotIn(names["prod-local"], (names["dev-local"], names["dev-prod"]))

    def test_dev_pair_uses_separate_scratch_databases(self) -> None:
        """dev 페어는 실사용 DB 를 절대 건드리지 않는다."""
        dev_local = _snapshot("dev-local")["DB_NAME"]
        dev_prod = _snapshot("dev-prod")["DB_NAME"]
        self.assertEqual(dev_local, "teddynote_dev_local")
        self.assertEqual(dev_prod, "teddynote_dev_prod")
        self.assertNotEqual(dev_local, dev_prod)
        for name in (dev_local, dev_prod):
            self.assertNotIn(name, ("teddynote", "todo"))

    def test_sync_tuning_values_are_mode_independent(self) -> None:
        for mode in ALL_MODES:
            with self.subTest(mode=mode):
                snapshot = _snapshot(mode)
                self.assertEqual(snapshot["SYNC_POLL_SECONDS"], 60)
                self.assertEqual(snapshot["SYNC_PUSH_DEBOUNCE_MS"], 1000)
                self.assertEqual(snapshot["SYNC_OFFLINE_BACKOFF_SECONDS"], 30)
                self.assertEqual(snapshot["SYNC_CLOCK_SKEW_LIMIT_SECONDS"], 5)
                self.assertEqual(snapshot["SYNC_VERIFY_CACHE_SECONDS"], 300)
                self.assertEqual(snapshot["SYNC_BATCH_LIMIT"], 500)
                self.assertEqual(snapshot["SYNC_LOCK_TTL_SECONDS"], 120)
                self.assertEqual(snapshot["SYNC_HTTP_TIMEOUT_SECONDS"], 15)
                self.assertEqual(snapshot["SYNC_ALLOW_SCHEMA_DRIFT"], False)
                self.assertEqual(snapshot["SYNC_REQUIRED_SCOPE"], "sync")


class SyncRoleDerivationTests(unittest.TestCase):
    """위치 축이 sync 역할을 정한다 — `*-local`=client, `*-prod`=server."""

    def test_role_per_mode(self) -> None:
        expected = {
            "dev-local": "client",
            "dev-prod": "server",
            "prod-local": "client",
            "prod-prod": "server",
        }
        for mode, role in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual(_snapshot(mode)["syncRole"], role)

    def test_no_mode_disables_sync(self) -> None:
        """'sync 꺼진 dev' 특수 모드는 없다 — 네 모드 전부 sync 가 켜져 있다."""
        for mode in ALL_MODES:
            with self.subTest(mode=mode):
                self.assertTrue(_snapshot(mode)["SYNC_ENABLED"])


class FeatureFlagAxisTests(unittest.TestCase):
    """표면 판정 축: 위치 — `*-local` 과 `*-prod` 기능 묶음이 서로 다르다."""

    def test_local_side_hides_prod_side_shows(self) -> None:
        for mode in ALL_MODES:
            with self.subTest(mode=mode):
                flags = _snapshot(mode)["featureFlags"]
                self.assertEqual(
                    flags,
                    LOCAL_FEATURES if mode.endswith("-local") else PROD_FEATURES,
                )

    def test_dev_local_mirrors_prod_local_hiding(self) -> None:
        """dev-local 은 prod-local 의 거울 — 숨김 UX 까지 동일해야 한다 (2026-07-31 사용자 확정).

        숨겨진 기능(화면공유·게시·멤버초대)의 개발·테스트는 dev-prod web 에서 한다.
        """
        snapshot = _snapshot("dev-local")
        self.assertEqual(snapshot["syncRole"], "client")
        self.assertEqual(snapshot["featureFlags"], LOCAL_FEATURES)

    def test_legacy_client_still_hides(self) -> None:
        """구동 중인 실사용 스택(:20022, TODO_MODE 미설정, role=client) 보호."""
        legacy_env = {
            "SYNC_ENABLED": "true",
            "SYNC_PEER_URL": "https://todo.lafamila.xyz",
            "DB_PORT": "33306",
            "AUTH_JWKS_CACHE_SECONDS": "300",
            "TODO_SESSION_MAX_AGE_SECONDS": "604800",
            "SYNC_POLL_SECONDS": "60",
            "SYNC_PUSH_DEBOUNCE_MS": "1000",
            "SYNC_OFFLINE_BACKOFF_SECONDS": "30",
            "SYNC_CLOCK_SKEW_LIMIT_SECONDS": "5",
            "SYNC_VERIFY_CACHE_SECONDS": "300",
            "SYNC_BATCH_LIMIT": "500",
            "SYNC_LOCK_TTL_SECONDS": "120",
            "SYNC_HTTP_TIMEOUT_SECONDS": "15",
        }
        result = _run_config(legacy_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertEqual(snapshot["TODO_MODE"], "(legacy)")
        self.assertEqual(snapshot["syncRole"], "client")
        self.assertEqual(snapshot["featureFlags"], LOCAL_FEATURES)


class PrecedenceTests(unittest.TestCase):
    """명시 env > 모드 프리셋 > 레거시 기본값."""

    def test_explicit_env_beats_preset(self) -> None:
        snapshot = _snapshot(
            "prod-local",
            {
                "DB_NAME": "teddynote_scratch",
                "DB_HOST": "127.0.0.1",
                "AUTH_ISSUER_URL": "http://localhost:3032",
                "TODO_WEB_BASE_URL": "http://localhost:9999",
                "SYNC_POLL_SECONDS": "5",
            },
        )
        self.assertEqual(snapshot["DB_NAME"], "teddynote_scratch")
        self.assertEqual(snapshot["DB_HOST"], "127.0.0.1")
        self.assertEqual(snapshot["AUTH_ISSUER_URL"], "http://localhost:3032")
        self.assertEqual(snapshot["TODO_WEB_BASE_URL"], "http://localhost:9999")
        self.assertEqual(snapshot["SYNC_POLL_SECONDS"], 5)
        # 덮지 않은 키는 여전히 프리셋
        self.assertEqual(snapshot["DB_PORT"], 33306)
        self.assertEqual(snapshot["syncRole"], "client")

    def test_empty_env_value_falls_back_to_preset(self) -> None:
        """빈 문자열은 '미지정'이다 — compose 가 `SYNC_PEER_URL: ""` 로 비워도 프리셋이 채운다."""
        snapshot = _snapshot(
            "prod-local",
            {"SYNC_PEER_URL": "", "TODO_ALLOWED_ORIGINS": "", "DB_NAME": ""},
        )
        self.assertEqual(snapshot["SYNC_PEER_URL"], "https://todo.lafamila.xyz")
        self.assertEqual(snapshot["DB_NAME"], "teddynote")
        self.assertEqual(
            snapshot["TODO_ALLOWED_ORIGINS"],
            ["http://localhost:3030", "http://127.0.0.1:3030"],
        )

    def test_explicit_sync_peer_url_still_wins_when_non_empty(self) -> None:
        snapshot = _snapshot(
            "dev-local", {"SYNC_PEER_URL": "http://todo-api-other:8000/"}
        )
        self.assertEqual(snapshot["SYNC_PEER_URL"], "http://todo-api-other:8000")
        self.assertEqual(snapshot["syncRole"], "client")

    def test_legacy_mode_unset_uses_env_only(self) -> None:
        """`TODO_MODE` 미설정이면 프리셋은 전혀 개입하지 않는다."""
        legacy_env = {
            "DB_HOST": "legacy-host",
            "DB_PORT": "13306",
            "DB_NAME": "legacy_db",
            "DB_USER": "legacy_user",
            "AUTH_ISSUER_URL": "http://legacy-auth:1234",
            "AUTH_PUBLIC_BASE_URL": "http://legacy-auth:1234",
            "AUTH_API_BASE_URL": "http://legacy-auth:1234",
            "AUTH_JWKS_URL": "http://legacy-auth:1234/oauth/jwks",
            "AUTH_JWKS_CACHE_SECONDS": "300",
            "TODO_ALLOWED_ORIGINS": "http://legacy-web:1",
            "TODO_OIDC_REDIRECT_URI": "http://legacy-api:2/api/todo/session/callback",
            "TODO_WEB_BASE_URL": "http://legacy-web:1",
            "TODO_SESSION_MAX_AGE_SECONDS": "604800",
            "SYNC_POLL_SECONDS": "60",
            "SYNC_PUSH_DEBOUNCE_MS": "1000",
            "SYNC_OFFLINE_BACKOFF_SECONDS": "30",
            "SYNC_CLOCK_SKEW_LIMIT_SECONDS": "5",
            "SYNC_VERIFY_CACHE_SECONDS": "300",
            "SYNC_BATCH_LIMIT": "500",
            "SYNC_LOCK_TTL_SECONDS": "120",
            "SYNC_HTTP_TIMEOUT_SECONDS": "15",
        }
        result = _run_config(legacy_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)

        self.assertEqual(snapshot["TODO_MODE"], "(legacy)")
        self.assertEqual(snapshot["DB_HOST"], "legacy-host")
        self.assertEqual(snapshot["DB_PORT"], 13306)
        self.assertEqual(snapshot["DB_NAME"], "legacy_db")
        self.assertEqual(snapshot["AUTH_ISSUER_URL"], "http://legacy-auth:1234")
        self.assertEqual(snapshot["TODO_WEB_BASE_URL"], "http://legacy-web:1")
        # 비밀이 하나도 없어도 레거시는 기동을 막지 않는다 (preflight 미동작)
        self.assertFalse(snapshot["secretsPresent"]["DB_PASSWORD"])
        self.assertEqual(snapshot["syncRole"], "disabled")

    def test_legacy_keeps_empty_string_semantics(self) -> None:
        """레거시에서 빈 문자열은 그대로 빈 문자열이다 (기본값으로 되돌아가지 않는다)."""
        legacy_env = {
            "TODO_ALLOWED_ORIGINS": "",
            "DB_PORT": "3306",
            "AUTH_JWKS_CACHE_SECONDS": "300",
            "TODO_SESSION_MAX_AGE_SECONDS": "604800",
            "SYNC_POLL_SECONDS": "60",
            "SYNC_PUSH_DEBOUNCE_MS": "1000",
            "SYNC_OFFLINE_BACKOFF_SECONDS": "30",
            "SYNC_CLOCK_SKEW_LIMIT_SECONDS": "5",
            "SYNC_VERIFY_CACHE_SECONDS": "300",
            "SYNC_BATCH_LIMIT": "500",
            "SYNC_LOCK_TTL_SECONDS": "120",
            "SYNC_HTTP_TIMEOUT_SECONDS": "15",
        }
        result = _run_config(legacy_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertEqual(snapshot["TODO_ALLOWED_ORIGINS"], [])


class PreflightFailureTests(unittest.TestCase):
    """기동 검증이 실제로 프로세스를 막는가 (자식 프로세스 종료코드로 확인)."""

    def _failing(self, overrides: dict[str, str]) -> str:
        result = _run_config(overrides)
        self.assertNotEqual(
            result.returncode, 0, f"기동이 막히지 않았습니다: {result.stdout}"
        )
        return result.stderr

    def test_unknown_mode_is_rejected(self) -> None:
        stderr = self._failing({"TODO_MODE": "production"})
        self.assertIn("TODO_MODE", stderr)
        self.assertIn("production", stderr)

    def test_retired_three_mode_names_are_rejected(self) -> None:
        """구 3모드 이름(`dev`/`local`/`prod`)은 이제 오타다 — 조용히 통과하면 안 된다."""
        for retired in ("dev", "local", "prod"):
            with self.subTest(mode=retired):
                stderr = self._failing({"TODO_MODE": retired})
                self.assertIn("TODO_MODE", stderr)

    def test_missing_required_secret_names_the_key(self) -> None:
        stderr = self._failing(_mode_overrides("prod-local", {"SYNC_SECRET": ""}))
        self.assertIn("SYNC_SECRET", stderr)
        self.assertIn("TODO_MODE=prod-local", stderr)

    def test_dev_local_requires_sync_client_credentials(self) -> None:
        """dev 페어도 실제 auth verify 경로를 타므로 credential 이 필요하다."""
        stderr = self._failing(_mode_overrides("dev-local", {"SYNC_KEY_ID": ""}))
        self.assertIn("SYNC_KEY_ID", stderr)

    def test_dev_prod_requires_server_side_secrets(self) -> None:
        stderr = self._failing(_mode_overrides("dev-prod", {"SYNC_ALLOWED_KEY_IDS": ""}))
        self.assertIn("SYNC_ALLOWED_KEY_IDS", stderr)
        stderr = self._failing(_mode_overrides("dev-prod", {"AUTH_SERVICE_SECRET": ""}))
        self.assertIn("AUTH_SERVICE_SECRET", stderr)

    def test_prod_prod_requires_allowed_key_ids(self) -> None:
        stderr = self._failing(_mode_overrides("prod-prod", {"SYNC_ALLOWED_KEY_IDS": ""}))
        self.assertIn("SYNC_ALLOWED_KEY_IDS", stderr)

    def test_inline_comment_in_value_is_rejected(self) -> None:
        """2026-07-29 prod 장애 재발 방지 — docker env-file 은 인라인 주석을 지원하지 않는다."""
        stderr = self._failing(
            _mode_overrides("prod-prod", {"SYNC_ACCOUNT_ID": "abc-123 # 계정 id"})
        )
        self.assertIn("SYNC_ACCOUNT_ID", stderr)
        self.assertIn("#", stderr)

    def test_malformed_url_is_rejected(self) -> None:
        stderr = self._failing(
            _mode_overrides("prod-local", {"AUTH_ISSUER_URL": "auth.lafamila.xyz"})
        )
        self.assertIn("AUTH_ISSUER_URL", stderr)

    def test_explicit_env_breaking_mode_role_is_rejected(self) -> None:
        """prod-prod 인데 SYNC_PEER_URL 이 들어오면 서버가 클라이언트로 뒤바뀐다 — 막는다."""
        stderr = self._failing(
            _mode_overrides("prod-prod", {"SYNC_PEER_URL": "https://todo.lafamila.xyz"})
        )
        self.assertIn("server", stderr)
        self.assertIn("client", stderr)

    def test_disabling_sync_in_a_mode_is_rejected(self) -> None:
        """모드는 전부 sync 를 쓴다 — SYNC_ENABLED=false 로 역할을 없애면 막는다."""
        stderr = self._failing(_mode_overrides("dev-local", {"SYNC_ENABLED": "false"}))
        self.assertIn("client", stderr)
        self.assertIn("disabled", stderr)

    def test_valid_modes_start_clean(self) -> None:
        for mode in ALL_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(_snapshot(mode)["TODO_MODE"], mode)


class PreflightUnitTests(unittest.TestCase):
    """preflight 개별 규칙 (프로세스 없이)."""

    def test_validate_mode_name(self) -> None:
        self.assertEqual(preflight.validate_mode_name(None), "")
        self.assertEqual(preflight.validate_mode_name("  "), "")
        self.assertEqual(preflight.validate_mode_name(" PROD-LOCAL "), "prod-local")
        for bad in ("prd", "dev", "local", "prod", "dev_local", "prod-localhost"):
            with self.subTest(value=bad):
                with self.assertRaises(PreflightError):
                    preflight.validate_mode_name(bad)

    def test_every_mode_has_secrets_and_role(self) -> None:
        for mode in preflight.VALID_MODES:
            with self.subTest(mode=mode):
                self.assertIn(mode, preflight.REQUIRED_SECRETS)
                self.assertIn(mode, preflight.EXPECTED_ROLE)

    def test_scan_raw_env_catches_inline_comment(self) -> None:
        problems = preflight.scan_raw_env({"SYNC_ACCOUNT_ID": "abc # comment"})
        self.assertEqual(len(problems), 1)
        self.assertIn("SYNC_ACCOUNT_ID", problems[0])

    def test_scan_raw_env_catches_bare_hash_in_non_secret(self) -> None:
        problems = preflight.scan_raw_env({"DB_NAME": "teddynote#1"})
        self.assertEqual(len(problems), 1)

    def test_scan_raw_env_allows_hash_inside_secret(self) -> None:
        """비밀번호에 `#` 이 있는 건 정당하다 — 인라인 주석 형태(공백+#)만 잡는다."""
        self.assertEqual(preflight.scan_raw_env({"DB_PASSWORD": "p#ss#word"}), [])
        self.assertEqual(len(preflight.scan_raw_env({"DB_PASSWORD": "pw # 주석"})), 1)

    def test_scan_raw_env_catches_internal_whitespace(self) -> None:
        self.assertEqual(len(preflight.scan_raw_env({"SYNC_CLIENT_ID": "my laptop"})), 1)

    def test_scan_raw_env_allows_spacing_between_csv_items(self) -> None:
        problems = preflight.scan_raw_env(
            {"TODO_ALLOWED_ORIGINS": "http://a.test, http://b.test"}
        )
        self.assertEqual(problems, [])

    def test_scan_raw_env_ignores_unmanaged_keys(self) -> None:
        self.assertEqual(preflight.scan_raw_env({"PATH": "/usr/bin:/bin # x"}), [])

    def test_check_required_secrets(self) -> None:
        env = {key: "x" for key in preflight.REQUIRED_SECRETS["prod-prod"]}
        self.assertEqual(preflight.check_required_secrets("prod-prod", env), [])
        env["SYNC_ACCOUNT_ID"] = "   "
        problems = preflight.check_required_secrets("prod-prod", env)
        self.assertEqual(len(problems), 1)
        self.assertIn("SYNC_ACCOUNT_ID", problems[0])

    def test_check_urls(self) -> None:
        resolved = {
            "AUTH_ISSUER_URL": "https://auth.test",
            "AUTH_PUBLIC_BASE_URL": "https://auth.test",
            "AUTH_API_BASE_URL": "https://auth.test",
            "AUTH_JWKS_URL": "https://auth.test/oauth/jwks",
            "TODO_OIDC_REDIRECT_URI": "https://todo.test/api/todo/session/callback",
            "TODO_WEB_BASE_URL": "https://todo.test",
            "TODO_ALLOWED_ORIGINS": ["https://todo.test"],
            "SYNC_PEER_URL": "",
            "LIVEKIT_URL": "ws://livekit:7880",
        }
        self.assertEqual(preflight.check_urls(resolved), [])

        # compose 서비스 DNS 도 유효한 URL 이어야 한다 (dev 페어 peer)
        resolved["SYNC_PEER_URL"] = "http://todo-api-dev-prod:8000"
        self.assertEqual(preflight.check_urls(resolved), [])

        resolved["TODO_ALLOWED_ORIGINS"] = ["todo.test"]
        self.assertEqual(len(preflight.check_urls(resolved)), 1)

        resolved["TODO_ALLOWED_ORIGINS"] = ["https://todo.test"]
        resolved["AUTH_JWKS_URL"] = ""
        self.assertEqual(len(preflight.check_urls(resolved)), 1)

    def test_check_role(self) -> None:
        self.assertEqual(preflight.check_role("prod-prod", "server"), [])
        self.assertEqual(preflight.check_role("dev-local", "client"), [])
        self.assertEqual(len(preflight.check_role("prod-prod", "client")), 1)
        self.assertEqual(len(preflight.check_role("dev-local", "disabled")), 1)
        self.assertEqual(preflight.check_role("", "client"), [])

    def test_run_preflight_is_inert_without_mode(self) -> None:
        preflight.run_preflight("", {}, "disabled", env={"DB_NAME": "x # y"})

    def test_run_preflight_collects_every_problem(self) -> None:
        with self.assertRaises(PreflightError) as caught:
            preflight.run_preflight(
                "prod-local",
                {"AUTH_ISSUER_URL": "nope"},
                "disabled",
                env={"SYNC_CLIENT_ID": "bad value"},
            )
        message = str(caught.exception)
        self.assertIn("SYNC_CLIENT_ID", message)
        self.assertIn("SYNC_SECRET", message)
        self.assertIn("AUTH_ISSUER_URL", message)
        self.assertIn("client", message)


if __name__ == "__main__":
    unittest.main()
