import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Request, Response

from src.services import session_auth
from src import token_verifier


class TodoSessionServiceOidcTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        session_auth.AUTH_API_BASE_URL = "http://auth.example"
        session_auth.AUTH_PUBLIC_BASE_URL = "http://auth-browser.example"
        session_auth.TODO_OIDC_CLIENT_ID = "todo-web"
        session_auth.TODO_OIDC_CLIENT_SECRET = "todo-secret"
        session_auth.TODO_OIDC_REDIRECT_URI = (
            "http://todo.example/api/todo/session/callback"
        )
        session_auth.TODO_OIDC_CALLBACK_ROUTE_PATH = "/todo/session/callback"
        session_auth.TODO_WEB_BASE_URL = "http://todo.example"
        session_auth.TODO_SESSION_COOKIE_NAME = "teddy_todo_session"
        session_auth.TODO_SESSION_COOKIE_SECURE = False
        session_auth.TODO_SESSION_COOKIE_SAMESITE = "lax"
        session_auth.TODO_SESSION_COOKIE_DOMAIN = None
        session_auth.TODO_SESSION_MAX_AGE_SECONDS = 3600
        self.service = session_auth.TodoSessionService()

    async def test_start_login_returns_authorize_url_with_pkce_state(self) -> None:
        payload = await self.service.start_login("/projects/alpha?view=board")

        authorize_url = payload["authorizeUrl"]
        parsed = urlsplit(authorize_url)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.netloc, "auth-browser.example")
        self.assertEqual(parsed.path, "/oauth/authorize")
        self.assertEqual(params["client_id"], ["todo-web"])
        self.assertEqual(
            params["redirect_uri"],
            ["http://todo.example/api/todo/session/callback"],
        )
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["scope"], [session_auth.LOGIN_SCOPE])
        self.assertEqual(params["code_challenge_method"], ["S256"])

        state = params["state"][0]
        transaction = self.service._login_transactions[state]
        self.assertEqual(transaction.return_to_path, "/projects/alpha?view=board")
        self.assertTrue(transaction.code_verifier)

    async def test_callback_success_sets_cookie_and_redirects_to_return_to(self) -> None:
        payload = await self.service.start_login("/projects/alpha")
        state = parse_qs(urlsplit(payload["authorizeUrl"]).query)["state"][0]
        code_verifier = self.service._login_transactions[state].code_verifier

        def fake_exchange(code: str, code_verifier: str) -> dict:
            self.assertEqual(code, "auth-code")
            self.assertEqual(code_verifier, code_verifier_expected)
            return {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_in": 3600,
            }

        code_verifier_expected = code_verifier

        def fake_create_session(token: dict) -> session_auth.TodoSession:
            self.assertEqual(token["access_token"], "access-token")
            return session_auth.TodoSession(
                id="todo-session-123",
                access_token="access-token",
                refresh_token="refresh-token",
                access_token_expires_at=9999999999,
                user={"id": "user-1", "permission": "user"},
            )

        self.service._exchange_code_for_token = fake_exchange  # type: ignore[method-assign]
        self.service._create_session = fake_create_session  # type: ignore[method-assign]

        response = await self.service.handle_oidc_callback(
            code="auth-code",
            state=state,
            error=None,
            error_description=None,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "http://todo.example/projects/alpha")
        self.assertIn("teddy_todo_session=todo-session-123", response.headers["set-cookie"])
        self.assertIn("todo-session-123", self.service._sessions)
        self.assertNotIn(state, self.service._login_transactions)

    async def test_callback_error_redirects_back_to_login(self) -> None:
        response = await self.service.handle_oidc_callback(
            code=None,
            state=None,
            error="access_denied",
            error_description="Todo access approval required",
        )

        self.assertEqual(response.status_code, 302)
        parsed = urlsplit(response.headers["location"])
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.netloc, "todo.example")
        self.assertEqual(parsed.path, "/login")
        self.assertEqual(params["error"], ["access_denied"])
        self.assertEqual(
            params["error_description"],
            ["Todo access approval required"],
        )
        self.assertNotIn("set-cookie", response.headers)

    async def test_start_login_rejects_absolute_return_to(self) -> None:
        payload = await self.service.start_login("https://evil.example/phish")
        state = parse_qs(urlsplit(payload["authorizeUrl"]).query)["state"][0]

        self.assertEqual(
            self.service._login_transactions[state].return_to_path,
            session_auth.TODO_WEB_DEFAULT_RETURN_PATH,
        )

    async def test_create_service_application_requests_user_permission(self) -> None:
        request = object()
        session = session_auth.TodoSession(
            id="todo-session-123",
            access_token="access-token",
            refresh_token="refresh-token",
            access_token_expires_at=9999999999,
            user={"id": "user-1", "permission": "visitor"},
        )
        calls = []

        async def fake_require_valid_session(request_value):
            self.assertIs(request_value, request)
            return session

        def fake_request_json(method, url, body, headers, allowed_error_statuses):
            calls.append((method, url, body, headers, allowed_error_statuses))
            return session_auth._HttpResponse(
                status=201,
                headers={},
                data={"id": "application-1"},
            )

        self.service.require_valid_session = fake_require_valid_session  # type: ignore[method-assign]
        self.service._request_json = fake_request_json  # type: ignore[method-assign]

        response = await self.service.create_service_application(
            request,  # type: ignore[arg-type]
            "  Please grant access  ",
        )

        self.assertEqual(response, {"id": "application-1"})
        self.assertEqual(
            calls,
            [
                (
                    "POST",
                    "http://auth.example/api/service-applications",
                    {
                        "serviceKey": "todo",
                        "message": "Please grant access",
                        "requestedPermissionKey": "user",
                    },
                    {"Authorization": "Bearer access-token"},
                    None,
                )
            ],
        )

    async def test_create_service_application_uses_default_for_blank_message(
        self,
    ) -> None:
        request = object()
        session = session_auth.TodoSession(
            id="todo-session-123",
            access_token="access-token",
            refresh_token="refresh-token",
            access_token_expires_at=9999999999,
            user={"id": "user-1", "permission": "visitor"},
        )
        payloads = []

        async def fake_require_valid_session(_request):
            return session

        def fake_request_json(_method, _url, body, _headers, _allowed_error_statuses):
            payloads.append(body)
            return session_auth._HttpResponse(status=201, headers={}, data={})

        self.service.require_valid_session = fake_require_valid_session  # type: ignore[method-assign]
        self.service._request_json = fake_request_json  # type: ignore[method-assign]

        for message in ("", " \t\n ", None):
            with self.subTest(message=message):
                await self.service.create_service_application(
                    request,  # type: ignore[arg-type]
                    message,
                )

        self.assertEqual(
            payloads,
            [
                {
                    "serviceKey": "todo",
                    "message": "todo 서비스를 사용하기 위해 user 권한 상승을 요청합니다.",
                    "requestedPermissionKey": "user",
                }
            ]
            * 3,
        )


class TodoSocketSessionValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = session_auth.TodoSessionService()
        self.session = session_auth.TodoSession(
            id="socket-session",
            access_token="expired-access",
            refresh_token="refresh-token",
            access_token_expires_at=0,
            user={"id": "user-1", "permission": "user"},
        )
        self.service._store_session(self.session)
        self.cookie = f"{self.service.cookie_name}={self.session.id}"

    async def test_expired_refresh_rejected_connect_is_denied(self) -> None:
        invalidated: list[str] = []
        self.service.on_session_invalidated(invalidated.append)

        def reject_refresh(_body):
            raise HTTPException(status_code=400, detail="invalid_grant")

        self.service._request_token = reject_refresh  # type: ignore[method-assign]

        validated = await self.service.get_valid_session_from_cookie_header(
            self.cookie
        )

        self.assertIsNone(validated)
        self.assertNotIn(self.session.id, self.service._sessions)
        self.assertEqual(invalidated, [self.session.id])

    async def test_expired_session_refreshes_before_connect_is_accepted(self) -> None:
        self.service._request_token = lambda _body: {  # type: ignore[method-assign]
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }

        def create_refreshed(token, *, session_id=None, refresh_lock=None):
            self.assertEqual(token["access_token"], "new-access")
            self.assertEqual(session_id, self.session.id)
            return session_auth.TodoSession(
                id=session_id,
                access_token=token["access_token"],
                refresh_token=token["refresh_token"],
                access_token_expires_at=9999999999,
                user={"id": "user-1", "permission": "user"},
                refresh_lock=refresh_lock,
            )

        self.service._create_session = create_refreshed  # type: ignore[method-assign]

        validated = await self.service.get_valid_session_from_cookie_header(
            self.cookie
        )

        self.assertIsNotNone(validated)
        self.assertEqual(validated.access_token, "new-access")
        self.assertIs(self.service._sessions[self.session.id], validated)

    async def test_logout_during_refresh_cannot_resurrect_session(self) -> None:
        revoked: list[str] = []
        self.service._request_token = lambda _body: {  # type: ignore[method-assign]
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }
        self.service._revoke_refresh_token_safe = revoked.append  # type: ignore[method-assign]

        def create_after_logout(token, *, session_id=None, refresh_lock=None):
            self.service._delete_session(session_id)
            return session_auth.TodoSession(
                id=session_id,
                access_token=token["access_token"],
                refresh_token=token["refresh_token"],
                access_token_expires_at=9999999999,
                user={"id": "user-1", "permission": "user"},
                refresh_lock=refresh_lock,
            )

        self.service._create_session = create_after_logout  # type: ignore[method-assign]

        validated = await self.service.get_valid_session_from_cookie_header(
            self.cookie
        )

        self.assertIsNone(validated)
        self.assertNotIn(self.session.id, self.service._sessions)
        self.assertEqual(revoked, ["new-refresh"])


