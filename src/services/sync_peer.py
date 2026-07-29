"""원격(NAS) todo 를 부르는 클라이언트. **접속은 언제나 노트북이 시작한다.**

NAS → 노트북 인바운드는 만들지 않는다 (인바운드 포트·DDNS 불필요). 그래서 push 도 pull 도
락 위임도 병합도 전부 이 방향의 호출이다.
"""

from __future__ import annotations

from urllib.parse import quote

try:
    from ..config import (
        SYNC_HTTP_TIMEOUT_SECONDS,
        SYNC_KEY_ID,
        SYNC_PEER_URL,
        SYNC_SECRET,
    )
    from ..sync_schema import SCHEMA_VERSION
    from .http_json import HttpUnreachable, request_json
    from .sync_auth import (
        SERVICE_CREDENTIAL_KEY_HEADER,
        SERVICE_CREDENTIAL_SECRET_HEADER,
    )
except ImportError:  # pragma: no cover
    from config import SYNC_HTTP_TIMEOUT_SECONDS, SYNC_KEY_ID, SYNC_PEER_URL, SYNC_SECRET
    from sync_schema import SCHEMA_VERSION
    from services.http_json import HttpUnreachable, request_json
    from services.sync_auth import (
        SERVICE_CREDENTIAL_KEY_HEADER,
        SERVICE_CREDENTIAL_SECRET_HEADER,
    )


class SyncPeerUnreachable(Exception):
    """네트워크가 닿지 않는다 = 오프라인. 백오프 후 재시도한다."""


class SyncPeerError(Exception):
    """원격이 요청을 거절했다 (인증/스키마/검증). 재시도로 풀리지 않는다."""

    def __init__(self, status: int, detail):
        super().__init__(f"peer rejected request: status={status} detail={detail}")
        self.status = status
        self.detail = detail


def normalize_peer_root(url: str) -> str:
    """`SYNC_PEER_URL` 은 사이트 루트다. `/api` 를 붙여 준 값도 받아들인다."""
    root = (url or "").strip().rstrip("/")
    if root.endswith("/api"):
        root = root[: -len("/api")]
    return root


class SyncPeer:
    def __init__(
        self,
        base_url: str | None = None,
        key_id: str | None = None,
        secret: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.root = normalize_peer_root(base_url if base_url is not None else SYNC_PEER_URL)
        self.key_id = key_id if key_id is not None else SYNC_KEY_ID
        self.secret = secret if secret is not None else SYNC_SECRET
        self.timeout = timeout or SYNC_HTTP_TIMEOUT_SECONDS

    @property
    def configured(self) -> bool:
        return bool(self.root and self.key_id and self.secret)

    @property
    def socket_url(self) -> str:
        return self.root

    def auth_headers(self) -> dict[str, str]:
        return {
            SERVICE_CREDENTIAL_KEY_HEADER: self.key_id,
            SERVICE_CREDENTIAL_SECRET_HEADER: self.secret,
        }

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.configured:
            raise SyncPeerError(
                0, "SYNC_PEER_URL / SYNC_KEY_ID / SYNC_SECRET 가 모두 설정되어야 합니다"
            )
        try:
            response = request_json(
                method,
                f"{self.root}{path}",
                body,
                self.auth_headers(),
                timeout=self.timeout,
            )
        except HttpUnreachable as exc:
            raise SyncPeerUnreachable(str(exc)) from exc
        if not response.ok:
            raise SyncPeerError(response.status, response.data)
        if not isinstance(response.data, dict):
            raise SyncPeerError(response.status, "expected a JSON object")
        return response.data

    # --- 동기화 프로토콜 ---------------------------------------------------

    def handshake(self) -> dict:
        return self._call("GET", "/api/sync/handshake")

    def changes(self, since: int, limit: int) -> dict:
        return self._call("GET", f"/api/sync/changes?since={int(since)}&limit={int(limit)}")

    def push(self, client_id: str, changes: list[dict]) -> dict:
        return self._call(
            "POST",
            "/api/sync/push",
            {
                "clientId": client_id,
                "schemaVersion": SCHEMA_VERSION,
                "changes": changes,
            },
        )

    # --- 락 위임 ----------------------------------------------------------

    def lock_acquire(self, memo_id: str, owner_key: str, user_id: str, display_name: str) -> dict:
        return self._call(
            "POST",
            f"/api/sync/locks/{quote(memo_id)}/acquire",
            {"ownerKey": owner_key, "userId": user_id, "displayName": display_name},
        )

    def lock_release(self, memo_id: str, owner_key: str) -> dict:
        return self._call(
            "POST",
            f"/api/sync/locks/{quote(memo_id)}/release",
            {"ownerKey": owner_key},
        )

    def lock_holder(self, memo_id: str) -> dict:
        return self._call("GET", f"/api/sync/locks/{quote(memo_id)}")

    # --- 병합 위임 --------------------------------------------------------

    # 브라우저용 경로(`/api/memos/{id}/merge-into/{id}`)는 세션 쿠키를 요구한다.
    # 피어는 service credential 로 인증하므로 `/api/sync/merge/...` 를 쓴다.
    def merge_memo(self, loser_id: str, winner_id: str) -> dict:
        return self._call(
            "POST", f"/api/sync/merge/memos/{quote(loser_id)}/merge-into/{quote(winner_id)}"
        )

    def merge_project(self, loser_id: str, winner_id: str) -> dict:
        return self._call(
            "POST",
            f"/api/sync/merge/projects/{quote(loser_id)}/merge-into/{quote(winner_id)}",
        )


_peer = SyncPeer()


def get_sync_peer() -> SyncPeer:
    return _peer
