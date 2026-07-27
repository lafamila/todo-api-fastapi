import asyncio
import json
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from threading import RLock
from time import time
from typing import Any
from urllib import error, parse, request

from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse

try:
    from ..config import (
        AUTH_API_BASE_URL,
        AUTH_SERVICE_KEY_ID,
        AUTH_SERVICE_SECRET,
        TODO_OIDC_CLIENT_ID,
        TODO_OIDC_CLIENT_SECRET,
        TODO_OIDC_CALLBACK_ROUTE_PATH,
        TODO_OIDC_REDIRECT_URI,
        TODO_SESSION_COOKIE_DOMAIN,
        TODO_SESSION_COOKIE_NAME,
        TODO_SESSION_COOKIE_SAMESITE,
        TODO_SESSION_COOKIE_SECURE,
        TODO_SESSION_MAX_AGE_SECONDS,
        TODO_WEB_BASE_URL,
    )
    from ..token_verifier import build_user_from_payload, decode_auth_api_token, serialize_user
except ImportError:  # pragma: no cover
    from config import (
        AUTH_API_BASE_URL,
        AUTH_SERVICE_KEY_ID,
        AUTH_SERVICE_SECRET,
        TODO_OIDC_CLIENT_ID,
        TODO_OIDC_CLIENT_SECRET,
        TODO_OIDC_CALLBACK_ROUTE_PATH,
        TODO_OIDC_REDIRECT_URI,
        TODO_SESSION_COOKIE_DOMAIN,
        TODO_SESSION_COOKIE_NAME,
        TODO_SESSION_COOKIE_SAMESITE,
        TODO_SESSION_COOKIE_SECURE,
        TODO_SESSION_MAX_AGE_SECONDS,
        TODO_WEB_BASE_URL,
    )
    from token_verifier import build_user_from_payload, decode_auth_api_token, serialize_user


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


LOGIN_SCOPE = "openid profile email service.permission"
LOGIN_TRANSACTION_TTL_SECONDS = 10 * 60
TODO_WEB_DEFAULT_RETURN_PATH = "/"
TODO_WEB_LOGIN_PATH = "/login"


@dataclass
class TodoSession:
    id: str
    access_token: str
    refresh_token: str
    access_token_expires_at: float
    user: dict
    refresh_lock: Any = field(default_factory=RLock)


@dataclass
class _HttpResponse:
    status: int
    headers: Any
    data: Any


@dataclass
class OidcLoginTransaction:
    state: str
    code_verifier: str
    return_to_path: str
    created_at: float


@dataclass
class _CallbackResult:
    redirect_url: str
    session_id: str | None = None


class OidcCallbackError(Exception):
    def __init__(self, error_code: str, description: str):
        super().__init__(description)
        self.error_code = error_code
        self.description = description


