"""동기화 인증 — **교체 가능한 단계**.

지금은 auth 가 발급한 service credential(scope `sync`) 하나만 등록한다. 결과는 항상
`SyncPrincipal(account_id, permission)` 으로 정규화되고 하위 로직은 `account_id` 만 본다.
후에 사용자 앱이 PKCE+refresh access token 으로 같은 엔드포인트를 쓰게 되면
`register_authenticator()` 로 검증기 하나만 추가하면 된다 (`token_verifier.py` 재사용).

auth 계약 (auth-api-nest e5c9133 에서 구현·확정):

    POST {AUTH_VERIFY_URL}
    headers: x-auth-service-key-id / x-auth-service-secret   (호출자 = prod todo 자신)
    body:    {"keyId": ..., "secret": ..., "requiredScope": "sync"}

    판정은 **항상 HTTP 200** 이고, 401 은 호출자 자신의 인증 실패만 의미한다.
      {"valid": true,  "serviceKey": "todo", "scopes": ["sync"], "status": "active"}
      {"valid": false, "reason": "invalid_credential" | "disabled" | "scope_missing"}

    - 다른 서비스의 keyId 는 `invalid_credential` 로 돌아온다
    - `disabled` 는 credential 상태·서비스 상태·만료를 모두 포괄한다
    - `requiredScope` 가 미지의 scope 키면 auth 가 400 을 준다
    - 알 수 없는 body 필드도 400 (forbidNonWhitelisted)
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Protocol

try:
    from ..config import (
        AUTH_SERVICE_KEY_ID,
        AUTH_SERVICE_SECRET,
        SYNC_ACCOUNT_ID,
        SYNC_ALLOWED_KEY_IDS,
        SYNC_HTTP_TIMEOUT_SECONDS,
        SYNC_REQUIRED_SCOPE,
        SYNC_VERIFY_CACHE_SECONDS,
        SYNC_VERIFY_URL,
        serves_sync_peer_api,
    )
    from .http_json import HttpUnreachable, request_json
except ImportError:  # pragma: no cover
    from config import (
        AUTH_SERVICE_KEY_ID,
        AUTH_SERVICE_SECRET,
        SYNC_ACCOUNT_ID,
        SYNC_ALLOWED_KEY_IDS,
        SYNC_HTTP_TIMEOUT_SECONDS,
        SYNC_REQUIRED_SCOPE,
        SYNC_VERIFY_CACHE_SECONDS,
        SYNC_VERIFY_URL,
        serves_sync_peer_api,
    )
    from services.http_json import HttpUnreachable, request_json


logger = logging.getLogger(__name__)

SERVICE_CREDENTIAL_KEY_HEADER = "x-auth-service-key-id"
SERVICE_CREDENTIAL_SECRET_HEADER = "x-auth-service-secret"

# service credential 은 서비스 자신의 기계 신원이므로 서비스 최고 권한으로 정규화한다.
# (사용자를 증명하지 않기 때문에 계정은 별도로 고정한다 — resolve_account_id 참조)
SERVICE_CREDENTIAL_PERMISSION = "owner"


@dataclass(frozen=True)
class SyncPrincipal:
    """정규화된 동기화 주체. 하위 로직은 `account_id` 만 사용한다."""

    account_id: str
    permission: str
    subject_kind: str
    subject_id: str


class SyncAuthError(Exception):
    """인증 실패. `status_code` 로 401/403 을 구분한다."""

    def __init__(self, status_code: int, reason: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.message = message


class SyncAuthUnavailable(Exception):
    """auth 서버에 닿지 못해 판정 자체를 못 했다 (503)."""


class SyncAuthenticator(Protocol):
    name: str

    def handles(self, headers: dict[str, str]) -> bool:
        """이 어댑터가 처리할 요청인지."""

    def verify(self, headers: dict[str, str]) -> tuple[str, str]:
        """`(subject_id, permission)` 반환. 실패 시 `SyncAuthError`."""


@dataclass
class _CachedVerdict:
    valid: bool
    reason: str | None
    permission: str
    expires_at: float


class ServiceCredentialAuthenticator:
    """제시된 keyId/secret 을 auth 검증 엔드포인트로 확인한다 (판정 5분 캐시).

    prod todo 는 노트북의 secret 을 저장하지 않는다. 그래서 폐기는 auth 관리화면에서
    `disabled` 로 바꾸면 캐시 만료 후 차단되고, NAS 를 손댈 필요가 없다.
    """

    name = "service_credential"

    def __init__(self) -> None:
        self._cache: dict[str, _CachedVerdict] = {}
        self._lock = RLock()

    def handles(self, headers: dict[str, str]) -> bool:
        return bool(headers.get(SERVICE_CREDENTIAL_KEY_HEADER))

    def verify(self, headers: dict[str, str]) -> tuple[str, str]:
        key_id = (headers.get(SERVICE_CREDENTIAL_KEY_HEADER) or "").strip()
        secret = (headers.get(SERVICE_CREDENTIAL_SECRET_HEADER) or "").strip()
        if not key_id or not secret:
            raise SyncAuthError(401, "missing_credential", "Sync service credential is required")
        self._require_server_allowlist()

        cached = self._get_cached(key_id, secret)
        if cached is not None:
            if cached.valid:
                self._require_allowed_key_id(key_id)
                return key_id, cached.permission
            raise SyncAuthError(403, cached.reason or "invalid_credential", "Sync credential rejected")

        if not AUTH_SERVICE_KEY_ID or not AUTH_SERVICE_SECRET:
            raise SyncAuthUnavailable(
                "AUTH_SERVICE_KEY_ID/AUTH_SERVICE_SECRET are required to verify sync credentials"
            )

        try:
            response = request_json(
                "POST",
                SYNC_VERIFY_URL,
                {"keyId": key_id, "secret": secret, "requiredScope": SYNC_REQUIRED_SCOPE},
                {
                    SERVICE_CREDENTIAL_KEY_HEADER: AUTH_SERVICE_KEY_ID,
                    SERVICE_CREDENTIAL_SECRET_HEADER: AUTH_SERVICE_SECRET,
                },
                timeout=SYNC_HTTP_TIMEOUT_SECONDS,
            )
        except HttpUnreachable as exc:
            raise SyncAuthUnavailable(f"auth verify endpoint unreachable: {exc}") from exc

        if response.status == 401:
            # 우리(prod todo) 자신의 credential 이 거절됐다 — 설정 문제다
            raise SyncAuthUnavailable(
                "this service failed to authenticate itself to auth (check AUTH_SERVICE_* values)"
            )
        if not response.ok or not isinstance(response.data, dict):
            raise SyncAuthUnavailable(
                f"unexpected auth verify response: status={response.status}"
            )

        verdict = response.data
        if verdict.get("valid") is True:
            self._require_allowed_key_id(key_id)
            self._store(key_id, secret, True, None, SERVICE_CREDENTIAL_PERMISSION)
            return key_id, SERVICE_CREDENTIAL_PERMISSION

        reason = str(verdict.get("reason") or "invalid_credential")
        self._store(key_id, secret, False, reason, SERVICE_CREDENTIAL_PERMISSION)
        raise SyncAuthError(403, reason, "Sync credential rejected")

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _require_server_allowlist() -> None:
        if serves_sync_peer_api() and not SYNC_ALLOWED_KEY_IDS:
            raise SyncAuthUnavailable(
                "SYNC_ALLOWED_KEY_IDS is required when serving the sync peer API"
            )

    @staticmethod
    def _require_allowed_key_id(key_id: str) -> None:
        if not serves_sync_peer_api():
            return
        # 전체 allowlist를 끝까지 비교해 첫 일치 위치가 timing으로 드러나지 않게 한다.
        matched = 0
        for allowed_key_id in SYNC_ALLOWED_KEY_IDS:
            matched |= int(secrets.compare_digest(key_id, allowed_key_id))
        if not matched:
            raise SyncAuthError(
                403,
                "credential_not_allowed",
                "Sync credential is valid but not authorized for this peer",
            )

    def _cache_key(self, key_id: str, secret: str) -> str:
        return hashlib.sha256(f"{key_id}:{secret}".encode()).hexdigest()

    def _get_cached(self, key_id: str, secret: str) -> _CachedVerdict | None:
        token = self._cache_key(key_id, secret)
        with self._lock:
            cached = self._cache.get(token)
            if cached is None:
                return None
            if cached.expires_at <= monotonic():
                self._cache.pop(token, None)
                return None
            return cached

    def _store(
        self, key_id: str, secret: str, valid: bool, reason: str | None, permission: str
    ) -> None:
        with self._lock:
            self._cache[self._cache_key(key_id, secret)] = _CachedVerdict(
                valid=valid,
                reason=reason,
                permission=permission,
                expires_at=monotonic() + SYNC_VERIFY_CACHE_SECONDS,
            )


_service_credential_authenticator = ServiceCredentialAuthenticator()
_authenticators: list[SyncAuthenticator] = [_service_credential_authenticator]


def register_authenticator(authenticator: SyncAuthenticator) -> None:
    """검증기를 추가한다 (먼저 등록된 것이 우선)."""
    _authenticators.insert(0, authenticator)


def get_service_credential_authenticator() -> ServiceCredentialAuthenticator:
    return _service_credential_authenticator


class AccountResolutionError(Exception):
    """어느 계정 데이터를 다룰지 결정할 수 없다."""


def resolve_account_id(cursor) -> str:
    """동기화가 다룰 계정 id.

    credential 은 사용자를 증명하지 않으므로 계정을 데이터/설정에서 고정한다.
    `SYNC_ACCOUNT_ID` 가 있으면 그 값, 없으면 데이터의 distinct owner id 가
    정확히 1개일 때 자동 해석한다.
    """
    if SYNC_ACCOUNT_ID:
        return SYNC_ACCOUNT_ID

    owner_ids = distinct_owner_ids(cursor)
    if len(owner_ids) == 1:
        return owner_ids[0]
    if not owner_ids:
        raise AccountResolutionError(
            "이 노드의 동기화 대상 데이터가 비어 있어 계정을 해석할 수 없습니다. "
            "SYNC_ACCOUNT_ID 를 설정하세요 (부트스트랩 전에는 필수입니다)."
        )
    raise AccountResolutionError(
        f"distinct owner id 가 {len(owner_ids)}개입니다({owner_ids}). "
        "SYNC_ACCOUNT_ID 로 어느 계정인지 고정하세요."
    )


def distinct_owner_ids(cursor) -> list[str]:
    """프로젝트의 authoritative owner id 목록.

    `memos.created_by` 와 `project_members.user_id` 는 작성자/협업자이지 데이터셋
    소유자가 아니다. 이 값들을 identity preflight 에 섞으면 정상적인 협업 하나만으로도
    다음 동기화가 영구 차단된다.
    """
    cursor.execute(
        """
        SELECT DISTINCT owner_id AS account_id FROM projects WHERE owner_id IS NOT NULL AND owner_id <> ''
        """
    )
    return sorted(row["account_id"] for row in cursor.fetchall())


def authenticate_headers(headers: dict[str, str], account_id: str) -> SyncPrincipal:
    """헤더 → 정규화된 `SyncPrincipal`."""
    normalized = {key.lower(): value for key, value in headers.items()}
    for authenticator in _authenticators:
        if not authenticator.handles(normalized):
            continue
        subject_id, permission = authenticator.verify(normalized)
        return SyncPrincipal(
            account_id=account_id,
            permission=permission,
            subject_kind=authenticator.name,
            subject_id=subject_id,
        )
    raise SyncAuthError(401, "missing_credential", "Sync service credential is required")
