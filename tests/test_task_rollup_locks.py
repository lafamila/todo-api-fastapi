import asyncio
import threading
import time
import unittest
from collections import defaultdict
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

from src.routers import sync as sync_router
from src.services import realtime
from src.services.lock_registry import LockRegistry, ReservationConflict
from src.services.sync_auth import SyncPrincipal
from src.services.sync_peer import SyncPeerUnreachable


def reserve(registry, leases, reservation_id="reservation-1"):
    return registry.reserve_batch(
        reservation_id=reservation_id,
        operation_id="operation-1",
        owner_key="owner-1",
        user_id="user-1",
        leases=leases,
    )


class BatchReservationRegistryTests(unittest.TestCase):
    def test_all_or_none_sorted_and_blocks_reassignment(self):
        registry = LockRegistry(ttl_seconds=60)
        _, a = registry.acquire("memo-a", "owner-1", "user-1", "User")
        _, b = registry.acquire("memo-b", "owner-1", "user-1", "User")
        with self.assertRaises(ReservationConflict):
            reserve(registry, [
                {"memoId": "memo-b", "leaseToken": b["leaseToken"]},
                {"memoId": "memo-a", "leaseToken": "forged"},
            ])
        self.assertIsNotNone(registry.release("memo-b", "owner-1"))
        _, b = registry.acquire("memo-b", "owner-1", "user-1", "User")

        result = reserve(registry, [
            {"memoId": "memo-b", "leaseToken": b["leaseToken"]},
            {"memoId": "memo-a", "leaseToken": a["leaseToken"]},
        ])
        self.assertEqual([x["memoId"] for x in result["leases"]], ["memo-a", "memo-b"])
        self.assertIsNone(registry.release("memo-a", "owner-1"))
        acquired, current = registry.acquire("memo-a", "owner-2", "user-2", "Other")
        self.assertFalse(acquired)
        self.assertEqual(current["generation"], a["generation"])

    def test_renew_crash_ttl_and_generation_fence(self):
        registry = LockRegistry(ttl_seconds=0.12)
        _, holder = registry.acquire("memo-1", "owner-1", "user-1", "User")
        initial = reserve(registry, [{"memoId": "memo-1", "leaseToken": holder["leaseToken"]}])
        threading.Event().wait(0.07)
        renewed = registry.renew_batch(
            reservation_id="reservation-1", operation_id="operation-1",
            owner_key="owner-1", user_id="user-1",
            leases=[{"memoId": "memo-1", "leaseToken": holder["leaseToken"]}],
        )
        self.assertEqual(initial["leases"][0]["generation"], renewed["leases"][0]["generation"])
        threading.Event().wait(0.08)
        self.assertTrue(registry.validate_batch(
            reservation_id="reservation-1", operation_id="operation-1",
            owner_key="owner-1", user_id="user-1",
        ))
        threading.Event().wait(0.08)
        acquired, next_holder = registry.acquire("memo-1", "owner-2", "user-2", "Other")
        self.assertTrue(acquired)
        self.assertGreater(next_holder["generation"], holder["generation"])

    def test_own_only_release_and_forced_revocation(self):
        registry = LockRegistry(ttl_seconds=60)
        _, holder = registry.acquire("memo-1", "owner-1", "user-1", "User")
        lease = {"memoId": "memo-1", "leaseToken": holder["leaseToken"]}
        reserve(registry, [lease])
        self.assertFalse(registry.release_batch(
            reservation_id="reservation-1", operation_id="wrong",
            owner_key="owner-1", user_id="user-1",
        ))
        self.assertTrue(registry.release_batch(
            reservation_id="reservation-1", operation_id="operation-1",
            owner_key="owner-1", user_id="user-1",
        ))
        self.assertTrue(registry.validate("memo-1", holder["leaseToken"]))

        reserve(registry, [lease], reservation_id="reservation-2")
        released = registry.revoke_batch(
            reservation_id="reservation-2", operation_id="operation-1",
            owner_key="owner-1", user_id="user-1",
        )
        self.assertEqual(released, ["memo-1"])
        self.assertIsNone(registry.holder("memo-1"))

    def test_disconnect_cleanup_cannot_drop_an_active_fence(self):
        registry = LockRegistry(ttl_seconds=60)
        _, holder = registry.acquire("memo-1", "owner-1", "user-1", "User")
        reserve(registry, [{"memoId": "memo-1", "leaseToken": holder["leaseToken"]}])

        self.assertEqual(registry.release_all_for_owner("owner-1"), [])
        self.assertTrue(registry.validate_batch(
            reservation_id="reservation-1", operation_id="operation-1",
            owner_key="owner-1", user_id="user-1",
        ))
        self.assertEqual(
            registry.release_all_for_owner("owner-1", force=True), ["memo-1"]
        )
        self.assertFalse(registry.validate_batch(
            reservation_id="reservation-1", operation_id="operation-1",
            owner_key="owner-1", user_id="user-1",
        ))

    def test_full_256_memo_manifest(self):
        registry = LockRegistry(ttl_seconds=60)
        leases = []
        for index in range(256):
            memo_id = f"memo-{index:03d}"
            _, holder = registry.acquire(memo_id, "owner-1", "user-1", "User")
            leases.append({"memoId": memo_id, "leaseToken": holder["leaseToken"]})
        result = reserve(registry, list(reversed(leases)))
        self.assertEqual(len(result["leases"]), 256)
        self.assertEqual(result["leases"][0]["memoId"], "memo-000")
        self.assertEqual(result["leases"][-1]["memoId"], "memo-255")