class TodoSessionService:
    def __init__(self) -> None:
        self.auth_api_base_url = AUTH_API_BASE_URL
        self.client_id = TODO_OIDC_CLIENT_ID
        self.client_secret = TODO_OIDC_CLIENT_SECRET
        self.redirect_uri = TODO_OIDC_REDIRECT_URI
        self.callback_route_path = TODO_OIDC_CALLBACK_ROUTE_PATH
        self.cookie_name = TODO_SESSION_COOKIE_NAME
        self._sessions: dict[str, TodoSession] = {}
        self.todo_web_base_url = TODO_WEB_BASE_URL
        self._login_transactions: dict[str, OidcLoginTransaction] = {}
        self._lock = RLock()

    async def start_login(self, return_to: str | None) -> dict:
        return await asyncio.to_thread(self._start_login_sync, return_to)

    async def handle_oidc_callback(
        self,
        code: str | None,
        state: str | None,
        error: str | None,
        error_description: str | None,
    ) -> RedirectResponse:
        result = await asyncio.to_thread(
            self._handle_oidc_callback_sync,
            code,
            state,
            error,
            error_description,
        )
        response = RedirectResponse(result.redirect_url, status_code=302)
        if result.session_id:
            self._set_session_cookie(response, result.session_id)
        return response

    async def logout(self, request: Request, response: Response) -> None:
        session = self._get_request_session(request)
        if session is not None:
            await asyncio.to_thread(self._revoke_refresh_token_safe, session.refresh_token)
            self._delete_session(session.id)
        self._clear_session_cookie(response)

    async def get_user(self, request: Request) -> dict:
        session = await self.require_valid_session(request)
        return serialize_user(session.user)

    async def require_valid_session(self, request: Request) -> TodoSession:
        session = self._get_request_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail="Todo session is required")
        return await asyncio.to_thread(self._refresh_if_needed_sync, session.id)

    async def get_valid_session_user(self, request: Request) -> dict | None:
        session = self._get_request_session(request)
        if session is None:
            return None
        valid_session = await asyncio.to_thread(self._refresh_if_needed_sync, session.id)
        return valid_session.user

    async def create_service_application(self, request: Request, message: str | None) -> Any:
        session = await self.require_valid_session(request)
        request_message = (
            message.strip()
            if isinstance(message, str) and message.strip()
            else "todo 서비스를 사용하기 위해 user 권한 상승을 요청합니다."
        )
        response = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"{self.auth_api_base_url}/api/service-applications",
            {
                "serviceKey": "todo",
                "message": request_message,
                "requestedPermissionKey": "user",
            },
            {
                "Authorization": f"Bearer {session.access_token}",
            },
            None,
        )
        if 200 <= response.status < 300:
            return response.data
        raise HTTPException(
            status_code=response.status,
            detail=_extract_error_detail(response.data, "Service application failed"),
        )

    async def search_accounts(self, query: str) -> list[dict]:
        if not AUTH_SERVICE_KEY_ID or not AUTH_SERVICE_SECRET:
            raise HTTPException(
                status_code=503,
                detail="Auth service credential is required for account search",
            )
        url = (
            f"{self.auth_api_base_url}/api/internal/service-accounts/search"
            f"?serviceKey=todo&q={parse.quote(query)}"
        )
        response = await asyncio.to_thread(
            self._request_json,
            "GET",
            url,
            None,
            {
                "x-auth-service-key-id": AUTH_SERVICE_KEY_ID,
                "x-auth-service-secret": AUTH_SERVICE_SECRET,
            },
            None,
        )
        if not 200 <= response.status < 300:
            raise HTTPException(
                status_code=response.status,
                detail=_extract_error_detail(response.data, "Account search failed"),
            )

        accounts = response.data if isinstance(response.data, list) else []
        results = []
        for account in accounts:
            permission_key = (
                account.get("permissionKey")
                if isinstance(account.get("permissionKey"), str)
                else "visitor"
            )
            can_invite = permission_key != "visitor"
            results.append(
                {
                    "id": str(account.get("id", "")),
                    "username": str(account.get("loginId", "")),
                    "displayName": str(account.get("name", "")),
                    "loginId": str(account.get("loginId", "")),
                    "name": str(account.get("name", "")),
                    "email": str(account.get("email", "")),
                    "isAdmin": bool(account.get("isSuperAdmin")),
                    "isSuperAdmin": bool(account.get("isSuperAdmin")),
                    "canInviteToTodo": can_invite,
                    "inviteDisabledReason": None if can_invite else "visitor",
                }
            )
        return results

    def get_session_user_from_cookie_header(self, cookie_header: str | None) -> dict | None:
        session = self._get_session_by_id(self._parse_session_id(cookie_header))
        return session.user if session is not None else None

    def is_session_configured(self) -> bool:
        return True

    def _start_login_sync(self, return_to: str | None) -> dict:
        self._prune_expired_login_transactions()
        self._ensure_oidc_configured()

        state = _random_token(24)
        code_verifier = _random_token(48)
        code_challenge = _code_challenge(code_verifier)
        transaction = OidcLoginTransaction(
            state=state,
            code_verifier=code_verifier,
            return_to_path=_normalize_return_to_path(return_to),
            created_at=time(),
        )
        with self._lock:
            self._login_transactions[state] = transaction

        return {
            "authorizeUrl": self._build_authorize_url(state, code_challenge),
        }

    def _handle_oidc_callback_sync(
        self,
        code: str | None,
        state: str | None,
        error_code: str | None,
        error_description: str | None,
    ) -> _CallbackResult:
        self._prune_expired_login_transactions()

        transaction = self._pop_login_transaction(state)
        if error_code:
            return _CallbackResult(
                redirect_url=self._build_error_redirect_url(
                    error_code,
                    error_description or "Authorization was denied",
                )
            )
        if transaction is None:
            return _CallbackResult(
                redirect_url=self._build_error_redirect_url(
                    "invalid_state",
                    "Login transaction expired or was not found",
                )
            )
        if not code:
            return _CallbackResult(
                redirect_url=self._build_error_redirect_url(
                    "invalid_request",
                    "Authorization code is missing",
                )
            )

        try:
            token = self._exchange_code_for_token(code, transaction.code_verifier)
            session = self._create_session(token)
            self._store_session(session)
        except OidcCallbackError as exc:
            return _CallbackResult(
                redirect_url=self._build_error_redirect_url(
                    exc.error_code,
                    exc.description,
                )
            )
        except HTTPException as exc:
            return _CallbackResult(
                redirect_url=self._build_error_redirect_url(
                    "callback_failed",
                    _stringify_error_detail(exc.detail, "OIDC callback failed"),
                )
            )

        return _CallbackResult(
            redirect_url=self._build_success_redirect_url(transaction.return_to_path),
            session_id=session.id,
        )

    def _build_authorize_url(self, state: str, code_challenge: str) -> str:
        params = parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": LOGIN_SCOPE,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.auth_api_base_url}/oauth/authorize?{params}"

    def _exchange_code_for_token(self, code: str, code_verifier: str) -> dict:
        self._ensure_oidc_configured()
        response = self._request_json(
            "POST",
            f"{self.auth_api_base_url}/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            },
            None,
            None,
        )
        if not 200 <= response.status < 300:
            error_code, description = _extract_oidc_error(
                response.data,
                fallback_error="token_exchange_failed",
                fallback_description="Token exchange failed",
            )
            raise OidcCallbackError(error_code, description)
        if not isinstance(response.data, dict):
            raise OidcCallbackError("token_exchange_failed", "Invalid token response")
        return response.data

    def _request_token(self, body: dict[str, Any]) -> dict:
        clean_body = {key: value for key, value in body.items() if value is not None}
        response = self._request_json(
            "POST",
            f"{self.auth_api_base_url}/oauth/token",
            clean_body,
            None,
            None,
        )
        if not 200 <= response.status < 300:
            raise HTTPException(
                status_code=response.status,
                detail=_extract_error_detail(response.data, "Token exchange failed"),
            )
        if not isinstance(response.data, dict):
            raise HTTPException(status_code=503, detail="Invalid token response")
        return response.data

    def _refresh_if_needed_sync(self, session_id: str) -> TodoSession:
        session = self._get_session_by_id(session_id)
        if session is None:
            raise HTTPException(status_code=401, detail="Todo session is required")

        if session.access_token_expires_at - time() <= 30:
            with session.refresh_lock:
                session = self._get_session_by_id(session_id)
                if session is None:
                    raise HTTPException(status_code=401, detail="Todo session is required")
                if session.access_token_expires_at - time() > 30:
                    return session

                refresh_token = session.refresh_token
                refresh_lock = session.refresh_lock

                try:
                    token = self._request_token(
                        {
                            "grant_type": "refresh_token",
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "refresh_token": refresh_token,
                        }
                    )
                except HTTPException as exc:
                    with self._lock:
                        current = self._sessions.get(session_id)
                        if current is not None and current.refresh_token == refresh_token:
                            self._sessions.pop(session_id, None)
                    raise HTTPException(status_code=401, detail=exc.detail) from exc

                updated = self._create_session(
                    token,
                    session_id=session_id,
                    refresh_lock=refresh_lock,
                )
                with self._lock:
                    self._sessions[session_id] = updated
                session = updated

        return session

    def _revoke_refresh_token_safe(self, refresh_token: str) -> None:
        response = self._request_json(
            "POST",
            f"{self.auth_api_base_url}/oauth/revoke",
            {"token": refresh_token},
            None,
            None,
        )
        if response.status >= 500:
            return

    def _create_session(
        self,
        token: dict,
        session_id: str | None = None,
        refresh_lock: Any | None = None,
    ) -> TodoSession:
        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token")
        expires_in = token.get("expires_in")
        if not access_token or not refresh_token or not expires_in:
            raise HTTPException(status_code=503, detail="Invalid token response")

        payload = decode_auth_api_token(access_token)
        user = build_user_from_payload(payload)
        return TodoSession(
            id=session_id or _random_token(32),
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_expires_at=time() + int(expires_in),
            user=user,
            refresh_lock=refresh_lock or RLock(),
        )

    def _get_request_session(self, request_obj: Request) -> TodoSession | None:
        session_id = request_obj.cookies.get(self.cookie_name)
        return self._get_session_by_id(session_id)

    def _get_session_by_id(self, session_id: str | None) -> TodoSession | None:
        if not session_id:
            return None
        with self._lock:
            return self._sessions.get(session_id)

    def _store_session(self, session: TodoSession) -> None:
        with self._lock:
            self._sessions[session.id] = session

    def _delete_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _pop_login_transaction(self, state: str | None) -> OidcLoginTransaction | None:
        if not state:
            return None
        with self._lock:
            return self._login_transactions.pop(state, None)

    def _prune_expired_login_transactions(self) -> None:
        cutoff = time() - LOGIN_TRANSACTION_TTL_SECONDS
        with self._lock:
            expired_states = [
                state
                for state, transaction in self._login_transactions.items()
                if transaction.created_at < cutoff
            ]
            for state in expired_states:
                self._login_transactions.pop(state, None)

    def _set_session_cookie(self, response: Response, session_id: str) -> None:
        response.set_cookie(
            key=self.cookie_name,
            value=session_id,
            max_age=TODO_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=TODO_SESSION_COOKIE_SECURE,
            samesite=TODO_SESSION_COOKIE_SAMESITE,
            path="/",
            domain=TODO_SESSION_COOKIE_DOMAIN,
        )

    def _clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(
            key=self.cookie_name,
            path="/",
            domain=TODO_SESSION_COOKIE_DOMAIN,
            secure=TODO_SESSION_COOKIE_SECURE,
            samesite=TODO_SESSION_COOKIE_SAMESITE,
        )

    def _parse_session_id(self, cookie_header: str | None) -> str | None:
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(self.cookie_name)
        return morsel.value if morsel else None

    def _build_success_redirect_url(self, return_to_path: str) -> str:
        return _join_base_and_path(self.todo_web_base_url, return_to_path)

    def _build_error_redirect_url(self, error_code: str, error_description: str) -> str:
        query = parse.urlencode(
            {
                "error": error_code,
                "error_description": error_description,
            }
        )
        return (
            f"{_join_base_and_path(self.todo_web_base_url, TODO_WEB_LOGIN_PATH)}?{query}"
        )

    def _ensure_oidc_configured(self) -> None:
        if not self.client_id:
            raise HTTPException(status_code=503, detail="TODO_OIDC_CLIENT_ID is required")
        if not self.redirect_uri:
            raise HTTPException(
                status_code=503,
                detail="TODO_OIDC_REDIRECT_URI is required",
            )

    def _request_json(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None,
        headers: dict[str, str] | None,
        allowed_error_statuses: set[int] | None,
    ) -> _HttpResponse:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(url, data=payload, method=method.upper())
        req.add_header("Accept", "application/json")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)

        opener = request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(req, timeout=10) as response_obj:
                raw = response_obj.read().decode("utf-8")
                return _HttpResponse(
                    status=response_obj.status,
                    headers=response_obj.headers,
                    data=_decode_json(raw),
                )
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            if allowed_error_statuses and exc.code in allowed_error_statuses:
                return _HttpResponse(
                    status=exc.code,
                    headers=exc.headers,
                    data=_decode_json(raw),
                )
            return _HttpResponse(
                status=exc.code,
                headers=exc.headers,
                data=_decode_json(raw),
            )
        except error.URLError as exc:
            raise HTTPException(status_code=502, detail=f"Upstream connection failed: {exc.reason}") from exc


