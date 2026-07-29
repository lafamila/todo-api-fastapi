import asyncio
import threading
import unittest
from collections import defaultdict
from unittest.mock import patch

from fastapi import HTTPException, Request, Response

from src.routers import memos
from src.services import realtime
from src.services import session_auth
from src.services.lock_registry import LockRegistry
from src.services.sync_peer import SyncPeerError, SyncPeerUnreachable


class LockRegistryLeaseTests(unittest.TestCase):
    def test_expired_lease_notifies_listeners(self) -> None:
        registry = LockRegistry(ttl_seconds=0.05)
        expired = threading.Event()
        changes: list[tuple[str, dict | None, dict | None]] = []

        def listener(memo_id, holder, previous):
            changes.append((memo_id, holder, previous))
            if holder is None:
                expired.set()

        registry.on_change(listener)
        acquired, _ = registry.acquire("memo-1", "owner-1", "user-1", "User")

        self.assertTrue(acquired)
        self.assertTrue(expired.wait(1))
        self.assertIsNone(registry.holder("memo-1"))
        self.assertEqual(changes[-1][0], "memo-1")
        self.assertIsNone(changes[-1][1])
        self.assertEqual(changes[-1][2]["ownerKey"], "owner-1")

    def test_same_owner_renewal_extends_lease(self) -> None:
        registry = LockRegistry(ttl_seconds=0.12)
        expired = threading.Event()
        registry.on_change(
            lambda _memo_id, holder, _previous: expired.set()
            if holder is None
            else None
        )
        registry.acquire("memo-1", "owner-1", "user-1", "User")
        threading.Event().wait(0.07)

        acquired, holder = registry.acquire(
            "memo-1", "owner-1", "user-1", "User"
        )

        self.assertTrue(acquired)
        self.assertEqual(holder["ownerKey"], "owner-1")
        self.assertFalse(expired.wait(0.07))
        self.assertIsNotNone(registry.holder("memo-1"))
        self.assertTrue(expired.wait(0.2))

    def test_renewal_preserves_token_and_generation_while_extending_ttl(self) -> None:
        registry = LockRegistry(ttl_seconds=60)
        acquired, first = registry.acquire(
            "memo-1", "owner-1", "user-1", "User"
        )
        renewed, second = registry.acquire(
            "memo-1", "owner-1", "user-1", "User"
        )

        self.assertTrue(acquired)
        self.assertTrue(renewed)
        self.assertEqual(first["leaseToken"], second["leaseToken"])
        self.assertEqual(first["generation"], second["generation"])
        self.assertTrue(
            registry.validate("memo-1", second["leaseToken"], user_id="user-1")
        )
        self.assertFalse(
            registry.validate("memo-1", second["leaseToken"], user_id="user-2")
        )

    def test_release_and_reacquire_rotates_token_and_generation(self) -> None:
        registry = LockRegistry(ttl_seconds=60)
        _, first = registry.acquire("memo-1", "owner-1", "user-1", "User")
        registry.release("memo-1", "owner-1")
        _, second = registry.acquire("memo-1", "owner-1", "user-1", "User")

        self.assertNotEqual(first["leaseToken"], second["leaseToken"])
        self.assertGreater(second["generation"], first["generation"])
        self.assertFalse(
            registry.validate("memo-1", first["leaseToken"], user_id="user-1")
        )

    def test_expiry_and_reacquire_rotates_token_and_generation(self) -> None:
        registry = LockRegistry(ttl_seconds=0.03)
        expired = threading.Event()
        registry.on_change(
            lambda _memo_id, holder, _previous: expired.set()
            if holder is None
            else None
        )
        _, first = registry.acquire("memo-1", "owner-1", "user-1", "User")
        self.assertTrue(expired.wait(1))
        _, second = registry.acquire("memo-1", "owner-1", "user-1", "User")

        self.assertNotEqual(first["leaseToken"], second["leaseToken"])
        self.assertGreater(second["generation"], first["generation"])