class Runtime:
    online = True
    last_error_kind = None
    def mark_offline(self, reason):
        self.online = False
        self.last_error_kind = "offline"
        self.reason = reason


class UncertainPeer:
    configured = True
    def lock_reservation_reserve(self, *_args):
        raise SyncPeerUnreachable("response lost")


class CanonicalPeer:
    configured = True

    def __init__(self, registry):
        self.registry = registry
        self.validate_calls = 0

    def lock_reservation_reserve(
        self, reservation_id, operation_id, owner_key, user_id, leases
    ):
        value = self.registry.reserve_batch(
            reservation_id=reservation_id, operation_id=operation_id,
            owner_key=f"peer:{owner_key}", user_id=user_id, leases=leases,
        )
        return {"reserved": True, "reservation": value}

    def lock_reservation_renew(
        self, reservation_id, operation_id, owner_key, user_id, leases
    ):
        value = self.registry.renew_batch(
            reservation_id=reservation_id, operation_id=operation_id,
            owner_key=f"peer:{owner_key}", user_id=user_id, leases=leases,
        )
        return {"renewed": True, "reservation": value}

    def lock_reservation_release(
        self, reservation_id, operation_id, owner_key, user_id
    ):
        return {"released": self.registry.release_batch(
            reservation_id=reservation_id, operation_id=operation_id,
            owner_key=f"peer:{owner_key}", user_id=user_id,
        )}

    def lock_reservation_validate(
        self, reservation_id, operation_id, owner_key, user_id, leases
    ):
        self.validate_calls += 1
        return {"valid": self.registry.validate_batch(
            reservation_id=reservation_id, operation_id=operation_id,
            owner_key=f"peer:{owner_key}", user_id=user_id, leases=leases,
        )}


class CorruptReservePeer(CanonicalPeer):
    def lock_reservation_reserve(self, *args):
        result = super().lock_reservation_reserve(*args)
        result["reservation"]["leases"][0]["generation"] += 1
        return result

    def lock_reservation_release(self, *_args):
        return {"released": False}


class ValidationUnreachablePeer(CanonicalPeer):
    def lock_reservation_validate(self, *_args):
        raise SyncPeerUnreachable("validation response lost")