_session_service = TodoSessionService()


def get_session_service() -> TodoSessionService:
    return _session_service


def _decode_json(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _extract_error_detail(data: Any, fallback: str) -> Any:
    if isinstance(data, dict):
        for key in ("detail", "message", "error_description", "error"):
            if key in data and data[key]:
                return data[key]
    if isinstance(data, (list, str)) and data:
        return data
    return fallback


def _random_token(byte_length: int) -> str:
    import secrets

    return secrets.token_urlsafe(byte_length)


def _code_challenge(code_verifier: str) -> str:
    import base64
    import hashlib

    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _extract_oidc_error(
    data: Any,
    fallback_error: str,
    fallback_description: str,
) -> tuple[str, str]:
    if isinstance(data, dict):
        error_code = str(data.get("error") or fallback_error)
        description = _stringify_error_detail(
            data.get("error_description")
            or data.get("detail")
            or data.get("message"),
            fallback_description,
        )
        return error_code, description
    return fallback_error, _stringify_error_detail(data, fallback_description)


def _stringify_error_detail(detail: Any, fallback: str) -> str:
    if isinstance(detail, str) and detail.strip():
        return detail
    if isinstance(detail, list) and detail:
        rendered = ", ".join(str(item) for item in detail if item)
        if rendered:
            return rendered
    return fallback


def _normalize_return_to_path(return_to: str | None) -> str:
    if not return_to:
        return TODO_WEB_DEFAULT_RETURN_PATH
    parsed = parse.urlsplit(return_to)
    if parsed.scheme or parsed.netloc:
        return TODO_WEB_DEFAULT_RETURN_PATH
    path = parsed.path or TODO_WEB_DEFAULT_RETURN_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    return parse.urlunsplit(("", "", path, parsed.query, parsed.fragment))


def _join_base_and_path(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path if path.startswith('/') else f'/{path}'}"