class _Runtime:
    def __init__(self, online: bool) -> None:
        self.online = online
        self.offline_reason: str | None = None
        self.last_error_kind = None if online else "offline"

    def mark_offline(self, reason: str) -> None:
        self.online = False
        self.offline_reason = reason
        self.last_error_kind = "offline"


class _Peer:
    configured = True

    def __init__(
        self, error: Exception | None = None, release_result: bool = True
    ) -> None:
        self.error = error
        self.release_result = release_result

    def lock_acquire(self, *_args):
        if self.error is not None:
            raise self.error
        return {
            "acquired": True,
            "holder": {
                "leaseToken": "delegated-token",
                "generation": 1,
            },
        }

    def lock_release(self, *_args):
        if self.error is not None:
            raise self.error
        return {"released": self.release_result}

    def lock_holder(self, *_args):
        if self.error is not None:
            raise self.error
        return {"holder": None}


class _Sio:
    def __init__(self) -> None:
        self.emits: list[tuple[str, dict, dict]] = []
        self.disconnects: list[str] = []

    async def emit(self, event: str, payload: dict, **kwargs) -> None:
        self.emits.append((event, payload, kwargs))

    async def enter_room(self, *_args) -> None:
        return None

    async def leave_room(self, *_args) -> None:
        return None

    async def disconnect(self, sid: str) -> None:
        self.disconnects.append(sid)


