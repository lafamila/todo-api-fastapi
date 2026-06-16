import unittest
from urllib.parse import parse_qs, urlsplit

from src.services import session_auth
from src import token_verifier


class TodoSessionServiceOidcTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        session_auth.AUTH_API_BASE_URL = "http://auth.example"
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
        self.assertEqual(parsed.netloc, "auth.example")
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


if __name__ == "__main__":
    unittest.main()