class TodoTokenVerifierTests(unittest.TestCase):
    def test_builds_superadmin_user_from_auth_service_claim(self) -> None:
        user = token_verifier.build_user_from_payload(
            {
                "sub": "account-1",
                "preferred_username": "lafamila",
                "name": "Lafamila",
                "email": "lafamila@example.test",
                token_verifier.SERVICE_CLAIM: {
                    "key": "todo",
                    "permission": "superadmin",
                    "permissionSchemaVersion": 1,
                },
            }
        )

        self.assertEqual(user["permission"], "superadmin")
        self.assertTrue(user["is_admin"])
        self.assertTrue(user["is_super_admin"])


def _request(
    client_host: str,
    *,
    target_host: str = "localhost:20022",
    origin: str | None = "http://localhost:3030",
    cookie: str | None = None,
) -> Request:
    headers = [(b"host", target_host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/session/local",
            "raw_path": b"/api/session/local",
            "query_string": b"",
            "headers": headers,
            "client": (client_host, 41000),
            "server": ("0.0.0.0", 8000),
        }
    )


class TodoLocalSessionSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = session_auth.TodoSessionService()
        self.identity = {
            "account_id": "account-1",
            "login_id": "lafamila",
            "display_name": "Lafamila",
            "email": "private@example.test",
            "permission": "user",
            "issuer": session_auth.AUTH_ISSUER_URL,
            "verified_at_utc": None,
        }
        self.service._load_identity = lambda *_args: self.identity  # type: ignore[method-assign]

    async def test_local_session_requires_sync_client_role(self) -> None:
        with (
            patch.object(session_auth, "TODO_LOCAL_SESSION_ENABLED", True),
            patch.object(session_auth, "runs_sync_daemon", return_value=False),
        ):
            with self.assertRaises(HTTPException) as raised:
                await self.service.start_local_session(
                    _request("127.0.0.1"), Response()
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "local_session_wrong_role")

    async def test_direct_loopback_client_can_start_local_session(self) -> None:
        response = Response()
        with (
            patch.object(session_auth, "TODO_LOCAL_SESSION_ENABLED", True),
            patch.object(session_auth, "SYNC_ACCOUNT_ID", "account-1"),
            patch.object(session_auth, "runs_sync_daemon", return_value=True),
        ):
            result = await self.service.start_local_session(
                _request("127.0.0.1", origin=None), response
            )

        self.assertTrue(result["offline"])
        self.assertIn("teddy_todo_session=", response.headers["set-cookie"])

    async def test_docker_bridge_requires_loopback_target_and_origin(self) -> None:
        with (
            patch.object(session_auth, "TODO_LOCAL_SESSION_ENABLED", True),
            patch.object(session_auth, "SYNC_ACCOUNT_ID", "account-1"),
            patch.object(session_auth, "runs_sync_daemon", return_value=True),
        ):
            allowed = self.service._local_session_request_allowed(
                _request("192.168.65.1")
            )
            missing_origin = self.service._local_session_request_allowed(
                _request("192.168.65.1", origin=None)
            )
            public_target = self.service._local_session_request_allowed(
                _request(
                    "192.168.65.1",
                    target_host="todo.example",
                    origin="https://todo.example",
                )
            )

        self.assertTrue(allowed)
        self.assertFalse(missing_origin)
        self.assertFalse(public_target)

    async def test_remote_request_cannot_use_offline_session(self) -> None:
        offline = session_auth.TodoSession(
            id="offline-session",
            access_token="",
            refresh_token="",
            access_token_expires_at=float("inf"),
            user={"id": "account-1"},
            offline=True,
        )
        self.service._store_session(offline)
        request = _request(
            "203.0.113.10",
            target_host="todo.example",
            origin="https://todo.example",
            cookie=f"{self.service.cookie_name}=offline-session",
        )

        with (
            patch.object(session_auth, "TODO_LOCAL_SESSION_ENABLED", True),
            patch.object(session_auth, "runs_sync_daemon", return_value=True),
        ):
            with self.assertRaises(HTTPException) as raised:
                await self.service.require_valid_session(request)

        self.assertEqual(raised.exception.status_code, 403)

    async def test_identity_probe_returns_availability_without_personal_data(self) -> None:
        with (
            patch.object(session_auth, "TODO_LOCAL_SESSION_ENABLED", True),
            patch.object(session_auth, "SYNC_ACCOUNT_ID", "account-1"),
            patch.object(session_auth, "runs_sync_daemon", return_value=True),
        ):
            result = await self.service.get_local_identity_info(
                _request("127.0.0.1")
            )

        self.assertEqual(result, {"available": True})
        self.assertNotIn("email", result)
        self.assertNotIn("accountId", result)

    async def test_explicit_local_session_uses_configured_account_only(self) -> None:
        requested: list[tuple[str | None, str | None]] = []

        def load_identity(account_id, issuer=session_auth.AUTH_ISSUER_URL):
            requested.append((account_id, issuer))
            return self.identity if account_id == "account-1" else None

        self.service._load_identity = load_identity  # type: ignore[method-assign]
        with (
            patch.object(session_auth, "TODO_LOCAL_SESSION_ENABLED", True),
            patch.object(session_auth, "SYNC_ACCOUNT_ID", "account-1"),
            patch.object(session_auth, "runs_sync_daemon", return_value=True),
        ):
            await self.service.start_local_session(
                _request("127.0.0.1", origin=None), Response()
            )

        self.assertEqual(
            requested, [("account-1", session_auth.AUTH_ISSUER_URL)]
        )

    async def test_offline_conversion_never_switches_to_latest_other_account(self) -> None:
        online = session_auth.TodoSession(
            id="online-session",
            access_token="expired",
            refresh_token="refresh",
            access_token_expires_at=0,
            user={"id": "account-1", "account_id": "account-1"},
            issuer="http://auth.example",
        )
        self.service._store_session(online)
        requested: list[tuple[str | None, str | None]] = []

        def load_identity(account_id, issuer=session_auth.AUTH_ISSUER_URL):
            requested.append((account_id, issuer))
            return {
                **self.identity,
                "account_id": "other-account",
            }

        self.service._load_identity = load_identity  # type: ignore[method-assign]
        with patch.object(session_auth, "SYNC_ACCOUNT_ID", "account-1"):
            converted = self.service._to_offline_session("online-session")

        self.assertIsNone(converted)
        self.assertEqual(
            requested, [("account-1", "http://auth.example")]
        )

    async def test_offline_conversion_binds_original_issuer(self) -> None:
        online = session_auth.TodoSession(
            id="online-session",
            access_token="expired",
            refresh_token="refresh",
            access_token_expires_at=0,
            user={"id": "account-1", "account_id": "account-1"},
            issuer="https://issuer.example",
        )
        self.service._store_session(online)
        requested: list[tuple[str | None, str | None]] = []

        def load_identity(account_id, issuer=session_auth.AUTH_ISSUER_URL):
            requested.append((account_id, issuer))
            return None

        self.service._load_identity = load_identity  # type: ignore[method-assign]
        with patch.object(session_auth, "SYNC_ACCOUNT_ID", "account-1"):
            converted = self.service._to_offline_session("online-session")

        self.assertIsNone(converted)
        self.assertEqual(
            requested, [("account-1", "https://issuer.example")]
        )


if __name__ == "__main__":
    unittest.main()