class TaskRollupAdapterTests(unittest.IsolatedAsyncioTestCase):
    def make_server(self, count=1):
        server = realtime.MemoRealtimeServer.__new__(realtime.MemoRealtimeServer)
        server.available = True
        server.users = {
            "rollup": {"user_id": "user-1", "display_name": "Rollup"},
            "editor": {"user_id": "user-1", "display_name": "Editor"},
        }
        server.socket_memos = defaultdict(set)
        server.socket_locks = defaultdict(set)
        server.socket_lease_tokens = defaultdict(dict)
        server.lock_registry = LockRegistry(ttl_seconds=60)
        server._lock_delegation_quarantine_until = 0.0
        server._task_rollup_reservations = {}
        leases = []
        for index in range(count):
            memo_id = f"memo-{index:03d}"
            _, holder = server.lock_registry.acquire(
                memo_id, server._owner_key("rollup"), "user-1", "Rollup"
            )
            server.socket_memos["rollup"].add(memo_id)
            server.socket_locks["rollup"].add(memo_id)
            server.socket_lease_tokens["rollup"][memo_id] = (
                holder["leaseToken"], realtime.monotonic() + 60,
            )
            leases.append({"memoId": memo_id, "token": holder["leaseToken"]})
        return server, leases

    async def test_one_dedicated_socket_reserves_95_leases(self):
        server, leases = self.make_server(95)
        with patch.object(realtime, "runs_sync_daemon", return_value=False):
            handle = await server.reserve_task_rollup(
                "user-1", str(uuid4()), list(reversed(leases))
            )
            self.assertTrue(handle.validate())
            self.assertEqual(len(handle.memo_ids), 95)
            await handle.release()
        self.assertFalse(handle.validate())
        self.assertIsNotNone(server.lock_registry.holder("memo-000"))

    async def test_same_account_split_socket_is_rejected_without_stealing(self):
        server, leases = self.make_server(2)
        memo_id = "memo-001"
        server.lock_registry.release(memo_id, server._owner_key("rollup"))
        _, holder = server.lock_registry.acquire(
            memo_id, server._owner_key("editor"), "user-1", "Editor"
        )
        server.socket_memos["rollup"].discard(memo_id)
        server.socket_locks["rollup"].discard(memo_id)
        server.socket_lease_tokens["rollup"].pop(memo_id)
        server.socket_memos["editor"].add(memo_id)
        server.socket_locks["editor"].add(memo_id)
        server.socket_lease_tokens["editor"][memo_id] = (
            holder["leaseToken"], realtime.monotonic() + 60,
        )
        leases[1] = {"memoId": memo_id, "token": holder["leaseToken"]}
        with patch.object(realtime, "runs_sync_daemon", return_value=False):
            with self.assertRaises(realtime.TaskRollupReservationError) as raised:
                await server.reserve_task_rollup("user-1", str(uuid4()), leases)
        self.assertEqual(raised.exception.code, "lease_owner_mismatch")
        self.assertEqual(server.lock_registry.holder(memo_id)["leaseToken"], holder["leaseToken"])

    async def test_uncertain_remote_outcome_quarantines_without_fallback(self):
        server, leases = self.make_server()
        runtime = Runtime()
        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=UncertainPeer()),
        ):
            with self.assertRaises(realtime.TaskRollupReservationError):
                await server.reserve_task_rollup("user-1", str(uuid4()), leases)
        self.assertFalse(runtime.online)
        self.assertGreater(server._lock_delegation_quarantine_until, realtime.monotonic())
        self.assertIsNotNone(server.lock_registry.release("memo-000", server._owner_key("rollup")))

    async def test_unrelated_success_cannot_clear_lost_reservation_quarantine(self):
        server, leases = self.make_server()
        runtime = Runtime()
        peer = UncertainPeer()
        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            with self.assertRaises(realtime.TaskRollupReservationError):
                await server.reserve_task_rollup("user-1", str(uuid4()), leases)
            deadline = server._lock_delegation_quarantine_until

            runtime.online = True
            runtime.last_error_kind = None
            self.assertEqual(
                await server._delegate_lock(lambda: {"holder": None}),
                {"holder": None},
            )
            self.assertEqual(server._lock_delegation_quarantine_until, deadline)

            runtime.online = False
            runtime.last_error_kind = "offline"
            with self.assertRaises(realtime.LockDelegationError):
                await server._delegate_lock(lambda: {"holder": None})

    async def test_online_client_uses_remote_canonical_reservation(self):
        server, _ = self.make_server()
        # Online clients only cache the browser token locally; the server node
        # owns the canonical generation under the peer-prefixed owner key.
        local_holder = server.lock_registry.holder("memo-000")
        server.lock_registry.release("memo-000", server._owner_key("rollup"))
        remote_registry = LockRegistry(ttl_seconds=60)
        _, remote_holder = remote_registry.acquire(
            "memo-000", f"peer:{server._owner_key('rollup')}", "user-1", "Rollup"
        )
        server.socket_lease_tokens["rollup"]["memo-000"] = (
            remote_holder["leaseToken"], realtime.monotonic() + 60,
        )
        peer = CanonicalPeer(remote_registry)
        runtime = Runtime()
        leases = [{"memoId": "memo-000", "token": remote_holder["leaseToken"]}]
        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            handle = await server.reserve_task_rollup("user-1", str(uuid4()), leases)
            self.assertTrue(handle.delegated)
            for _ in range(10):
                self.assertTrue(handle.validate_local())
            self.assertEqual(peer.validate_calls, 0)
            self.assertTrue(handle.validate())
            self.assertEqual(peer.validate_calls, 1)
            await server._renew_task_rollup_reservation(handle)
            server._invalidate_changed_task_rollup_fences("memo-000", None)
            self.assertFalse(handle.validate())
            await handle.release()
        self.assertIsNone(server.lock_registry.holder("memo-000"))
        self.assertTrue(remote_registry.validate(
            "memo-000", remote_holder["leaseToken"], user_id="user-1"
        ))
        self.assertIsNotNone(local_holder)

    async def test_final_worker_fence_detects_remote_force_revoke(self):
        server, _ = self.make_server()
        server.lock_registry.release("memo-000", server._owner_key("rollup"))
        remote_registry = LockRegistry(ttl_seconds=60)
        _, holder = remote_registry.acquire(
            "memo-000", f"peer:{server._owner_key('rollup')}", "user-1", "Rollup"
        )
        server.socket_lease_tokens["rollup"]["memo-000"] = (
            holder["leaseToken"], realtime.monotonic() + 60,
            holder["generation"],
        )
        peer = CanonicalPeer(remote_registry)
        runtime = Runtime()
        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            handle = await server.reserve_task_rollup(
                "user-1", str(uuid4()),
                [{"memoId": "memo-000", "token": holder["leaseToken"]}],
            )
            await server._renew_task_rollup_reservation(handle)
            self.assertTrue(handle._locally_valid())
            remote_registry.revoke_batch(
                reservation_id=handle.reservation_id,
                operation_id=handle.operation_id,
                owner_key=f"peer:{handle.owner_key}",
                user_id=handle.user_id,
            )
            writes = []
            if handle.validate():
                writes.append("committed")
            self.assertEqual(writes, [])
            self.assertFalse(handle.validate())
            await handle.release()

    async def test_final_worker_fence_fails_closed_on_unknown_remote_outcome(self):
        server, _ = self.make_server()
        server.lock_registry.release("memo-000", server._owner_key("rollup"))
        remote_registry = LockRegistry(ttl_seconds=60)
        _, holder = remote_registry.acquire(
            "memo-000", f"peer:{server._owner_key('rollup')}", "user-1", "Rollup"
        )
        server.socket_lease_tokens["rollup"]["memo-000"] = (
            holder["leaseToken"], realtime.monotonic() + 60,
            holder["generation"],
        )
        peer = ValidationUnreachablePeer(remote_registry)
        runtime = Runtime()
        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            handle = await server.reserve_task_rollup(
                "user-1", str(uuid4()),
                [{"memoId": "memo-000", "token": holder["leaseToken"]}],
            )
            self.assertFalse(handle.validate())
            self.assertFalse(runtime.online)
            self.assertGreater(
                server._lock_delegation_quarantine_until, realtime.monotonic()
            )
            await handle.release()

    async def test_malformed_remote_success_is_quarantined_until_ttl(self):
        server, _ = self.make_server()
        server.lock_registry.release("memo-000", server._owner_key("rollup"))
        remote_registry = LockRegistry(ttl_seconds=60)
        _, holder = remote_registry.acquire(
            "memo-000", f"peer:{server._owner_key('rollup')}", "user-1", "Rollup"
        )
        server.socket_lease_tokens["rollup"]["memo-000"] = (
            holder["leaseToken"], realtime.monotonic() + 60,
            holder["generation"],
        )
        peer = CorruptReservePeer(remote_registry)
        runtime = Runtime()
        with (
            patch.object(realtime, "runs_sync_daemon", return_value=True),
            patch.object(realtime, "get_sync_runtime", return_value=runtime),
            patch.object(realtime, "get_sync_peer", return_value=peer),
        ):
            with self.assertRaises(realtime.TaskRollupReservationError) as raised:
                await server.reserve_task_rollup(
                    "user-1", str(uuid4()),
                    [{"memoId": "memo-000", "token": holder["leaseToken"]}],
                )
        self.assertEqual(raised.exception.code, "reservation_fence_mismatch")
        self.assertGreater(server._lock_delegation_quarantine_until, realtime.monotonic())
        # False cleanup confirmation must not be mistaken for a released fence.
        self.assertIsNone(remote_registry.release(
            "memo-000", f"peer:{server._owner_key('rollup')}"
        ))


