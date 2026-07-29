import asyncio
import logging
from collections import defaultdict
from time import monotonic
from urllib.parse import urlsplit

try:
    import socketio
except ImportError:  # pragma: no cover
    socketio = None

try:
    from ..config import (
        SYNC_CLIENT_ID,
        SYNC_LOCK_TTL_SECONDS,
        runs_sync_daemon,
        serves_sync_peer_api,
    )
    from ..connectors import get_db_connection
    from ..utils import can_write_project, check_project_membership
    from .lock_registry import get_lock_registry
    from .sync_auth import (
        AccountResolutionError,
        SyncAuthError,
        SyncAuthUnavailable,
        authenticate_headers,
        resolve_account_id,
    )
    from .sync_peer import SyncPeerError, SyncPeerUnreachable, get_sync_peer
    from .sync_runtime import get_sync_runtime
except ImportError:  # pragma: no cover
    from config import (
        SYNC_CLIENT_ID,
        SYNC_LOCK_TTL_SECONDS,
        runs_sync_daemon,
        serves_sync_peer_api,
    )
    from connectors import get_db_connection
    from utils import can_write_project, check_project_membership
    from services.lock_registry import get_lock_registry
    from services.sync_auth import (
        AccountResolutionError,
        SyncAuthError,
        SyncAuthUnavailable,
        authenticate_headers,
        resolve_account_id,
    )
    from services.sync_peer import SyncPeerError, SyncPeerUnreachable, get_sync_peer
    from services.sync_runtime import get_sync_runtime


logger = logging.getLogger(__name__)

SYNC_ROOM_PREFIX = "sync:"

# pull 로 들어온 변경임을 프론트가 구분할 수 있게 하는 출처 표시.
# 편집 중 버퍼를 덮을지 말지는 `todo-web-next` 가 이 값으로 판단한다.
ORIGIN_SYNC_PULL = "sync-pull"
SOCKET_SESSION_REVALIDATE_SECONDS = 30


class LockDelegationError(RuntimeError):
    """온라인 lock 단일 진실에 접근했지만 이번 요청을 안전하게 처리하지 못했다."""


def sync_room(account_id: str) -> str:
    return f"{SYNC_ROOM_PREFIX}{account_id}"