class RealtimeDelegationTests(unittest.IsolatedAsyncioTestCase):
    def _server(self) -> realtime.MemoRealtimeServer:
        server = realtime.MemoRealtimeServer.__new__(realtime.MemoRealtimeServer)
        server.users = {"sid-1": {"user_id": "user-1", "display_name": "User"}}
        server.socket_memos = defaultdict(set, {"sid-1": {"memo-1"}})
        server.socket_projects = defaultdict(set)
        server.socket_locks = defaultdict(set)
        server.socket_lease_tokens = defaultdict(dict)
        server.screen_shares = {}
        server.lock_registry = LockRegistry(ttl_seconds=60)
        server.sio = _Sio()
        server.available = True
        server.socket_sessions = {}
        server.session_sockets = defaultdict(set)
        server.socket_allows_offline = {}
        server.session_revalidation_tasks = {}
        server._can_access_memo = lambda *_args: True
        return server

    async def test_online_rejection_fails_closed_without_local_lock(self) -> None:
        server = self._server()
        runtime = _Runtime(online=True)
        peer = _Peer(SyncPeerError(409, "rejected"))

        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            await server._acquire_lock("sid-1", "memo-1", renewal=False)

        self.assertIsNone(server.lock_registry.holder("memo-1"))
        self.assertNotIn("memo-1", server.socket_locks["sid-1"])
        self.assertEqual(server.sio.emits[-1][0], "lockDenied")
        self.assertEqual(
            server.sio.emits[-1][1]["reason"], "delegation_unavailable"
        )

    async def test_unreachable_request_fails_closed_then_marks_offline(self) -> None:
        server = self._server()
        runtime = _Runtime(online=True)
        peer = _Peer(SyncPeerUnreachable("network down"))

        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            await server._acquire_lock("sid-1", "memo-1", renewal=False)
            await server._acquire_lock("sid-1", "memo-1", renewal=False)

        self.assertIsNone(server.lock_registry.holder("memo-1"))
        self.assertFalse(runtime.online)
        self.assertIn("network down", runtime.offline_reason or "")
        self.assertEqual(server.sio.emits[-1][1]["reason"], "delegation_unavailable")

    async def test_established_offline_mode_uses_local_renewable_lock(self) -> None:
        server = self._server()
        runtime = _Runtime(online=False)
        peer = _Peer(SyncPeerUnreachable("must not be called"))

        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            await server._acquire_lock("sid-1", "memo-1", renewal=False)
            await server._acquire_lock("sid-1", "memo-1", renewal=True)

        self.assertIsNotNone(server.lock_registry.holder("memo-1"))
        self.assertIn("memo-1", server.socket_locks["sid-1"])
        self.assertEqual(server.sio.emits[-1][0], "lockLeaseRenewed")

    async def test_uninitialized_sync_state_does_not_use_local_lock(self) -> None:
        server = self._server()
        runtime = _Runtime(online=False)
        runtime.last_error_kind = None
        peer = _Peer()

        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            await server._acquire_lock("sid-1", "memo-1", renewal=False)

        self.assertIsNone(server.lock_registry.holder("memo-1"))
        self.assertEqual(server.sio.emits[-1][1]["reason"], "delegation_unavailable")

    async def test_rebroadcast_skips_originating_local_socket(self) -> None:
        server = self._server()
        with patch.object(realtime, "SYNC_CLIENT_ID", "laptop"):
            await server.emit_lock_state(
                "memo-1",
                {
                    "ownerKey": "peer:laptop:sid-1",
                    "userId": "user-1",
                    "displayName": "User",
                },
            )

        event, _payload, kwargs = server.sio.emits[-1]
        self.assertEqual(event, "memoLocked")
        self.assertEqual(kwargs["skip_sid"], "sid-1")

    async def test_false_delegated_release_never_broadcasts_unlock(self) -> None:
        server = self._server()
        server.socket_locks["sid-1"].add("memo-1")
        server.socket_lease_tokens["sid-1"]["memo-1"] = (
            "delegated-token",
            realtime.monotonic() + 60,
        )
        runtime = _Runtime(online=True)
        peer = _Peer(release_result=False)

        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            await server._release_lock("sid-1", "memo-1")

        self.assertNotIn("memo-1", server.socket_locks["sid-1"])
        self.assertNotIn(
            "memoUnlocked", [event for event, _payload, _kwargs in server.sio.emits]
        )
        self.assertEqual(server.sio.emits[-1][0], "lockReleaseFailed")

    async def test_unlock_from_socket_without_ownership_is_ignored(self) -> None:
        server = self._server()
        runtime = _Runtime(online=True)
        peer = _Peer()

        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            await server._release_lock("sid-1", "memo-1")

        self.assertEqual(server.sio.emits, [])

    def test_rest_lease_requires_matching_user_socket_and_token(self) -> None:
        server = self._server()
        server.socket_locks["sid-1"].add("memo-1")
        server.socket_lease_tokens["sid-1"]["memo-1"] = (
            "delegated-token",
            realtime.monotonic() + 60,
        )

        with patch.object(realtime, "runs_sync_daemon", return_value=True):
            self.assertTrue(
                server.validate_rest_lease(
                    "memo-1", "user-1", "delegated-token"
                )
            )
            self.assertFalse(
                server.validate_rest_lease("memo-1", "user-1", "forged")
            )
            self.assertFalse(
                server.validate_rest_lease(
                    "memo-1", "different-user", "delegated-token"
                )
            )

    def test_memo_join_authorization_fails_closed_without_membership(self) -> None:
        server = self._server()

        class Cursor:
            def execute(self, *_args):
                return None

            def fetchone(self):
                return {"project_id": "project-1"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        class Connection:
            def cursor(self):
                return Cursor()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with (
            patch.object(realtime, "get_db_connection", return_value=Connection()),
            patch.object(
                realtime, "check_project_membership", return_value=False
            ),
        ):
            allowed = realtime.MemoRealtimeServer._can_access_memo(
                server,
                "memo-1",
                {"user_id": "user-1", "permission": "user"},
                False,
            )

        self.assertFalse(allowed)

    async def test_memo_update_broadcast_requires_joined_valid_lease(self) -> None:
        server = self._server()

        with patch.object(realtime, "runs_sync_daemon", return_value=True):
            self.assertFalse(
                await server._broadcast_memo_update(
                    "sid-1", {"memoId": "memo-1", "content": "forged"}
                )
            )
            server.socket_locks["sid-1"].add("memo-1")
            server.socket_lease_tokens["sid-1"]["memo-1"] = (
                "delegated-token",
                realtime.monotonic() + 60,
            )
            self.assertTrue(
                await server._broadcast_memo_update(
                    "sid-1", {"memoId": "memo-1", "content": "owned"}
                )
            )

        self.assertEqual(server.sio.emits[-1][0], "memoContentUpdated")

    async def test_foreign_project_uuid_cannot_join_or_start_screen_share(self) -> None:
        server = self._server()
        server._can_access_project = lambda *_args: False

        joined = await server._join_project(
            "sid-1", {"projectId": "foreign-project"}
        )
        started = await server._start_screen_share(
            "sid-1", {"projectId": "foreign-project"}
        )

        self.assertFalse(joined)
        self.assertFalse(started)
        self.assertNotIn(
            "foreign-project", server.socket_projects["sid-1"]
        )
        self.assertNotIn("foreign-project", server.screen_shares)
        self.assertEqual(
            [event for event, _payload, _kwargs in server.sio.emits],
            ["projectAccessDenied", "screenShareDenied"],
        )

    async def test_long_socket_refresh_failure_disconnects(self) -> None:
        server = self._server()

        class InvalidSessionService:
            async def validate_session_id(self, session_id, *, allow_offline):
                self.session_id = session_id
                self.allow_offline = allow_offline
                return None

        service = InvalidSessionService()
        server.session_service = service
        server.socket_sessions["sid-1"] = "session-1"
        server.session_sockets["session-1"].add("sid-1")
        server.socket_allows_offline["sid-1"] = False

        valid = await server._revalidate_browser_session("sid-1")

        self.assertFalse(valid)
        self.assertNotIn("sid-1", server.users)
        self.assertNotIn("sid-1", server.socket_sessions)
        self.assertEqual(server.sio.disconnects, ["sid-1"])
        self.assertEqual(service.session_id, "session-1")
        self.assertFalse(service.allow_offline)

    async def test_logout_invalidates_and_disconnects_session_socket(self) -> None:
        service = session_auth.TodoSessionService()
        session = session_auth.TodoSession(
            id="session-1",
            access_token="access",
            refresh_token="refresh",
            access_token_expires_at=9999999999,
            user={"id": "user-1", "permission": "user"},
        )
        service._store_session(session)
        service._revoke_refresh_token_safe = lambda _token: None  # type: ignore[method-assign]

        server = self._server()
        server.session_service = service
        server._loop = asyncio.get_running_loop()
        server.socket_sessions["sid-1"] = session.id
        server.session_sockets[session.id].add("sid-1")
        server.socket_allows_offline["sid-1"] = False
        service.on_session_invalidated(server._on_session_invalidated)

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/session/logout",
                "raw_path": b"/api/session/logout",
                "query_string": b"",
                "headers": [
                    (
                        b"cookie",
                        f"{service.cookie_name}={session.id}".encode(),
                    )
                ],
                "client": ("127.0.0.1", 41000),
                "server": ("127.0.0.1", 8000),
            }
        )

        await service.logout(request, Response())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertNotIn(session.id, service._sessions)
        self.assertNotIn("sid-1", server.users)
        self.assertEqual(server.sio.disconnects, ["sid-1"])


class MemoWriteLeaseApiTests(unittest.TestCase):
    def test_realtime_write_rejects_missing_or_forged_lease(self) -> None:
        class Server:
            available = True

            @staticmethod
            def validate_rest_lease(_memo_id, _user_id, token):
                return token == "valid-token"

        with patch.object(memos, "get_realtime_server", return_value=Server()):
            with self.assertRaises(HTTPException) as raised:
                memos._require_memo_write_lease("memo-1", "user-1", "forged")
            memos._require_memo_write_lease(
                "memo-1", "user-1", "valid-token"
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"], "memo_lease_required"
        )

    def test_realtime_unavailable_write_fails_closed(self) -> None:
        class UnavailableServer:
            available = False

        for server in (None, UnavailableServer()):
            with self.subTest(server=server):
                with (
                    patch.object(memos, "get_realtime_server", return_value=server),
                    self.assertRaises(HTTPException) as raised,
                ):
                    memos._require_memo_write_lease(
                        "memo-1", "user-1", None
                    )

                self.assertEqual(raised.exception.status_code, 503)
                self.assertEqual(
                    raised.exception.detail["code"],
                    "memo_realtime_unavailable",
                )


if __name__ == "__main__":
    unittest.main()
