import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID, uuid4

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
    from ..utils import (
        can_read_memo,
        can_write_memo,
        can_write_project,
        check_project_membership,
    )
    from .lock_registry import ReservationConflict, get_lock_registry
    from .task_rollup_limits import MAX_ROLLUP_MEMOS
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
    from utils import can_read_memo, can_write_memo, can_write_project, check_project_membership
    from services.lock_registry import ReservationConflict, get_lock_registry
    from services.task_rollup_limits import MAX_ROLLUP_MEMOS
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

    def __init__(self, message: str, code: str = "delegation_unavailable"):
        super().__init__(message)
        self.code = code


def _peer_error_code(exc: SyncPeerError) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        nested = detail.get("detail")
        if isinstance(nested, dict) and nested.get("code"):
            return str(nested["code"])
        if detail.get("code"):
            return str(detail["code"])
    return "reservation_rejected"


class TaskRollupReservationError(RuntimeError):
    """Safe failure raised before task-rollup SQL work may begin."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TaskRollupReservation:
    """Renewable fence over one canonical, already-owned memo lease set."""

    def __init__(
        self,
        server: "MemoRealtimeServer",
        *,
        reservation_id: str,
        operation_id: str,
        user_id: str,
        permission: str,
        owner_key: str,
        leases: tuple[dict, ...],
        delegated: bool,
    ) -> None:
        self._server = server
        self.reservation_id = reservation_id
        self.operation_id = operation_id
        self.user_id = user_id
        self.permission = permission
        self.owner_key = owner_key
        self.leases = leases
        self.memo_ids = tuple(str(lease["memoId"]) for lease in leases)
        self.delegated = delegated
        self._state_lock = Lock()
        self._valid_until = monotonic() + max(SYNC_LOCK_TTL_SECONDS * 0.75, 0.05)
        self._valid = True
        self._released = False
        self._renewal_task: asyncio.Task | None = None

    def start(self) -> None:
        self._renewal_task = asyncio.create_task(
            self._renew_loop(),
            name=f"task-rollup-reservation:{self.reservation_id}",
        )

    def invalidate(self) -> None:
        with self._state_lock:
            self._valid = False

    def _mark_renewed(self) -> None:
        with self._state_lock:
            if not self._released:
                self._valid = True
                self._valid_until = monotonic() + max(
                    SYNC_LOCK_TTL_SECONDS * 0.75, 0.05
                )

    def _locally_valid(self) -> bool:
        with self._state_lock:
            return (
                self._valid
                and not self._released
                and self._valid_until > monotonic()
            )

    def validate(self) -> bool:
        """Synchronous canonical final fence for SQL worker threads."""
        if not self.validate_local():
            return False
        if self.delegated:
            return self._server._validate_task_rollup_reservation(self)
        return True

    def validate_local(self) -> bool:
        """Fast post-statement fence; delegated callers still need final validate()."""
        if not self._locally_valid():
            return False
        if self.delegated:
            return True
        return self._server.lock_registry.validate_batch(
            reservation_id=self.reservation_id,
            operation_id=self.operation_id,
            owner_key=self.owner_key,
            user_id=self.user_id,
            leases=self.leases,
        )

    async def _renew_loop(self) -> None:
        interval = max(SYNC_LOCK_TTL_SECONDS / 3, 0.05)
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._locally_valid():
                    self.invalidate()
                    return
                await self._server._renew_task_rollup_reservation(self)
                self._mark_renewed()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - any lost renewal invalidates the SQL fence
            self.invalidate()
            logger.exception(
                "task rollup reservation renewal failed: reservation=%s",
                self.reservation_id,
            )

    async def release(self) -> None:
        """Idempotently release the reservation, never the editor leases."""
        with self._state_lock:
            if self._released:
                return
            self._released = True
            self._valid = False
        renewal_task = self._renewal_task
        if renewal_task is not None and renewal_task is not asyncio.current_task():
            renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await renewal_task
        try:
            await self._server._release_task_rollup_reservation(self)
        except Exception:  # noqa: BLE001 - canonical TTL is the crash-safe cleanup
            logger.exception(
                "task rollup reservation release deferred to TTL: reservation=%s",
                self.reservation_id,
            )
        finally:
            self._server._task_rollup_reservations.pop(self.reservation_id, None)

    async def __aenter__(self) -> "TaskRollupReservation":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        await self.release()


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
        self.socket_lease_tokens: dict[
            str, dict[str, tuple[str, float] | tuple[str, float, int | None]]
        ] = defaultdict(dict)
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
        self._task_rollup_reservations: dict[str, TaskRollupReservation] = {}
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
            return result
        except SyncPeerUnreachable as exc:
            runtime.mark_offline(f"lock delegation failed: {exc}")
            self._lock_delegation_quarantine_until = max(
                getattr(self, "_lock_delegation_quarantine_until", 0.0),
                monotonic() + SYNC_LOCK_TTL_SECONDS,
            )
            raise LockDelegationError("sync peer is unreachable") from exc
        except SyncPeerError as exc:
            logger.warning("remote lock delegation rejected: %s", exc)
            raise LockDelegationError(
                "sync peer rejected the lock request", _peer_error_code(exc)
            ) from exc

    async def _resolve_lock_holder(self, memo_id: str) -> dict | None:
        delegated = await self._delegate_lock(get_sync_peer().lock_holder, memo_id)
        if delegated is not None:
            return delegated.get("holder")
        return self.lock_registry.holder(memo_id)

    def _rollup_reservations(self) -> dict[str, TaskRollupReservation]:
        reservations = getattr(self, "_task_rollup_reservations", None)
        if reservations is None:
            reservations = {}
            self._task_rollup_reservations = reservations
        return reservations

    @staticmethod
    def _normalize_task_rollup_leases(leases) -> tuple[dict, ...]:  # noqa: ANN001
        normalized: list[dict] = []
        for lease in leases or []:
            if hasattr(lease, "model_dump"):
                raw = lease.model_dump()
            elif isinstance(lease, dict):
                raw = lease
            else:
                raw = {
                    "memoId": getattr(lease, "memoId", None)
                    or getattr(lease, "memo_id", None),
                    "leaseToken": getattr(lease, "token", None)
                    or getattr(lease, "leaseToken", None)
                    or getattr(lease, "lease_token", None),
                }
            memo_id = str(raw.get("memoId") or raw.get("memo_id") or "").strip()
            lease_token = str(
                raw.get("token")
                or raw.get("leaseToken")
                or raw.get("lease_token")
                or ""
            ).strip()
            if not memo_id or not lease_token:
                raise TaskRollupReservationError(
                    "invalid_lease_manifest",
                    "every lease needs a memo id and token",
                )
            normalized.append({"memoId": memo_id, "leaseToken": lease_token})
        normalized.sort(key=lambda item: item["memoId"])
        if len(normalized) > MAX_ROLLUP_MEMOS:
            raise TaskRollupReservationError(
                "too_many_memo_leases", "task rollup memo lease limit exceeded"
            )
        if not normalized:
            raise TaskRollupReservationError(
                "empty_lease_manifest", "at least one lease is required"
            )
        memo_ids = [item["memoId"] for item in normalized]
        if len(set(memo_ids)) != len(memo_ids):
            raise TaskRollupReservationError(
                "duplicate_memo_lease", "a memo may appear only once"
            )
        return tuple(normalized)

    def _reservation_owner_sid(
        self, user_id: str, leases: tuple[dict, ...]
    ) -> str:
        candidates = {
            sid
            for sid, user_info in self.users.items()
            if user_info.get("user_id") == user_id
        }
        for lease in leases:
            memo_id = lease["memoId"]
            token = lease["leaseToken"]
            candidates = {
                sid
                for sid in candidates
                if memo_id in self.socket_memos.get(sid, set())
                and memo_id in self.socket_locks.get(sid, set())
                and (entry := self._lease_tokens_for(sid).get(memo_id)) is not None
                and entry[0] == token
                and entry[1] > monotonic()
                and self.socket_owns_valid_lease(sid, memo_id)
            }
            if not candidates:
                break
        if len(candidates) != 1:
            raise TaskRollupReservationError(
                "lease_owner_mismatch",
                "all leases must be owned by one authenticated editor socket",
            )
        return next(iter(candidates))

    @staticmethod
    def _validated_reservation_leases(
        payload: dict,
        *,
        reservation_id: str,
        operation_id: str,
        user_id: str,
        owner_key: str,
        claims: tuple[dict, ...],
        delegated: bool,
    ) -> tuple[dict, ...]:
        reservation = payload.get("reservation", payload)
        if not isinstance(reservation, dict):
            raise TaskRollupReservationError(
                "invalid_reservation_response", "lock service returned an invalid response"
            )
        expected_owner = f"peer:{owner_key}" if delegated else owner_key
        returned = reservation.get("leases")
        if (
            reservation.get("reservationId") != reservation_id
            or reservation.get("operationId") != operation_id
            or reservation.get("userId") != user_id
            or reservation.get("ownerKey") != expected_owner
            or not isinstance(returned, list)
        ):
            raise TaskRollupReservationError(
                "reservation_identity_mismatch",
                "lock service changed the reservation identity",
            )
        expected = [(item["memoId"], item["leaseToken"]) for item in claims]
        fenced: list[dict] = []
        for item in returned:
            try:
                generation = int(item["generation"])
            except (KeyError, TypeError, ValueError) as exc:
                raise TaskRollupReservationError(
                    "invalid_reservation_response",
                    "lock service omitted a lease generation",
                ) from exc
            fenced.append(
                {
                    "memoId": str(item.get("memoId") or ""),
                    "leaseToken": str(item.get("leaseToken") or ""),
                    "generation": generation,
                }
            )
        fenced.sort(key=lambda item: item["memoId"])
        actual = [(item["memoId"], item["leaseToken"]) for item in fenced]
        expected_generations = {
            item["memoId"]: item.get("generation") for item in claims
        }
        if (
            actual != expected
            or any(item["generation"] < 1 for item in fenced)
            or any(
                expected_generations[item["memoId"]] is not None
                and item["generation"] != expected_generations[item["memoId"]]
                for item in fenced
            )
        ):
            raise TaskRollupReservationError(
                "reservation_fence_mismatch",
                "lock service did not reserve the exact lease set",
            )
        return tuple(fenced)

    async def _call_canonical_reservation(self, call, *args) -> dict:
        """Call an existing remote reservation without ever falling back locally."""
        peer = get_sync_peer()
        if not peer.configured:
            raise LockDelegationError("sync peer is not configured")
        try:
            result = await asyncio.to_thread(call, *args)
            return result
        except SyncPeerUnreachable as exc:
            get_sync_runtime().mark_offline(
                f"lock reservation delegation failed: {exc}"
            )
            self._lock_delegation_quarantine_until = max(
                getattr(self, "_lock_delegation_quarantine_until", 0.0),
                monotonic() + SYNC_LOCK_TTL_SECONDS,
            )
            raise LockDelegationError(
                "remote reservation outcome is uncertain"
            ) from exc
        except SyncPeerError as exc:
            raise LockDelegationError(
                "sync peer rejected the reservation request", _peer_error_code(exc)
            ) from exc

    async def _cleanup_uncertain_remote_reservation(
        self,
        *,
        reservation_id: str,
        operation_id: str,
        owner_key: str,
        user_id: str,
    ) -> None:
        """Best-effort exact cleanup after a successful but malformed reserve reply."""
        quarantine_until = monotonic() + SYNC_LOCK_TTL_SECONDS
        self._lock_delegation_quarantine_until = max(
            getattr(self, "_lock_delegation_quarantine_until", 0.0),
            quarantine_until,
        )
        try:
            result = await self._call_canonical_reservation(
                get_sync_peer().lock_reservation_release,
                reservation_id,
                operation_id,
                owner_key,
                user_id,
            )
        except LockDelegationError:
            self._lock_delegation_quarantine_until = max(
                getattr(self, "_lock_delegation_quarantine_until", 0.0),
                quarantine_until,
            )
            return
        if not bool(result.get("released")):
            self._lock_delegation_quarantine_until = max(
                getattr(self, "_lock_delegation_quarantine_until", 0.0),
                quarantine_until,
            )

    async def reserve_task_rollup(
        self, user_id: str, operation_id: str, leases
    ) -> TaskRollupReservation:  # noqa: ANN001
        """Reserve an exact already-owned lease set without acquiring any lease."""
        if not self.available:
            raise TaskRollupReservationError(
                "memo_realtime_unavailable", "realtime lease service is unavailable"
            )
        try:
            UUID(str(operation_id))
        except (TypeError, ValueError) as exc:
            raise TaskRollupReservationError(
                "invalid_operation_id", "operation id must be a UUID"
            ) from exc
        claims = self._normalize_task_rollup_leases(leases)
        sid = self._reservation_owner_sid(str(user_id), claims)
        claims = tuple(
            {
                **claim,
                "generation": (
                    entry[2]
                    if len(entry := self._lease_tokens_for(sid)[claim["memoId"]]) > 2
                    else None
                ),
            }
            for claim in claims
        )
        wire_claims = [
            {"memoId": claim["memoId"], "leaseToken": claim["leaseToken"]}
            for claim in claims
        ]
        owner_key = self._owner_key(sid)
        reservation_id = str(uuid4())
        try:
            delegated_result = await self._delegate_lock(
                get_sync_peer().lock_reservation_reserve,
                reservation_id,
                str(operation_id),
                owner_key,
                str(user_id),
                wire_claims,
            )
            delegated = delegated_result is not None
            if delegated:
                result = delegated_result
            else:
                result = self.lock_registry.reserve_batch(
                    reservation_id=reservation_id,
                    operation_id=str(operation_id),
                    owner_key=owner_key,
                    user_id=str(user_id),
                    leases=wire_claims,
                )
        except ReservationConflict as exc:
            raise TaskRollupReservationError(exc.code, str(exc)) from exc
        except LockDelegationError as exc:
            raise TaskRollupReservationError(
                (
                    "reservation_delegation_unavailable"
                    if exc.code == "delegation_unavailable"
                    else exc.code
                ),
                str(exc),
            ) from exc
        try:
            fenced = self._validated_reservation_leases(
                result,
                reservation_id=reservation_id,
                operation_id=str(operation_id),
                user_id=str(user_id),
                owner_key=owner_key,
                claims=claims,
                delegated=delegated,
            )
        except TaskRollupReservationError:
            if delegated:
                await self._cleanup_uncertain_remote_reservation(
                    reservation_id=reservation_id,
                    operation_id=str(operation_id),
                    owner_key=owner_key,
                    user_id=str(user_id),
                )
            else:
                self.lock_registry.release_batch(
                    reservation_id=reservation_id,
                    operation_id=str(operation_id),
                    owner_key=owner_key,
                    user_id=str(user_id),
                )
            raise
        handle = TaskRollupReservation(
            self,
            reservation_id=reservation_id,
            operation_id=str(operation_id),
            user_id=str(user_id),
            permission=str(self.users[sid].get("permission") or "user"),
            owner_key=owner_key,
            leases=fenced,
            delegated=delegated,
        )
        self._rollup_reservations()[reservation_id] = handle
        handle.start()
        return handle

    async def _renew_task_rollup_reservation(
        self, reservation: TaskRollupReservation
    ) -> None:
        claims = [
            {"memoId": lease["memoId"], "leaseToken": lease["leaseToken"]}
            for lease in reservation.leases
        ]
        if reservation.delegated:
            result = await self._call_canonical_reservation(
                get_sync_peer().lock_reservation_renew,
                reservation.reservation_id,
                reservation.operation_id,
                reservation.owner_key,
                reservation.user_id,
                claims,
            )
            fenced = self._validated_reservation_leases(
                result,
                reservation_id=reservation.reservation_id,
                operation_id=reservation.operation_id,
                user_id=reservation.user_id,
                owner_key=reservation.owner_key,
                claims=tuple(claims),
                delegated=True,
            )
            if fenced != reservation.leases:
                raise TaskRollupReservationError(
                    "reservation_fence_mismatch", "lease generation changed"
                )
            return

        user_info = {
            "user_id": reservation.user_id,
            "permission": reservation.permission,
        }
        access = await asyncio.gather(
            *(
                asyncio.to_thread(self._can_access_memo, memo_id, user_info, True)
                for memo_id in reservation.memo_ids
            )
        )
        if not all(access):
            self.lock_registry.revoke_batch(
                reservation_id=reservation.reservation_id,
                operation_id=reservation.operation_id,
                owner_key=reservation.owner_key,
                user_id=reservation.user_id,
            )
            raise TaskRollupReservationError(
                "reservation_access_revoked", "memo write access was revoked"
            )
        try:
            renewed = self.lock_registry.renew_batch(
                reservation_id=reservation.reservation_id,
                operation_id=reservation.operation_id,
                owner_key=reservation.owner_key,
                user_id=reservation.user_id,
                leases=claims,
            )
        except ReservationConflict as exc:
            raise TaskRollupReservationError(exc.code, str(exc)) from exc
        fenced = self._validated_reservation_leases(
            renewed,
            reservation_id=reservation.reservation_id,
            operation_id=reservation.operation_id,
            user_id=reservation.user_id,
            owner_key=reservation.owner_key,
            claims=tuple(claims),
            delegated=False,
        )
        if fenced != reservation.leases:
            raise TaskRollupReservationError(
                "reservation_fence_mismatch", "lease generation changed"
            )

    async def _release_task_rollup_reservation(
        self, reservation: TaskRollupReservation
    ) -> None:
        if reservation.delegated:
            await self._call_canonical_reservation(
                get_sync_peer().lock_reservation_release,
                reservation.reservation_id,
                reservation.operation_id,
                reservation.owner_key,
                reservation.user_id,
            )
            return
        self.lock_registry.release_batch(
            reservation_id=reservation.reservation_id,
            operation_id=reservation.operation_id,
            owner_key=reservation.owner_key,
            user_id=reservation.user_id,
        )

    def _validate_task_rollup_reservation(
        self, reservation: TaskRollupReservation
    ) -> bool:
        """Worker-thread remote fence check; unknown outcomes always fail closed."""
        claims = [dict(lease) for lease in reservation.leases]
        peer = get_sync_peer()
        if not peer.configured:
            reservation.invalidate()
            return False
        try:
            result = peer.lock_reservation_validate(
                reservation.reservation_id,
                reservation.operation_id,
                reservation.owner_key,
                reservation.user_id,
                claims,
            )
        except SyncPeerUnreachable as exc:
            get_sync_runtime().mark_offline(
                f"lock reservation validation failed: {exc}"
            )
            self._lock_delegation_quarantine_until = max(
                getattr(self, "_lock_delegation_quarantine_until", 0.0),
                monotonic() + SYNC_LOCK_TTL_SECONDS,
            )
            reservation.invalidate()
            return False
        except SyncPeerError:
            reservation.invalidate()
            return False
        except Exception:  # noqa: BLE001 - final SQL fence must fail closed
            self._lock_delegation_quarantine_until = max(
                getattr(self, "_lock_delegation_quarantine_until", 0.0),
                monotonic() + SYNC_LOCK_TTL_SECONDS,
            )
            reservation.invalidate()
            logger.exception(
                "task rollup canonical validation failed: reservation=%s",
                reservation.reservation_id,
            )
            return False
        valid = bool(result.get("valid"))
        if not valid:
            reservation.invalidate()
        return valid

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
            await self._join_memo(sid, data)

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
            (
                int(holder["generation"])
                if holder.get("generation") is not None
                else None
            ),
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

    async def _join_memo(self, sid: str, data: dict | None) -> bool:
        memo_id = _get_id(data, "memoId")
        user_info = self.users.get(sid)
        can_read = bool(
            memo_id
            and user_info
            and await asyncio.to_thread(
                self._can_access_memo, memo_id, user_info, False
            )
        )
        if not can_read:
            if memo_id:
                disconnect_after_denial = False
                if memo_id in self.socket_memos.get(sid, set()):
                    disconnect_after_denial = await self._revoke_memo_access(
                        sid, memo_id
                    )
                try:
                    await self.sio.emit(
                        "memoAccessDenied", {"memoId": memo_id}, to=sid
                    )
                except Exception:
                    if not disconnect_after_denial:
                        raise
                    logger.debug(
                        "revoked memo denial raced with closed socket: sid=%s memo=%s",
                        sid,
                        memo_id,
                    )
                finally:
                    if disconnect_after_denial:
                        # 룸 퇴장이 두 번 모두 실패하면 실제 manager 구독 여부를
                        # 확인할 수 없다. denial 전송을 먼저 시도한 뒤 연결 자체를
                        # 끊어 이후 memoContentUpdated 수신을 fail-closed 한다.
                        await self._disconnect_browser_socket(sid)
            return False

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
        return True

    async def _revoke_memo_access(self, sid: str, memo_id: str) -> bool:
        """읽기 권한이 회수된 기존 구독을 정리한다.

        반환값이 참이면 Socket.IO manager에서도 퇴장을 확인하지 못했으므로 호출자가
        denial 전송 뒤 소켓을 끊어야 한다.
        """
        room_eviction_failed = False
        try:
            await self._leave_memo(sid, memo_id)
        except Exception:  # noqa: BLE001 - denial must still reach the client
            logger.exception(
                "failed to fully leave revoked memo room: sid=%s memo=%s",
                sid,
                memo_id,
            )
            try:
                await self.sio.leave_room(sid, f"memo:{memo_id}")
            except Exception:  # noqa: BLE001 - caller disconnects fail-closed
                room_eviction_failed = True
                logger.exception(
                    "failed to leave revoked Socket.IO room: sid=%s memo=%s",
                    sid,
                    memo_id,
                )
            if memo_id in self.socket_locks.get(sid, set()):
                try:
                    await self._release_lock(sid, memo_id)
                except Exception:  # noqa: BLE001 - local lease is revoked below
                    logger.exception(
                        "failed to release revoked memo lock: sid=%s memo=%s",
                        sid,
                        memo_id,
                    )
        finally:
            # 위임 락 해제가 불확실해도 이 소켓이 stale lease를 계속 제시하지
            # 못하게 한다. 원격 canonical lease는 TTL로 만료된다.
            self.rooms[memo_id].discard(sid)
            if not self.rooms[memo_id]:
                self.rooms.pop(memo_id, None)
            self.socket_memos[sid].discard(memo_id)
            self.socket_locks[sid].discard(memo_id)
            self._lease_tokens_for(sid).pop(memo_id, None)
        return room_eviction_failed

    async def revoke_project_access(self, project_ids: list[str]) -> None:
        """프로젝트 tombstone 직후 stale project/memo room과 lease를 즉시 제거한다."""
        targets = set(project_ids)
        for sid, user_info in list(self.users.items()):
            for project_id in targets & set(self.socket_projects.get(sid, set())):
                if await asyncio.to_thread(
                    self._can_access_project, project_id, user_info, False
                ):
                    continue
                try:
                    await self.sio.leave_room(sid, f"project:{project_id}")
                finally:
                    self.socket_projects[sid].discard(project_id)
                await self.sio.emit(
                    "projectAccessDenied", {"projectId": project_id}, to=sid
                )

            for memo_id in list(self.socket_memos.get(sid, set())):
                can_read = await asyncio.to_thread(
                    self._can_access_memo, memo_id, user_info, False
                )
                if can_read:
                    can_write = await asyncio.to_thread(
                        self._can_access_memo, memo_id, user_info, True
                    )
                    if not can_write and memo_id in self.socket_locks.get(sid, set()):
                        await self._release_lock(sid, memo_id)
                    continue
                disconnect = await self._revoke_memo_access(sid, memo_id)
                try:
                    await self.sio.emit(
                        "memoAccessDenied", {"memoId": memo_id}, to=sid
                    )
                finally:
                    if disconnect:
                        await self._disconnect_browser_socket(sid)
                        break
        # Delegated peer owners are not members of local memo rooms. Recheck
        # canonical registry holders as well so a role tombstone immediately
        # tears down the exact reservation fence instead of waiting for TTL.
        for memo_id in list(self.lock_registry.snapshot()):
            await self._force_release_revoked_registry_lock(memo_id)

    async def revalidate_memo_access(self, memo_id: str) -> None:
        """메모 이동 직후 대상 프로젝트 권한이 없는 구독/lease를 즉시 제거한다."""
        for sid in list(self.rooms.get(memo_id, set())):
            user_info = self.users.get(sid)
            can_read = bool(
                user_info
                and await asyncio.to_thread(
                    self._can_access_memo, memo_id, user_info, False
                )
            )
            if can_read:
                can_write = await asyncio.to_thread(
                    self._can_access_memo, memo_id, user_info, True
                )
                if not can_write and memo_id in self.socket_locks.get(sid, set()):
                    await self._release_lock(sid, memo_id)
                continue
            disconnect = await self._revoke_memo_access(sid, memo_id)
            try:
                await self.sio.emit("memoAccessDenied", {"memoId": memo_id}, to=sid)
            finally:
                if disconnect:
                    await self._disconnect_browser_socket(sid)
        await self._force_release_revoked_registry_lock(memo_id)

    async def _force_release_revoked_registry_lock(self, memo_id: str) -> None:
        holder = self.lock_registry.holder(memo_id)
        if holder is None:
            return
        user_id = str(holder.get("userId") or "")
        if not user_id:
            self.lock_registry.release(memo_id, force=True)
            return
        permission = "user"
        owner_key = str(holder.get("ownerKey") or "")
        for reservation in self._rollup_reservations().values():
            if (
                reservation.owner_key in {owner_key, owner_key.removeprefix("peer:")}
                and memo_id in reservation.memo_ids
            ):
                permission = reservation.permission
                break
        if permission == "user":
            local_prefix = f"{SYNC_CLIENT_ID}:"
            local_owner = owner_key.removeprefix("peer:")
            if local_owner.startswith(local_prefix):
                sid = local_owner[len(local_prefix) :]
                permission = str(
                    (self.users.get(sid) or {}).get("permission") or "user"
                )
        can_write = await asyncio.to_thread(
            self._can_access_memo,
            memo_id,
            {"user_id": user_id, "permission": permission},
            True,
        )
        if not can_write:
            self.lock_registry.release(memo_id, owner_key, force=True)

    async def emit_projects_changed(self) -> None:
        """프로젝트/멤버/메모 배치 변경을 인증된 브라우저에 내용 없이 알린다."""
        for sid in list(self.users):
            try:
                await self.sio.emit("projectsChanged", {}, to=sid)
            except Exception:  # noqa: BLE001 - 한 stale socket이 다른 탭 알림을 막지 않는다
                logger.debug("projectsChanged emit raced with socket close: %s", sid)

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
        self._invalidate_changed_task_rollup_fences(memo_id, holder)
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

    def _invalidate_changed_task_rollup_fences(
        self, memo_id: str, holder: dict | None
    ) -> None:
        for reservation in list(self._rollup_reservations().values()):
            lease = next(
                (
                    item
                    for item in reservation.leases
                    if item["memoId"] == memo_id
                ),
                None,
            )
            if lease is None:
                continue
            expected_owners = {
                reservation.owner_key,
                f"peer:{reservation.owner_key}",
            }
            if (
                holder is None
                or holder.get("ownerKey") not in expected_owners
                or holder.get("leaseToken") != lease["leaseToken"]
                or holder.get("generation") != lease["generation"]
            ):
                reservation.invalidate()

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
                    checker = can_write_memo if require_write else can_read_memo
                    return checker(cursor, memo_id, user)
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

    def _lease_tokens_for(
        self, sid: str
    ) -> dict[str, tuple[str, float] | tuple[str, float, int | None]]:
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