class MemoRealtimeServer:
    def __init__(self, session_service, allowed_origins: list[str]):
        self.session_service = session_service
        self.allowed_origins = allowed_origins
        self.rooms: dict[str, set[str]] = defaultdict(set)
        self.users: dict[str, dict] = {}
        self.socket_memos: dict[str, set[str]] = defaultdict(set)
        self.socket_locks: dict[str, set[str]] = defaultdict(set)
        self.socket_lease_tokens: dict[str, dict[str, tuple[str, float]]] = defaultdict(dict)
        self.screen_shares: dict[str, dict] = {}
        self.socket_projects: dict[str, set[str]] = defaultdict(set)
        self.socket_sessions: dict[str, str] = {}
        self.session_sockets: dict[str, set[str]] = defaultdict(set)
        self.socket_allows_offline: dict[str, bool] = {}
        self.session_revalidation_tasks: dict[str, asyncio.Task] = {}
        # 전역 룸 `sync:<accountId>` 를 구독하는 피어 소켓 (동기화 데몬)
        self.peer_sockets: dict[str, str] = {}

        # 소켓 핸들러와 위임용 HTTP 엔드포인트가 **같은** 락 저장소를 쓴다
        self.lock_registry = get_lock_registry()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock_delegation_quarantine_until = 0.0
        self.available = socketio is not None
        self.session_service.on_session_invalidated(self._on_session_invalidated)

        self.sio = None
        if not self.available:
            logger.warning("python-socketio is not installed; realtime server disabled")
            return

        self.sio = socketio.AsyncServer(
            async_mode="asgi",
            cors_allowed_origins=allowed_origins,
            cors_credentials=True,
        )
        self._register_handlers()
        # 모든 역할에서 로컬 TTL 만료/해제를 브라우저에 반영해야 한다. 서버 역할만
        # 등록하면 오프라인 client 역할의 만료가 화면에 남아 split-brain 이 된다.
        self.lock_registry.on_change(self._on_lock_change)

    def wrap_asgi(self, other_asgi_app):
        if not self.available:
            return other_asgi_app
        return socketio.ASGIApp(self.sio, other_asgi_app, socketio_path="api/socket.io")

    def bind_loop(self) -> None:
        """실행 중인 이벤트 루프를 잡아둔다 (스레드에서 온 락 변경 알림을 emit 하기 위해)."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover
            self._loop = None

    # -- 브라우저 세션 수명 ------------------------------------------------

    def _register_browser_socket(
        self, sid: str, session, allow_offline: bool
    ) -> None:
        user = session.user
        self.users[sid] = {
            "user_id": user["id"],
            "display_name": user.get("display_name")
            or user.get("username")
            or user["id"],
            "permission": user.get("permission"),
        }
        self.socket_memos[sid] = set()
        self.socket_projects[sid] = set()
        self.socket_locks[sid] = set()
        self.socket_lease_tokens[sid] = {}
        self.socket_sessions[sid] = session.id
        self.session_sockets[session.id].add(sid)
        self.socket_allows_offline[sid] = allow_offline
        self.session_revalidation_tasks[sid] = asyncio.create_task(
            self._revalidate_browser_session_loop(sid),
            name=f"todo-socket-session:{sid}",
        )

    async def _revalidate_browser_session_loop(self, sid: str) -> None:
        """긴 연결도 access token 만료/refresh 거절을 반영한다.

        30초마다 프로세스 내부 세션을 확인하지만 auth 서버 호출은 기존
        ``_refresh_if_needed_sync``가 만료 30초 이내일 때만 수행한다.
        """
        try:
            while sid in self.socket_sessions:
                await asyncio.sleep(SOCKET_SESSION_REVALIDATE_SECONDS)
                if not await self._revalidate_browser_session(sid):
                    return
        except asyncio.CancelledError:
            return

    async def _revalidate_browser_session(self, sid: str) -> bool:
        session_id = self.socket_sessions.get(sid)
        if session_id is None:
            return False
        session = await self.session_service.validate_session_id(
            session_id,
            allow_offline=self.socket_allows_offline.get(sid, False),
        )
        if session is None:
            await self._disconnect_browser_socket(sid)
            return False
        user = session.user
        self.users[sid] = {
            "user_id": user["id"],
            "display_name": user.get("display_name")
            or user.get("username")
            or user["id"],
            "permission": user.get("permission"),
        }
        return True

    def _on_session_invalidated(self, session_id: str) -> None:
        """logout/refresh 거절 시 해당 세션의 모든 브라우저 소켓을 끊는다."""
        loop = self._loop
        if loop is None or not self.available:
            return

        def schedule_disconnect() -> None:
            asyncio.create_task(
                self._disconnect_session_sockets(session_id),
                name=f"todo-session-disconnect:{session_id}",
            )

        loop.call_soon_threadsafe(schedule_disconnect)

    async def _disconnect_session_sockets(self, session_id: str) -> None:
        for sid in list(self.session_sockets.get(session_id, set())):
            await self._disconnect_browser_socket(sid)

    async def _disconnect_browser_socket(self, sid: str) -> None:
        # Socket.IO disconnect callback보다 먼저 권한 캐시를 제거해 같은 loop tick에서
        # 들어온 후속 이벤트도 인증된 사용자로 재사용되지 않게 한다.
        self.users.pop(sid, None)
        self._forget_socket_session(sid)
        try:
            await self.sio.disconnect(sid)
        except Exception:  # noqa: BLE001 - 이미 끊긴 연결은 정리 완료로 본다
            logger.debug("socket disconnect raced with transport close: %s", sid)

    def _forget_socket_session(self, sid: str) -> None:
        session_id = self.socket_sessions.pop(sid, None)
        if session_id is not None:
            sockets = self.session_sockets.get(session_id)
            if sockets is not None:
                sockets.discard(sid)
                if not sockets:
                    self.session_sockets.pop(session_id, None)
        self.socket_allows_offline.pop(sid, None)
        task = self.session_revalidation_tasks.pop(sid, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    # -- 락 위임 -----------------------------------------------------------

    def _owner_key(self, sid: str) -> str:
        return f"{SYNC_CLIENT_ID}:{sid}"

    async def _delegate_lock(self, call, *args):
        """온라인 클라이언트 역할이면 락 조작을 원격에 위임한다.

        `None` 은 요청 시작 시 이미 오프라인이거나 로컬 전용 역할일 때만 반환한다.
        온라인 요청 중 위임 오류가 발생하면 그 요청은 fail-closed 한다. 오류 때문에 런타임이
        오프라인으로 전환된 뒤의 **다음** 요청부터 로컬 락을 쓸 수 있다.
        온라인일 때 락이 원격에서 단일하게 관리되므로 **온라인 동시 편집 충돌이 사라진다**.
        """
        if not runs_sync_daemon():
            return None
        runtime = get_sync_runtime()
        if not runtime.online:
            if monotonic() < getattr(self, "_lock_delegation_quarantine_until", 0):
                raise LockDelegationError(
                    "remote lock outcome is uncertain; wait for its lease to expire"
                )
            if getattr(runtime, "last_error_kind", None) != "offline":
                raise LockDelegationError("offline lock mode is not established yet")
            return None
        peer = get_sync_peer()
        if not peer.configured:
            raise LockDelegationError("sync peer is not configured")
        try:
            result = await asyncio.to_thread(call, *args)
            self._lock_delegation_quarantine_until = 0.0
            return result
        except SyncPeerUnreachable as exc:
            runtime.mark_offline(f"lock delegation failed: {exc}")
            self._lock_delegation_quarantine_until = (
                monotonic() + SYNC_LOCK_TTL_SECONDS
            )
            raise LockDelegationError("sync peer is unreachable") from exc
        except SyncPeerError as exc:
            logger.warning("remote lock delegation rejected: %s", exc)
            raise LockDelegationError("sync peer rejected the lock request") from exc

    async def _resolve_lock_holder(self, memo_id: str) -> dict | None:
        delegated = await self._delegate_lock(get_sync_peer().lock_holder, memo_id)
        if delegated is not None:
            return delegated.get("holder")
        return self.lock_registry.holder(memo_id)

    # -- 핸들러 -------------------------------------------------------------

    def _register_handlers(self) -> None:
        @self.sio.event
        async def connect(sid, environ, auth):  # noqa: ARG001
            if self._loop is None:
                self.bind_loop()

            headers = _extract_headers(environ)
            scope = environ.get("asgi.scope") or {}
            client = scope.get("client") or (None, None)
            target_host = urlsplit(f"//{headers.get('host', '')}").hostname
            allow_offline = self.session_service.local_session_connection_allowed(
                client_host=client[0],
                target_host=target_host,
                origin=headers.get("origin") or headers.get("referer"),
            )
            session = await self.session_service.get_valid_session_from_cookie_header(
                headers.get("cookie"),
                allow_offline=allow_offline,
            )
            if session:
                self._register_browser_socket(sid, session, allow_offline)
                return

            # 브라우저 세션이 없으면 동기화 피어(데몬)일 수 있다 — service credential 로 확인
            account_id = await self._authenticate_peer_socket(headers)
            if account_id is None:
                raise ConnectionRefusedError("Todo session is required")

            self.peer_sockets[sid] = account_id
            await self.sio.enter_room(sid, sync_room(account_id))
            await self.sio.emit("syncSubscribed", {"accountId": account_id}, to=sid)

        @self.sio.event
        async def disconnect(sid):
            if sid in self.peer_sockets:
                self.peer_sockets.pop(sid, None)
                return

            for memo_id in list(self.socket_memos.get(sid, set())):
                await self._leave_memo(sid, memo_id)

            stopped_projects = []
            for project_id, share in list(self.screen_shares.items()):
                if share["socketId"] == sid:
                    self.screen_shares.pop(project_id, None)
                    stopped_projects.append(project_id)

            for project_id in stopped_projects:
                await self.sio.emit(
                    "screenShareStopped",
                    {"projectId": project_id},
                    room=f"project:{project_id}",
                )

            for memo_id in list(self.socket_locks.get(sid, set())):
                await self._release_lock(sid, memo_id)

            self.users.pop(sid, None)
            self.socket_memos.pop(sid, None)
            self.socket_projects.pop(sid, None)
            self.socket_locks.pop(sid, None)
            self.socket_lease_tokens.pop(sid, None)
            self._forget_socket_session(sid)

        @self.sio.on("joinProject")
        async def join_project(sid, data):
            await self._join_project(sid, data)

        @self.sio.on("leaveProject")
        async def leave_project(sid, data):
            project_id = _get_id(data, "projectId")
            if (
                not project_id
                or project_id not in self.socket_projects.get(sid, set())
            ):
                return
            await self.sio.leave_room(sid, f"project:{project_id}")
            self.socket_projects[sid].discard(project_id)

            share = self.screen_shares.get(project_id)
            if share and share["socketId"] == sid:
                await self._stop_screen_share(project_id)

        @self.sio.on("startScreenShare")
        async def start_screen_share(sid, data):
            await self._start_screen_share(sid, data)

        @self.sio.on("stopScreenShare")
        async def stop_screen_share(sid, data):
            project_id = _get_id(data, "projectId")
            if (
                not project_id
                or project_id not in self.socket_projects.get(sid, set())
            ):
                return
            share = self.screen_shares.get(project_id)
            if not share or share["socketId"] != sid:
                return
            await self._stop_screen_share(project_id)

        @self.sio.on("joinMemo")
        async def join_memo(sid, data):
            memo_id = _get_id(data, "memoId")
            user_info = self.users.get(sid)
            if (
                not memo_id
                or not user_info
                or not await asyncio.to_thread(
                    self._can_access_memo, memo_id, user_info, False
                )
            ):
                if memo_id:
                    await self.sio.emit(
                        "memoAccessDenied", {"memoId": memo_id}, to=sid
                    )
                return
            room_name = f"memo:{memo_id}"
            await self.sio.enter_room(sid, room_name)
            self.rooms[memo_id].add(sid)
            self.socket_memos[sid].add(memo_id)

            lock_available = True
            try:
                lock = await self._resolve_lock_holder(memo_id)
            except LockDelegationError:
                lock = None
                lock_available = False
            await self.sio.emit(
                "lockStatus",
                {
                    "memoId": memo_id,
                    "lockedBy": lock["displayName"] if lock else None,
                    "lockedByUserId": lock["userId"] if lock else None,
                    "available": lock_available,
                },
                to=sid,
            )

        @self.sio.on("leaveMemo")
        async def leave_memo(sid, data):
            memo_id = _get_id(data, "memoId")
            if memo_id:
                await self._leave_memo(sid, memo_id)

        @self.sio.on("lockMemo")
        async def lock_memo(sid, data):
            memo_id = _get_id(data, "memoId")
            if not memo_id or memo_id not in self.socket_memos.get(sid, set()):
                return
            await self._acquire_lock(sid, memo_id, renewal=False)

        @self.sio.on("renewMemoLock")
        async def renew_memo_lock(sid, data):
            memo_id = _get_id(data, "memoId")
            if not memo_id or memo_id not in self.socket_locks.get(sid, set()):
                return
            await self._acquire_lock(sid, memo_id, renewal=True)

        @self.sio.on("unlockMemo")
        async def unlock_memo(sid, data):
            memo_id = _get_id(data, "memoId")
            if not memo_id or memo_id not in self.socket_locks.get(sid, set()):
                return
            await self._release_lock(sid, memo_id)

        @self.sio.on("memoUpdated")
        async def memo_updated(sid, data):
            await self._broadcast_memo_update(sid, data)

    async def _acquire_lock(self, sid: str, memo_id: str, renewal: bool) -> None:
        user_info = self.users.get(sid)
        if (
            not user_info
            or memo_id not in self.socket_memos.get(sid, set())
            or not await asyncio.to_thread(
                self._can_access_memo, memo_id, user_info, True
            )
        ):
            if user_info:
                await self.sio.emit(
                    "lockDenied",
                    {"memoId": memo_id, "reason": "access_denied"},
                    to=sid,
                )
            return

        owner_key = self._owner_key(sid)
        try:
            delegated = await self._delegate_lock(
                get_sync_peer().lock_acquire,
                memo_id,
                owner_key,
                user_info["user_id"],
                user_info["display_name"],
            )
        except LockDelegationError as exc:
            await self.sio.emit(
                "lockDenied",
                {
                    "memoId": memo_id,
                    "lockedBy": "동기화 서버 연결 오류",
                    "lockedByUserId": None,
                    "reason": "delegation_unavailable",
                    "message": str(exc),
                },
                to=sid,
            )
            return

        if delegated is not None:
            acquired = bool(delegated.get("acquired"))
            holder = delegated.get("holder") or {}
        else:
            acquired, holder = self.lock_registry.acquire(
                memo_id, owner_key, user_info["user_id"], user_info["display_name"]
            )

        if not acquired:
            self.socket_locks[sid].discard(memo_id)
            self._lease_tokens_for(sid).pop(memo_id, None)
            await self.sio.emit(
                "lockDenied",
                {
                    "memoId": memo_id,
                    "lockedBy": holder.get("displayName"),
                    "lockedByUserId": holder.get("userId"),
                },
                to=sid,
            )
            return

        self.socket_locks[sid].add(memo_id)
        lease_token = str(holder.get("leaseToken") or "")
        if not lease_token:
            self.socket_locks[sid].discard(memo_id)
            await self.sio.emit(
                "lockDenied",
                {
                    "memoId": memo_id,
                    "reason": "invalid_lease",
                    "message": "lock service did not issue a lease token",
                },
                to=sid,
            )
            return
        self._lease_tokens_for(sid)[memo_id] = (
            lease_token,
            monotonic() + max(SYNC_LOCK_TTL_SECONDS * 0.75, 1),
        )
        renew_after_ms = max(int(SYNC_LOCK_TTL_SECONDS * 1000 / 3), 1000)
        if renewal:
            await self.sio.emit(
                "lockLeaseRenewed",
                {
                    "memoId": memo_id,
                    "leaseToken": lease_token,
                    "generation": holder.get("generation"),
                    "renewAfterMs": renew_after_ms,
                },
                to=sid,
            )
            return
        await self.sio.emit(
            "memoLocked",
            {
                "memoId": memo_id,
                "displayName": user_info["display_name"],
                "userId": user_info["user_id"],
            },
            room=f"memo:{memo_id}",
            skip_sid=sid,
        )
        await self.sio.emit(
            "lockAcquired",
            {
                "memoId": memo_id,
                "leaseToken": lease_token,
                "generation": holder.get("generation"),
                "renewAfterMs": renew_after_ms,
            },
            to=sid,
        )

    async def _authenticate_peer_socket(self, headers: dict[str, str]) -> str | None:
        if not serves_sync_peer_api():
            return None
        if not headers.get("x-auth-service-key-id"):
            return None
        try:
            return await asyncio.to_thread(_verify_peer_credential, headers)
        except (SyncAuthError, SyncAuthUnavailable, AccountResolutionError) as exc:
            logger.warning("sync peer socket rejected: %s", exc)
            return None

    async def _release_lock(self, sid: str, memo_id: str) -> None:
        if memo_id not in self.socket_locks.get(sid, set()):
            return
        owner_key = self._owner_key(sid)
        try:
            delegated = await self._delegate_lock(
                get_sync_peer().lock_release, memo_id, owner_key
            )
        except LockDelegationError as exc:
            await self.sio.emit(
                "lockReleaseFailed",
                {"memoId": memo_id, "message": str(exc)},
                to=sid,
            )
            return
        if delegated is None:
            released = self.lock_registry.release(memo_id, owner_key)
            if released is None:
                self.socket_locks[sid].discard(memo_id)
                self._lease_tokens_for(sid).pop(memo_id, None)
                return
        elif not bool(delegated.get("released")):
            # 다른 owner의 위임 락을 release한 것처럼 로컬에 알리면 화면과 단일
            # 진실이 갈라진다. 요청 소켓의 stale 주장만 버리고 room broadcast는 금지한다.
            self.socket_locks[sid].discard(memo_id)
            self._lease_tokens_for(sid).pop(memo_id, None)
            await self.sio.emit(
                "lockReleaseFailed",
                {"memoId": memo_id, "message": "lock is not owned by this socket"},
                to=sid,
            )
            return
        self.socket_locks[sid].discard(memo_id)
        self._lease_tokens_for(sid).pop(memo_id, None)
        if delegated is not None:
            # 원격 listener의 syncLockChanged가 canonical 알림이지만, 소켓 가속기가
            # 끊겨도 현재 노드 탭은 즉시 정리되어야 한다.
            await self.emit_lock_state(memo_id, None)

    async def _leave_memo(self, sid: str, memo_id: str) -> None:
        room_name = f"memo:{memo_id}"
        await self.sio.leave_room(sid, room_name)
        self.rooms[memo_id].discard(sid)
        if not self.rooms[memo_id]:
            self.rooms.pop(memo_id, None)

        self.socket_memos[sid].discard(memo_id)
        if memo_id in self.socket_locks.get(sid, set()):
            await self._release_lock(sid, memo_id)

    async def _join_project(self, sid: str, data: dict | None) -> bool:
        project_id = _get_id(data, "projectId")
        user_info = self.users.get(sid)
        if (
            not project_id
            or not user_info
            or not await asyncio.to_thread(
                self._can_access_project, project_id, user_info, False
            )
        ):
            if project_id:
                await self.sio.emit(
                    "projectAccessDenied", {"projectId": project_id}, to=sid
                )
            return False

        await self.sio.enter_room(sid, f"project:{project_id}")
        self.socket_projects[sid].add(project_id)
        share = self.screen_shares.get(project_id)
        await self.sio.emit(
            "screenShareStatus",
            {
                "projectId": project_id,
                "isSharing": bool(share),
                "sharer": (
                    {
                        "userId": share["userId"],
                        "displayName": share["displayName"],
                    }
                    if share
                    else None
                ),
            },
            to=sid,
        )
        return True

    async def _start_screen_share(self, sid: str, data: dict | None) -> bool:
        project_id = _get_id(data, "projectId")
        user_info = self.users.get(sid)
        if (
            not project_id
            or not user_info
            or project_id not in self.socket_projects.get(sid, set())
            or not await asyncio.to_thread(
                self._can_access_project, project_id, user_info, True
            )
        ):
            if project_id:
                await self.sio.emit(
                    "screenShareDenied", {"projectId": project_id}, to=sid
                )
            return False

        existing = self.screen_shares.get(project_id)
        if existing and existing["socketId"] != sid:
            await self.sio.emit(
                "screenShareDenied", {"projectId": project_id}, to=sid
            )
            return False

        self.screen_shares[project_id] = {
            "userId": user_info["user_id"],
            "displayName": user_info["display_name"],
            "socketId": sid,
        }
        await self.sio.emit(
            "screenShareStarted",
            {
                "projectId": project_id,
                "sharer": {
                    "userId": user_info["user_id"],
                    "displayName": user_info["display_name"],
                },
            },
            room=f"project:{project_id}",
        )
        return True

    async def _stop_screen_share(self, project_id: str) -> None:
        self.screen_shares.pop(project_id, None)
        await self.sio.emit(
            "screenShareStopped",
            {"projectId": project_id},
            room=f"project:{project_id}",
        )

    async def _broadcast_memo_update(self, sid: str, data: dict | None) -> bool:
        memo_id = _get_id(data, "memoId")
        user_info = self.users.get(sid)
        if (
            not memo_id
            or not user_info
            or memo_id not in self.socket_memos.get(sid, set())
            or not self.socket_owns_valid_lease(sid, memo_id)
            or not await asyncio.to_thread(
                self._can_access_memo, memo_id, user_info, True
            )
        ):
            return False
        await self.sio.emit(
            "memoContentUpdated",
            {
                "memoId": memo_id,
                "content": data.get("content", ""),
                "title": data.get("title"),
                "origin": "peer-editor",
            },
            room=f"memo:{memo_id}",
            skip_sid=sid,
        )
        return True

    # -- 동기화 알림 --------------------------------------------------------

    async def emit_sync_changed(self, account_id: str, max_seq: int) -> None:
        """전역 룸 `sync:<accountId>` 에 "새 변경이 있다"고 알린다.

        폴링(60초)을 남겨 두는 이유: `change_log` 는 DB 트리거가 채우므로 API 밖 변경
        (수동 SQL·마이그레이션 스크립트)까지 잡히지만, 이 알림은 API 레이어에서 나가므로
        그런 변경에는 뜨지 않는다.
        """
        if not self.available:
            return
        await self.sio.emit(
            "syncChanged",
            {"accountId": account_id, "maxSeq": max_seq},
            room=sync_room(account_id),
        )

    async def emit_sync_applied(self, payload: dict) -> None:
        """pull 적용 결과를 로컬 브라우저에 재발행한다 (새로고침 없이 갱신)."""
        if not self.available:
            return
        await self.sio.emit("syncApplied", payload)

    async def emit_memo_pulled(
        self, memo_id: str, content: str | None, title: str | None, updated_at_utc: str | None
    ) -> None:
        """pull 로 바뀐 메모를 해당 메모 룸에 알린다.

        `origin` 이 `sync-pull` 이므로 프론트는 편집 중 버퍼를 덮지 않고
        "원격에서 변경됨 — 비교" 배너를 띄울 수 있다.
        """
        if not self.available:
            return
        await self.sio.emit(
            "memoContentUpdated",
            {
                "memoId": memo_id,
                "content": content or "",
                "title": title,
                "origin": ORIGIN_SYNC_PULL,
                "updatedAtUtc": updated_at_utc,
            },
            room=f"memo:{memo_id}",
        )

    async def emit_lock_state(self, memo_id: str, holder: dict | None) -> None:
        if not self.available:
            return
        if holder is None:
            self._clear_socket_lease_state(memo_id)
            await self.sio.emit("memoUnlocked", {"memoId": memo_id}, room=f"memo:{memo_id}")
            return
        owner_key = str(holder.get("ownerKey") or "")
        self._reconcile_socket_lease_state(memo_id, owner_key, holder.get("leaseToken"))
        local_owner_prefixes = (
            f"{SYNC_CLIENT_ID}:",
            f"peer:{SYNC_CLIENT_ID}:",
        )
        owner_sid = next(
            (
                owner_key[len(prefix) :]
                for prefix in local_owner_prefixes
                if owner_key.startswith(prefix)
            ),
            None,
        )
        await self.sio.emit(
            "memoLocked",
            {
                "memoId": memo_id,
                "displayName": holder.get("displayName"),
                "userId": holder.get("userId"),
            },
            room=f"memo:{memo_id}",
            skip_sid=owner_sid,
        )

    def _can_access_memo(
        self, memo_id: str, user_info: dict, require_write: bool
    ) -> bool:
        user = {
            "id": user_info["user_id"],
            "permission": user_info.get("permission"),
        }
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT project_id FROM memos "
                        "WHERE id = %s AND deleted_at IS NULL",
                        (memo_id,),
                    )
                    memo = cursor.fetchone()
                    if not memo:
                        return False
                    if require_write:
                        return can_write_project(cursor, memo["project_id"], user)
                    return check_project_membership(
                        cursor, memo["project_id"], user
                    )
        except Exception:  # noqa: BLE001 - 소켓 권한 판정은 fail-closed
            logger.exception("memo socket authorization failed")
            return False

    def _can_access_project(
        self, project_id: str, user_info: dict, require_write: bool
    ) -> bool:
        user = {
            "id": user_info["user_id"],
            "permission": user_info.get("permission"),
        }
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM projects "
                        "WHERE id = %s AND deleted_at IS NULL",
                        (project_id,),
                    )
                    if not cursor.fetchone():
                        return False
                    checker = can_write_project if require_write else check_project_membership
                    return checker(cursor, project_id, user)
        except Exception:  # noqa: BLE001 - 소켓 권한 판정은 fail-closed
            logger.exception("project socket authorization failed")
            return False

    def _lease_tokens_for(self, sid: str) -> dict[str, tuple[str, float]]:
        tokens = getattr(self, "socket_lease_tokens", None)
        if tokens is None:
            tokens = defaultdict(dict)
            self.socket_lease_tokens = tokens
        return tokens[sid]

    def socket_owns_valid_lease(self, sid: str, memo_id: str) -> bool:
        user_info = self.users.get(sid)
        if (
            not user_info
            or memo_id not in self.socket_memos.get(sid, set())
            or memo_id not in self.socket_locks.get(sid, set())
        ):
            return False
        token_entry = self._lease_tokens_for(sid).get(memo_id)
        if token_entry is None or token_entry[1] <= monotonic():
            return False
        token = token_entry[0]
        if runs_sync_daemon():
            return True
        return self.lock_registry.validate(
            memo_id,
            token,
            user_id=user_info["user_id"],
            owner_key=self._owner_key(sid),
        )

    def validate_rest_lease(
        self, memo_id: str, user_id: str, lease_token: str | None
    ) -> bool:
        if not lease_token:
            return False
        for sid, user_info in self.users.items():
            if user_info.get("user_id") != user_id:
                continue
            token_entry = self._lease_tokens_for(sid).get(memo_id)
            if (
                token_entry
                and token_entry[0] == lease_token
                and token_entry[1] > monotonic()
                and self.socket_owns_valid_lease(sid, memo_id)
            ):
                return True
        return False

    def _clear_socket_lease_state(self, memo_id: str) -> None:
        for sid in list(self.socket_locks):
            self.socket_locks[sid].discard(memo_id)
            self._lease_tokens_for(sid).pop(memo_id, None)

    def _reconcile_socket_lease_state(
        self, memo_id: str, owner_key: str, lease_token: object
    ) -> None:
        token = str(lease_token or "")
        for sid in list(self.socket_locks):
            expected_owner = self._owner_key(sid)
            token_entry = self._lease_tokens_for(sid).get(memo_id)
            is_owner = owner_key in {expected_owner, f"peer:{expected_owner}"}
            if (
                memo_id in self.socket_locks[sid]
                and (not is_owner or not token_entry or token_entry[0] != token)
            ):
                self.socket_locks[sid].discard(memo_id)
                self._lease_tokens_for(sid).pop(memo_id, None)

    def _on_lock_change(self, memo_id: str, holder: dict | None, previous: dict | None) -> None:
        """모든 역할에서 canonical 락 변화를 로컬 탭과 피어에 반영한다."""
        loop = self._loop
        if loop is None or not self.available:
            return
        coro = self._broadcast_lock_change(memo_id, holder)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:  # pragma: no cover - 루프 종료 중
            coro.close()

    async def _broadcast_lock_change(
        self, memo_id: str, holder: dict | None
    ) -> None:
        await self.emit_lock_state(memo_id, holder)
        for account_id in set(self.peer_sockets.values()):
            await self.sio.emit(
                "syncLockChanged",
                {"memoId": memo_id, "holder": holder},
                room=sync_room(account_id),
            )


_realtime_server: MemoRealtimeServer | None = None


def set_realtime_server(server: MemoRealtimeServer) -> None:
    global _realtime_server
    _realtime_server = server


def get_realtime_server() -> MemoRealtimeServer | None:
    return _realtime_server


def _verify_peer_credential(headers: dict[str, str]) -> str:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            account_id = resolve_account_id(cursor)
    principal = authenticate_headers(headers, account_id)
    return principal.account_id


def _extract_headers(environ: dict) -> dict[str, str]:
    """ASGI scope 또는 WSGI 스타일 environ 에서 헤더를 소문자 dict 로 뽑는다."""
    headers: dict[str, str] = {}
    scope = environ.get("asgi.scope") or {}
    for key, value in scope.get("headers") or []:
        headers[key.decode("latin1").lower()] = value.decode("latin1")
    if headers:
        return headers

    for key, value in environ.items():
        if key.startswith("HTTP_") and isinstance(value, str):
            headers[key[5:].replace("_", "-").lower()] = value
    return headers


def _get_id(data: dict | None, key: str) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return str(value) if value else None
