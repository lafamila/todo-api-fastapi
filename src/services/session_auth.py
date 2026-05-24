import asyncio
import json
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from threading import RLock
from time import time
from typing import Any
from urllib import error, parse, request

from fastapi import HTTPException, Request, Response

try:
    from ..config import (
        AUTH_API_BASE_URL,
        AUTH_SERVICE_KEY_ID,
        AUTH_SERVICE_SECRET,
        TODO_OIDC_CLIENT_ID,
        TODO_OIDC_CLIENT_SECRET,
        TODO_OIDC_REDIRECT_URI,
        TODO_SESSION_COOKIE_DOMAIN,
        TODO_SESSION_COOKIE_NAME,
        TODO_SESSION_COOKIE_SAMESITE,
        TODO_SESSION_COOKIE_SECURE,
        TODO_SESSION_MAX_AGE_SECONDS,
    )
    from ..token_verifier import build_user_from_payload, decode_auth_api_token, serialize_user
except ImportError:  # pragma: no cover
    from config import (
        AUTH_API_BASE_URL,
        AUTH_SERVICE_KEY_ID,
        AUTH_SERVICE_SECRET,
        TODO_OIDC_CLIENT_ID,
        TODO_OIDC_CLIENT_SECRET,
        TODO_OIDC_REDIRECT_URI,
        TODO_SESSION_COOKIE_DOMAIN,
        TODO_SESSION_COOKIE_NAME,
        TODO_SESSION_COOKIE_SAMESITE,
        TODO_SESSION_COOKIE_SECURE,
        TODO_SESSION_MAX_AGE_SECONDS,
    )
    from token_verifier import build_user_from_payload, decode_auth_api_token, serialize_user


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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


class TodoSessionService:
    def __init__(self) -> None:
        self.auth_api_base_url = AUTH_API_BASE_URL
        self.client_id = TODO_OIDC_CLIENT_ID
        self.client_secret = TODO_OIDC_CLIENT_SECRET
        self.redirect_uri = TODO_OIDC_REDIRECT_URI
        self.cookie_name = TODO_SESSION_COOKIE_NAME
        self._sessions: dict[str, TodoSession] = {}
        self._lock = RLock()

    async def login(self, login_id: str, password: str, response: Response) -> dict:
        session = await asyncio.to_thread(self._login_sync, login_id, password)
        self._set_session_cookie(response, session.id)
        return serialize_user(session.user)

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
        response = await asyncio.to_thread(
            self._request_json,
            "POST",
            f"{self.auth_api_base_url}/api/service-applications",
            {"serviceKey": "todo", "message": message or ""},
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

    def _login_sync(self, login_id: str, password: str) -> TodoSession:
        auth_cookie = self._create_auth_api_session(login_id, password)
        code_verifier = _random_token(48)
        code_challenge = _code_challenge(code_verifier)
        code = self._authorize(auth_cookie, code_challenge)
        token = self._request_token(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            }
        )
        session = self._create_session(token)
        self._store_session(session)
        return session

    def _create_auth_api_session(self, login_id: str, password: str) -> str:
        response = self._request_json(
            "POST",
            f"{self.auth_api_base_url}/login",
            {"loginId": login_id, "password": password},
            None,
            None,
        )
        if not 200 <= response.status < 300:
            raise HTTPException(
                status_code=response.status,
                detail=_extract_error_detail(response.data, "Central login failed"),
            )
        cookies = response.headers.get_all("Set-Cookie") if response.headers else []
        auth_cookies = [cookie.split(";", 1)[0] for cookie in cookies if cookie]
        if not auth_cookies:
            raise HTTPException(status_code=503, detail="Auth session cookie missing")
        return "; ".join(auth_cookies)

    def _authorize(self, auth_cookie: str, code_challenge: str) -> str:
        authorize_url = (
            f"{self.auth_api_base_url}/oauth/authorize"
            f"?client_id={parse.quote(self.client_id)}"
            f"&redirect_uri={parse.quote(self.redirect_uri, safe='')}"
            "&response_type=code"
            "&scope=openid%20profile%20email%20service.permission"
            f"&state={parse.quote(_random_token(16))}"
            f"&code_challenge={parse.quote(code_challenge)}"
            "&code_challenge_method=S256"
        )
        response = self._request_json(
            "GET",
            authorize_url,
            None,
            {"Cookie": auth_cookie},
            {302, 303, 307, 308},
        )
        location = response.headers.get("Location") if response.headers else None
        if not location:
            raise HTTPException(status_code=503, detail="Authorize redirect missing")
        redirect_url = parse.urlparse(location)
        params = parse.parse_qs(redirect_url.query)
        if "error" in params:
            detail = params.get("error_description", params["error"])[0]
            raise HTTPException(status_code=401, detail=detail)
        codes = params.get("code")
        if not codes:
            raise HTTPException(status_code=503, detail="Authorization code missing")
        return codes[0]

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