class ReservationRpcTests(unittest.IsolatedAsyncioTestCase):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return self

    async def test_authenticated_reserve_renew_release_round_trip(self):
        registry = LockRegistry(ttl_seconds=60)
        owner_key = "peer:laptop:rollup"
        _, holder = registry.acquire("memo-1", owner_key, "user-1", "User")
        reservation_id = str(uuid4())
        operation_id = str(uuid4())
        body = sync_router.LockReservationRequest(
            reservationId=reservation_id,
            operationId=operation_id,
            ownerKey="laptop:rollup",
            userId="user-1",
            leases=[{"memoId": "memo-1", "leaseToken": holder["leaseToken"]}],
        )
        principal = SyncPrincipal("user-1", "owner", "service", "sync-key")
        with (
            patch.object(sync_router, "get_lock_registry", return_value=registry),
            patch.object(
                sync_router,
                "_authorize_reservation_leases",
                side_effect=lambda request, _principal: [
                    lease.model_dump() for lease in request.leases
                ],
            ),
        ):
            reserved = await sync_router.sync_lock_reservation_reserve(body, principal)
            renewed = await sync_router.sync_lock_reservation_renew(body, principal)
            validated = await sync_router.sync_lock_reservation_validate(
                sync_router.LockReservationValidateRequest(
                    reservationId=reservation_id,
                    operationId=operation_id,
                    ownerKey="laptop:rollup",
                    userId="user-1",
                    leases=[{
                        "memoId": "memo-1",
                        "leaseToken": holder["leaseToken"],
                        "generation": holder["generation"],
                    }],
                ),
                principal,
            )
            released = await sync_router.sync_lock_reservation_release(
                sync_router.LockReservationReleaseRequest(
                    reservationId=reservation_id,
                    operationId=operation_id,
                    ownerKey="laptop:rollup",
                    userId="user-1",
                ),
                principal,
            )
        self.assertTrue(reserved["reserved"])
        self.assertTrue(renewed["renewed"])
        self.assertTrue(validated["valid"])
        self.assertTrue(released["released"])
        self.assertTrue(registry.validate("memo-1", holder["leaseToken"]))

    async def test_slow_reservation_authorization_does_not_block_event_loop(self):
        registry = LockRegistry(ttl_seconds=60)
        _, holder = registry.acquire(
            "memo-1", "peer:laptop:rollup", "user-1", "User"
        )
        body = sync_router.LockReservationRequest(
            reservationId=str(uuid4()),
            operationId=str(uuid4()),
            ownerKey="laptop:rollup",
            userId="user-1",
            leases=[{"memoId": "memo-1", "leaseToken": holder["leaseToken"]}],
        )
        principal = SyncPrincipal("user-1", "owner", "service", "sync-key")
        barrier = threading.Barrier(2)

        def slow_authorize(request, _principal):
            barrier.wait(timeout=1)
            time.sleep(0.08)
            return [lease.model_dump() for lease in request.leases]

        with (
            patch.object(sync_router, "get_lock_registry", return_value=registry),
            patch.object(
                sync_router,
                "_authorize_reservation_leases",
                side_effect=slow_authorize,
            ),
        ):
            request_task = asyncio.create_task(
                sync_router.sync_lock_reservation_reserve(body, principal)
            )
            await asyncio.to_thread(barrier.wait, 1)
            heartbeat_ran = False

            async def heartbeat():
                nonlocal heartbeat_ran
                await asyncio.sleep(0.01)
                heartbeat_ran = True

            await heartbeat()
            self.assertTrue(heartbeat_ran)
            self.assertFalse(request_task.done())
            result = await request_task

        self.assertTrue(result["reserved"])
        registry.release_batch(
            reservation_id=body.reservationId,
            operation_id=body.operationId,
            owner_key="peer:laptop:rollup",
            user_id="user-1",
        )

    async def test_rpc_actor_must_match_authenticated_account(self):
        body = sync_router.LockReservationReleaseRequest(
            reservationId=str(uuid4()), operationId=str(uuid4()),
            ownerKey="laptop:rollup", userId="other-user",
        )
        principal = SyncPrincipal("user-1", "owner", "service", "sync-key")
        with self.assertRaises(HTTPException) as raised:
            await sync_router.sync_lock_reservation_release(body, principal)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail["code"], "lock_reservation_actor_mismatch")

    async def test_legacy_acquire_cannot_spoof_holder_actor(self):
        principal = SyncPrincipal("user-1", "owner", "service", "sync-key")
        with self.assertRaises(HTTPException) as raised:
            await sync_router.sync_lock_acquire(
                "memo-1",
                sync_router.LockAcquireRequest(
                    ownerKey="laptop:sid", userId="victim", displayName="Victim"
                ),
                principal,
            )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail["code"], "lock_actor_mismatch")

    async def test_legacy_acquire_uses_canonical_display_identity(self):
        principal = SyncPrincipal("user-1", "owner", "service", "sync-key")
        registry = LockRegistry(ttl_seconds=60)
        with (
            patch.object(sync_router, "get_db_connection", return_value=self.Connection()),
            patch.object(sync_router, "_require_memo_write_access"),
            patch.object(
                sync_router,
                "_handshake_identity",
                return_value={"displayName": "Canonical User", "loginId": "user-1"},
            ),
            patch.object(sync_router, "get_lock_registry", return_value=registry),
        ):
            result = await sync_router.sync_lock_acquire(
                "memo-1",
                sync_router.LockAcquireRequest(
                    ownerKey="laptop:sid", userId="user-1", displayName="Spoofed"
                ),
                principal,
            )
        self.assertTrue(result["acquired"])
        self.assertEqual(result["holder"]["userId"], "user-1")
        self.assertEqual(result["holder"]["displayName"], "Canonical User")


class TaskRollupSyncRouterGuardTests(unittest.TestCase):
    class NoDatabaseCursor:
        def execute(self, *_args):
            raise AssertionError("guard must reject before touching the database")

    def test_unknown_receipt_fields_fail_closed_before_filtering(self):
        with self.assertRaises(HTTPException) as raised:
            sync_router._authorize_sync_changes(
                self.NoDatabaseCursor(),
                [{
                    "table": "task_rollup_completions",
                    "rowId": str(uuid4()),
                    "op": "update",
                    "row": {"actor_id": "user-1", "leaseToken": "must-not-drop"},
                }],
                "user-1",
            )
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail["code"], "task_rollup_unknown_fields")

    def test_receipt_hard_delete_fails_closed_before_lookup(self):
        with self.assertRaises(HTTPException) as raised:
            sync_router._authorize_sync_changes(
                self.NoDatabaseCursor(),
                [{
                    "table": "task_rollup_completions",
                    "rowId": str(uuid4()),
                    "op": "delete",
                    "row": {},
                }],
                "user-1",
            )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(
            raised.exception.detail["reason"], "task_rollup_hard_delete_forbidden"
        )


if __name__ == "__main__":
    unittest.main()
