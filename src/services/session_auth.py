import asyncio
import ipaddress
import json
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from threading import RLock
from time import time
from typing import Any, Callable
from urllib import error, parse, request

from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse

try:
    from ..config import (
        AUTH_API_BASE_URL,
        AUTH_PUBLIC_BASE_URL,
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
    from ..config import (
        AUTH_ISSUER_URL,
        SYNC_ACCOUNT_ID,
        TODO_LOCAL_SESSION_ENABLED,
        runs_sync_daemon,
    )
    from ..connectors import get_db_connection
    from ..token_verifier import build_user_from_payload, decode_auth_api_token, serialize_user
    from .sync_store import get_local_identity, identity_to_user, upsert_local_identity
except ImportError:  # pragma: no cover
    from config import (
        AUTH_API_BASE_URL,
        AUTH_PUBLIC_BASE_URL,
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
    from config import (
        AUTH_ISSUER_URL,
        SYNC_ACCOUNT_ID,
        TODO_LOCAL_SESSION_ENABLED,
        runs_sync_daemon,
    )
    from connectors import get_db_connection
    from token_verifier import build_user_from_payload, decode_auth_api_token, serialize_user
    from services.sync_store import get_local_identity, identity_to_user, upsert_local_identity


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


LOGIN_SCOPE = "openid profile email service.permission"
LOGIN_TRANSACTION_TTL_SECONDS = 10 * 60
TODO_WEB_DEFAULT_RETURN_PATH = "/"
TODO_WEB_LOGIN_PATH = "/login"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().lower().strip("[]")
    if normalized in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_private_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.strip().strip("[]"))
        return address.is_private
    except ValueError:
        return False


def _origin_host(origin: str | None) -> str | None:
    if not origin:
        return None
    return parse.urlsplit(origin).hostname


def _is_trusted_local_topology(
    *, client_host: str | None, target_host: str | None, origin: str | None
) -> bool:
    """직접 loopback 또는 Docker bridge를 통한 localhost 브라우저 요청만 신뢰한다."""
    if _is_loopback_host(client_host):
        return True
    return (
        _is_private_host(client_host)
        and _is_loopback_host(target_host)
        and _is_loopback_host(_origin_host(origin))
    )


@dataclass
class TodoSession:
    id: str
    access_token: str
    refresh_token: str
    access_token_expires_at: float
    user: dict
    issuer: str | None = None
    refresh_lock: Any = field(default_factory=RLock)
    # 원격 auth 가 닿지 않을 때 캐시된 신원으로 발급한 **무기한** 로컬 세션.
    # 만료로 인한 보호를 포기하는 대가로 오프라인 무기한 동작을 얻는다.
    # 전제: 로컬 API 는 loopback에 게시되고 sync client 역할에서만 허용한다.
    offline: bool = False


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
        self.auth_public_base_url = AUTH_PUBLIC_BASE_URL
        self.client_id = TODO_OIDC_CLIENT_ID
        self.client_secret = TODO_OIDC_CLIENT_SECRET
        self.redirect_uri = TODO_OIDC_REDIRECT_URI
        self.callback_route_path = TODO_OIDC_CALLBACK_ROUTE_PATH
        self.cookie_name = TODO_SESSION_COOKIE_NAME
        self._sessions: dict[str, TodoSession] = {}
        self.todo_web_base_url = TODO_WEB_BASE_URL
        self._login_transactions: dict[str, OidcLoginTransaction] = {}
        self._session_invalidation_listeners: list[Callable[[str], None]] = []
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
            self._delete_session(session.id)
            if session.refresh_token:
                await asyncio.to_thread(
                    self._revoke_refresh_token_safe, session.refresh_token
                )
        self._clear_session_cookie(response)

    async def get_user(self, request: Request) -> dict:
        session = await self.require_valid_session(request)
        return {**serialize_user(session.user), "offline": session.offline}

    async def require_valid_session(self, request: Request) -> TodoSession:
        session = self._get_request_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail="Todo session is required")
        if session.offline:
            self._require_local_session_request(request)
            return session
        return await asyncio.to_thread(
            self._refresh_if_needed_sync,
            session.id,
            self._local_session_request_allowed(request),
        )

    # -- 오프라인 로컬 세션 -------------------------------------------------

    async def start_local_session(self, request: Request, response: Response) -> dict:
        """원격 auth 가 닿지 않을 때 **캐시된 신원으로 무기한 로컬 세션**을 발급한다.

        최초 1회는 반드시 원격 auth 로 로그인해야 한다 (그때 신원이 캐시된다).
        트레이드오프는 "노트북 접근자가 로컬 데이터에 접근 가능" 이며, 로컬 MySQL 이 이미
        평문이므로 새로 생기는 위험은 아니다. 그래서 신뢰된 로컬 요청만 허용한다.
        """
        self._require_local_session_request(request)

        identity = await asyncio.to_thread(self._load_identity, SYNC_ACCOUNT_ID)
        if not _identity_matches(identity, SYNC_ACCOUNT_ID, AUTH_ISSUER_URL):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "identity_not_cached",
                    "message": "캐시된 신원이 없습니다. 온라인일 때 원격 auth 로 최소 1회 로그인하세요.",
                },
            )

        session = TodoSession(
            id=_random_token(32),
            access_token="",
            refresh_token="",
            access_token_expires_at=float("inf"),
            user=identity_to_user(identity),
            issuer=identity.get("issuer"),
            offline=True,
        )
        self._store_session(session)
        self._set_session_cookie(response, session.id)
        return {
            **serialize_user(session.user),
            "offline": True,
            "identityVerifiedAt": _iso_or_none(identity.get("verified_at_utc")),
        }

    async def get_local_identity_info(self, request: Request) -> dict:
        """로컬 로그인 가능 여부만 공개한다. 캐시된 개인정보는 인증 전 노출하지 않는다."""
        self._require_local_session_request(request)
        identity = await asyncio.to_thread(self._load_identity, SYNC_ACCOUNT_ID)
        return {
            "available": _identity_matches(
                identity, SYNC_ACCOUNT_ID, AUTH_ISSUER_URL
            )
        }

    def _local_session_request_allowed(self, request: Request) -> bool:
        return self.local_session_connection_allowed(
            client_host=request.client.host if request.client else None,
            target_host=request.url.hostname,
            origin=request.headers.get("origin") or request.headers.get("referer"),
        )

    def local_session_connection_allowed(
        self, *, client_host: str | None, target_host: str | None, origin: str | None
    ) -> bool:
        if not TODO_LOCAL_SESSION_ENABLED or not runs_sync_daemon():
            return False
        return _is_trusted_local_topology(
            client_host=client_host,
            target_host=target_host,
            origin=origin,
        )

    def _require_local_session_request(self, request: Request) -> None:
        if not TODO_LOCAL_SESSION_ENABLED:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "local_session_disabled",
                    "message": "TODO_LOCAL_SESSION_ENABLED=false — 오프라인 로컬 세션이 꺼져 있습니다.",
                },
            )
        if not runs_sync_daemon():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "local_session_wrong_role",
                    "message": "오프라인 로컬 세션은 sync client 배포에서만 허용합니다.",
                },
            )
        if not self._local_session_request_allowed(request):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "loopback_required",
                    "message": "오프라인 로컬 세션은 신뢰된 로컬 요청만 허용합니다.",
                },
            )

    def _load_identity(
        self, account_id: str | None, issuer: str | None = AUTH_ISSUER_URL
    ) -> dict | None:
        if not account_id or not issuer:
            return None
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    return get_local_identity(cursor, account_id, issuer)
        except Exception:  # noqa: BLE001 - 신원 캐시 조회 실패가 로그인 경로를 죽이지 않게
            return None

    def _cache_identity(self, user: dict, issuer: str | None = None) -> None:
        """원격 로그인 성공 시 신원을 캐시한다 (오프라인 세션의 유일한 근거)."""
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    upsert_local_identity(cursor, user, issuer or AUTH_ISSUER_URL)
        except Exception:  # noqa: BLE001 - 캐시 실패가 로그인 자체를 막지 않는다
            pass

    async def get_valid_session_user(self, request: Request) -> dict | None:
        session = self._get_request_session(request)
        if session is None:
            return None
        if session.offline:
            if not self._local_session_request_allowed(request):
                return None
            return session.user
        valid_session = await asyncio.to_thread(
            self._refresh_if_needed_sync,
            session.id,
            self._local_session_request_allowed(request),
        )
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

    async def get_valid_session_from_cookie_header(
        self, cookie_header: str | None, *, allow_offline: bool = False
    ) -> TodoSession | None:
        """소켓 handshake용 세션 검증.

        일반 HTTP 요청과 같은 refresh 경로를 사용하되, 인증 실패는 socket connect가
        service credential 검증으로 넘어갈 수 있도록 ``None``으로 정규화한다.
        """
        return await self.validate_session_id(
            self._parse_session_id(cookie_header),
            allow_offline=allow_offline,
        )

    async def validate_session_id(
        self, session_id: str | None, *, allow_offline: bool = False
    ) -> TodoSession | None:
        session = self._get_session_by_id(session_id)
        if session is None:
            return None
        if session.offline:
            return session if allow_offline else None
        try:
            return await asyncio.to_thread(
                self._refresh_if_needed_sync,
                session.id,
                allow_offline,
            )
        except HTTPException:
            return None

    def on_session_invalidated(self, listener: Callable[[str], None]) -> None:
        """세션 삭제를 realtime 연결 등 프로세스 내부 소비자에게 알린다."""
        with self._lock:
            self._session_invalidation_listeners.append(listener)

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
            self._cache_identity(session.user, session.issuer)
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
        return f"{self.auth_public_base_url}/oauth/authorize?{params}"

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

    def _refresh_if_needed_sync(
        self, session_id: str, allow_offline: bool = False
    ) -> TodoSession:
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
                    # 네트워크가 끊긴 것과 refresh token 이 거절된 것은 다르게 다뤄야 한다.
                    # 앞의 경우 로컬 세션이 허용되면 오프라인으로 이어붙인다 (무기한 동작).
                    if (
                        exc.status_code >= 502
                        and TODO_LOCAL_SESSION_ENABLED
                        and runs_sync_daemon()
                        and allow_offline
                    ):
                        offline_session = self._to_offline_session(session_id)
                        if offline_session is not None:
                            return offline_session
                    self._delete_session_if_refresh_token(
                        session_id, refresh_token
                    )
                    raise HTTPException(status_code=401, detail=exc.detail) from exc

                updated = self._create_session(
                    token,
                    session_id=session_id,
                    refresh_lock=refresh_lock,
                )
                with self._lock:
                    current = self._sessions.get(session_id)
                    replace_session = (
                        current is not None
                        and current.refresh_token == refresh_token
                    )
                    if replace_session:
                        self._sessions[session_id] = updated
                if not replace_session:
                    self._revoke_refresh_token_safe(updated.refresh_token)
                    raise HTTPException(
                        status_code=401,
                        detail="Todo session is required",
                    )
                session = updated

        return session

    def _revoke_refresh_token_safe(self, refresh_token: str) -> None:
        try:
            response = self._request_json(
                "POST",
                f"{self.auth_api_base_url}/oauth/revoke",
                {"token": refresh_token},
                None,
                None,
            )
        except HTTPException:
            return
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
            issuer=str(payload.get("iss") or AUTH_ISSUER_URL),
            refresh_lock=refresh_lock or RLock(),
        )

    def _to_offline_session(self, session_id: str) -> TodoSession | None:
        """만료된 원격 세션을 캐시된 신원 기반 오프라인 세션으로 이어붙인다."""
        current = self._get_session_by_id(session_id)
        if current is None:
            return None
        account_id = str(
            current.user.get("account_id") or current.user.get("id") or ""
        )
        if not account_id or (SYNC_ACCOUNT_ID and account_id != SYNC_ACCOUNT_ID):
            return None
        if not current.issuer:
            return None
        identity = self._load_identity(account_id, current.issuer)
        if not _identity_matches(identity, account_id, current.issuer):
            return None
        with self._lock:
            latest = self._sessions.get(session_id)
            if latest is not current:
                return None
            offline_session = TodoSession(
                id=session_id,
                access_token="",
                refresh_token="",
                access_token_expires_at=float("inf"),
                user=identity_to_user(identity),
                issuer=current.issuer,
                refresh_lock=current.refresh_lock if current else RLock(),
                offline=True,
            )
            self._sessions[session_id] = offline_session
        return offline_session

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
            deleted = self._sessions.pop(session_id, None)
        if deleted is not None:
            self._notify_session_invalidated(session_id)

    def _delete_session_if_refresh_token(
        self, session_id: str, refresh_token: str
    ) -> None:
        with self._lock:
            current = self._sessions.get(session_id)
            if current is None or current.refresh_token != refresh_token:
                return
            self._sessions.pop(session_id, None)
        self._notify_session_invalidated(session_id)

    def _notify_session_invalidated(self, session_id: str) -> None:
        with self._lock:
            listeners = tuple(self._session_invalidation_listeners)
        for listener in listeners:
            try:
                listener(session_id)
            except Exception:  # noqa: BLE001 - 한 소비자가 logout을 깨뜨리지 않게
                pass

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


def _identity_matches(
    identity: dict | None, account_id: str | None, issuer: str | None
) -> bool:
    return bool(
        identity
        and account_id
        and issuer
        and str(identity.get("account_id") or "") == account_id
        and str(identity.get("issuer") or "") == issuer
    )


def _iso_or_none(value: Any) -> str | None:
    try:
        from ..timeutil import iso_utc
    except ImportError:  # pragma: no cover
        from timeutil import iso_utc
    return iso_utc(value) if value is not None else None


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
