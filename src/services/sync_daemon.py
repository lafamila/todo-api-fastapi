"""동기화 데몬 — 노트북(클라이언트 역할)에서만 돈다.

동작:
    - 로컬 저장 후 **디바운스 push** (`SYNC_PUSH_DEBOUNCE_MS`)
    - 원격 Socket.IO `syncChanged` 수신 시 **즉시 pull**
    - **폴링 안전망** (`SYNC_POLL_SECONDS`) — 트리거는 API 밖 변경까지 잡지만 소켓 알림은
      API 레이어에서 나가므로 그 구멍을 메운다
    - 오프라인 **백오프** (`SYNC_OFFLINE_BACKOFF_SECONDS`), 복구 감지 시 즉시 동기화
    - `last_pushed_seq` 는 immutable outbox 적재 watermark,
      `last_pulled_seq` 는 immutable inbox 적재 watermark
    - `paused` 면 아무것도 하지 않는다

**API 프로세스 안에서 도는 것이 기본이다.** pull 적용 후 열려 있는 탭을 갱신하려면
로컬 Socket.IO 서버에 재발행해야 하고, 그 서버는 이 프로세스의 메모리에 있다.
별도 프로세스(`python -m src.sync_daemon`)로도 돌 수 있지만 그때는 재발행이 빠진다.
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic, sleep

try:
    import socketio
except ImportError:  # pragma: no cover
    socketio = None

try:
    from ..config import (
        SYNC_ALLOW_SCHEMA_DRIFT,
        SYNC_BATCH_LIMIT,
        SYNC_CLIENT_ID,
        SYNC_CLOCK_SKEW_LIMIT_SECONDS,
        SYNC_OFFLINE_BACKOFF_SECONDS,
        SYNC_POLL_SECONDS,
        SYNC_PUSH_DEBOUNCE_MS,
        runs_sync_daemon,
    )
    from ..connectors import get_db_connection
    from ..sync_schema import SCHEMA_VERSION
    from ..timeutil import iso_utc, parse_iso_utc, utcnow_naive
    from .sync_apply import SIDE_SERVER, apply_changes
    from .sync_auth import distinct_owner_ids
    from .sync_peer import SyncPeer, SyncPeerError, SyncPeerUnreachable, get_sync_peer
    from .sync_runtime import get_sync_runtime
    from .sync_store import (
        collect_local_changes,
        delete_sync_retry,
        enqueue_sync_retry,
        get_client_epoch,
        get_row_sync_clock,
        get_sync_state,
        list_sync_retries,
        mark_sync_retry_dead,
        mark_sync_retry_failed,
        max_change_seq,
        pending_sync_retry_count,
        record_issue,
        resolve_issues,
        set_row_sync_clock,
        update_sync_state,
    )
except ImportError:  # pragma: no cover
    from config import (
        SYNC_ALLOW_SCHEMA_DRIFT,
        SYNC_BATCH_LIMIT,
        SYNC_CLIENT_ID,
        SYNC_CLOCK_SKEW_LIMIT_SECONDS,
        SYNC_OFFLINE_BACKOFF_SECONDS,
        SYNC_POLL_SECONDS,
        SYNC_PUSH_DEBOUNCE_MS,
        runs_sync_daemon,
    )
    from connectors import get_db_connection
    from sync_schema import SCHEMA_VERSION
    from timeutil import iso_utc, parse_iso_utc, utcnow_naive
    from services.sync_apply import SIDE_SERVER, apply_changes
    from services.sync_auth import distinct_owner_ids
    from services.sync_peer import SyncPeer, SyncPeerError, SyncPeerUnreachable, get_sync_peer
    from services.sync_runtime import get_sync_runtime
    from services.sync_store import (
        collect_local_changes,
        delete_sync_retry,
        enqueue_sync_retry,
        get_client_epoch,
        get_row_sync_clock,
        get_sync_state,
        list_sync_retries,
        mark_sync_retry_dead,
        mark_sync_retry_failed,
        max_change_seq,
        pending_sync_retry_count,
        record_issue,
        resolve_issues,
        set_row_sync_clock,
        update_sync_state,
    )


logger = logging.getLogger(__name__)

# 편차 초과 감지 시 재측정 전 대기 (슬립 복귀 스파이크 필터) — 테스트는 0 으로 바꾼다.
CLOCK_SPIKE_RECHECK_SECONDS = 2.0

MAX_ROUNDS_PER_CYCLE = 20
# 소켓 구독 재시도 간격 (구독은 가속기이므로 오프라인 백오프보다 짧게 잡는다)
SOCKET_RETRY_SECONDS = 5


class SyncBlocked(Exception):
    """부분 적용 없이 중단해야 하는 상태 (신원/스키마/시계)."""

    def __init__(self, kind: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail or {}


class SyncDaemon:
    def __init__(
        self,
        peer: SyncPeer | None = None,
        client_id: str | None = None,
        realtime_server=None,
    ) -> None:
        self.peer = peer or get_sync_peer()
        self.client_id = client_id or SYNC_CLIENT_ID
        self.realtime_server = realtime_server
        self.runtime = get_sync_runtime()
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._socket = None
        self._socket_task: asyncio.Task | None = None
        self._last_cycle_at = 0.0
        self._last_attempt_at = 0.0

    # -- 수명주기 ----------------------------------------------------------

    async def start(self) -> None:
        if not runs_sync_daemon():
            logger.info("sync daemon not started (role is not client)")
            return
        if not self.peer.configured:
            logger.warning(
                "sync daemon not started: SYNC_PEER_URL / SYNC_KEY_ID / SYNC_SECRET are required"
            )
            return
        self.runtime.bind_wake_event(self._wake)
        self.runtime.daemon_running = True
        self._task = asyncio.create_task(self.run(), name="sync-daemon")
        self._socket_task = asyncio.create_task(self._run_socket(), name="sync-daemon-socket")
        logger.info("sync daemon started (peer=%s client=%s)", self.peer.root, self.client_id)

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        self.runtime.daemon_running = False

        # 소켓을 먼저 끊어야 `client.wait()` 가 스스로 반환한다 (취소만으로는
        # engineio 내부 태스크가 남아 종료가 지연될 수 있다).
        client = self._socket
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

        for task in (self._task, self._socket_task):
            if task is None:
                continue
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.CancelledError, TimeoutError, Exception):  # noqa: BLE001
                pass

    # -- 메인 루프 ---------------------------------------------------------

    async def run(self) -> None:
        tick = max(SYNC_PUSH_DEBOUNCE_MS, 100) / 1000
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=tick)
                self._wake.clear()
                triggered = True
            except asyncio.TimeoutError:
                triggered = False

            if self._stopping.is_set():
                return
            try:
                if triggered or await asyncio.to_thread(self._should_cycle):
                    await self.run_once()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:  # noqa: BLE001 - 루프는 절대 죽지 않는다
                logger.exception("sync cycle failed")

    def _should_cycle(self) -> bool:
        """이번 tick 에 한 바퀴 돌아야 하는지 (블로킹 — to_thread 로 호출한다)."""
        now = monotonic()
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                state = get_sync_state(cursor, self.client_id)
                if state["paused"]:
                    return False
                pending = (
                    max_change_seq(cursor) > int(state["last_pushed_seq"])
                    or pending_sync_retry_count(cursor, self.client_id) > 0
                )

        if pending:
            # 로컬 저장 직후 — 디바운스 tick 안에 감지된다
            return True
        if not self.runtime.online:
            return now - self._last_attempt_at >= SYNC_OFFLINE_BACKOFF_SECONDS
        return now - self._last_cycle_at >= SYNC_POLL_SECONDS

    async def run_once(self) -> dict:
        """한 사이클: handshake → preflight → push → pull → 재발행."""
        self._last_attempt_at = monotonic()
        try:
            report = await asyncio.to_thread(self._cycle_sync)
        except SyncPeerUnreachable as exc:
            self.runtime.mark_offline(str(exc))
            await asyncio.to_thread(self._store_error, f"offline: {exc}")
            return {"ok": False, "reason": "offline", "detail": str(exc)}
        except SyncBlocked as exc:
            self.runtime.mark_online()
            self.runtime.mark_blocked(exc.kind, exc.message)
            await asyncio.to_thread(self._store_blocked, exc)
            return {"ok": False, "reason": exc.kind, "detail": exc.message}
        except SyncPeerError as exc:
            self.runtime.mark_online()
            self.runtime.mark_blocked("peer_rejected", str(exc))
            await asyncio.to_thread(self._store_error, str(exc))
            return {"ok": False, "reason": "peer_rejected", "detail": str(exc)}

        self._last_cycle_at = monotonic()
        self.runtime.mark_online()
        self.runtime.clear_blocked()
        self.runtime.mark_cycle()
        await self._rebroadcast(report)
        return report

    # -- 사이클 본체 (블로킹) ----------------------------------------------

    def _cycle_sync(self) -> dict:
        handshake = self.peer.handshake()
        columns_by_table = self._check_schema(handshake)
        self._check_clock(handshake)

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                self._check_identity(cursor, handshake)

        push_report = self._push_rounds(columns_by_table)
        pull_report = self._pull_rounds(columns_by_table)

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                update_sync_state(
                    cursor, self.client_id, last_ok_at=utcnow_naive(), last_error=None
                )
                self._resolve_stale_gate_issues(cursor)

        return {
            "ok": True,
            "accountId": handshake.get("accountId"),
            "peerSchemaVersion": handshake.get("schemaVersion"),
            "push": push_report,
            "pull": pull_report,
        }

    def _check_schema(self, handshake: dict) -> dict[str, tuple[str, ...]]:
        peer_version = int(handshake.get("schemaVersion") or 0)
        self.runtime.peer_schema_version = peer_version
        if peer_version == SCHEMA_VERSION:
            return {}

        if peer_version < SCHEMA_VERSION:
            message = (
                f"로컬 스키마가 앞서 있습니다 (로컬 {SCHEMA_VERSION} > 원격 {peer_version}). "
                "원격 배포 후 재개하세요."
            )
            if not SYNC_ALLOW_SCHEMA_DRIFT:
                raise SyncBlocked(
                    "schema", message, {"local": SCHEMA_VERSION, "peer": peer_version}
                )
            logger.warning("%s — SYNC_ALLOW_SCHEMA_DRIFT=true 이므로 공통 필드만 동기화합니다", message)
            return self._intersect_columns(handshake.get("tables"))

        raise SyncBlocked(
            "schema",
            f"원격 스키마가 앞서 있습니다 (원격 {peer_version} > 로컬 {SCHEMA_VERSION}). "
            "코드를 pull 하고 init_db 를 실행한 뒤 재개하세요.",
            {"local": SCHEMA_VERSION, "peer": peer_version},
        )

    def _intersect_columns(self, peer_tables) -> dict[str, tuple[str, ...]]:
        try:
            from ..sync_schema import SYNC_TABLES, intersect_columns
        except ImportError:  # pragma: no cover
            from sync_schema import SYNC_TABLES, intersect_columns

        if not isinstance(peer_tables, dict):
            return {}
        resolved: dict[str, tuple[str, ...]] = {}
        for table in SYNC_TABLES:
            peer_columns = peer_tables.get(table)
            if peer_columns:
                resolved[table] = intersect_columns(table, list(peer_columns))
        return resolved

    def _check_clock(self, handshake: dict) -> None:
        """시계가 틀어진 LWW 는 조용히 최신 내용을 버린다 — 그래서 편차를 먼저 잡는다."""
        skew = self._measure_skew(handshake)
        if skew is None:
            return
        self.runtime.clock_skew_seconds = round(skew, 3)
        if skew <= SYNC_CLOCK_SKEW_LIMIT_SECONDS:
            return

        # 슬립 복귀 직후에는 요청이 멈췄다 재개되며 가짜 스파이크가 흔하다 —
        # 즉시 기록하지 않고 잠시 뒤 새 handshake 로 1회 재측정한다.
        # 재측정이 실패하면(네트워크 미복구 등) 스파이크를 반증하지 못한 것이므로
        # 원래대로 중단한다 — 이후 정상 사이클이 이슈를 자동 해소한다.
        retry_skew: float | None = None
        try:
            sleep(CLOCK_SPIKE_RECHECK_SECONDS)
            retry_skew = self._measure_skew(self.peer.handshake())
        except Exception:  # noqa: BLE001 - 재측정 실패는 차단 유지로 흡수한다
            retry_skew = None
        if retry_skew is not None:
            self.runtime.clock_skew_seconds = round(retry_skew, 3)
            if retry_skew <= SYNC_CLOCK_SKEW_LIMIT_SECONDS:
                logger.info(
                    "clock spike ignored after recheck (%.1fs -> %.1fs)", skew, retry_skew
                )
                return
            skew = retry_skew
        raise SyncBlocked(
            "clock",
            f"시계 편차가 {skew:.1f}초로 허용치({SYNC_CLOCK_SKEW_LIMIT_SECONDS}초)를 넘었습니다. "
            "시간 동기화 후 재개하세요.",
            {"skewSeconds": skew, "limit": SYNC_CLOCK_SKEW_LIMIT_SECONDS},
        )

    @staticmethod
    def _measure_skew(handshake: dict) -> float | None:
        server_time = parse_iso_utc(handshake.get("serverTimeUtc"))
        if server_time is None:
            return None
        return abs((utcnow_naive() - server_time).total_seconds())

    @staticmethod
    def _resolve_stale_gate_issues(cursor) -> None:
        """게이트(스키마·시계·신원)를 모두 통과한 사이클 직후 호출한다.

        같은 종류의 미해결 이슈가 남아 있다면 조건이 이미 정상으로 돌아온
        낡은 기록이므로 자동 해소한다 — 안 하면 상태 표시가 "중단"에 머문다
        (슬립 복귀 스파이크 뒤 실제로 겪은 문제).
        """
        stale = 0
        for kind in ("clock", "identity", "schema"):
            stale += resolve_issues(cursor, kind=kind)
        if stale:
            logger.info("resolved %d stale gate issue(s) after a healthy cycle", stale)

    def _check_identity(self, cursor, handshake: dict) -> None:
        """부분 적용 없이 중단 — 신원이 어긋난 채 적재되면 손으로 풀기 어렵다."""
        remote_account = handshake.get("accountId")
        local_ids = distinct_owner_ids(cursor)
        remote_ids = handshake.get("ownerIds") or []
        mismatched = [owner_id for owner_id in local_ids if owner_id != remote_account]
        if not mismatched:
            return
        detail = {
            "remoteAccountId": remote_account,
            "remoteOwnerIds": remote_ids,
            "localOwnerIds": local_ids,
            "mismatched": mismatched,
        }
        # 이슈 기록은 `_store_blocked` 가 별도 트랜잭션에서 한다 — 여기서 넣으면
        # 아래 raise 로 이 커넥션이 롤백되면서 함께 사라진다.
        raise SyncBlocked(
            "identity",
            "로컬 데이터의 owner id 가 원격 계정과 다릅니다: "
            f"{mismatched} != {remote_account}. "
            "`python -m src.sync_cli link-identity` 로 로컬을 원격에 맞추세요.",
            detail,
        )

    def _push_rounds(self, columns_by_table: dict[str, tuple[str, ...]]) -> dict:
        report = {
            "rounds": 0,
            "applied": 0,
            "skipped": 0,
            "conflicts": [],
            "duplicates": [],
            "deferred": [],
            "rejected": [],
        }
        self._merge_push_report(report, self._retry_push_queue())

        # 네트워크 호출 전에 현재 행 snapshot을 queue에 고정하고 같은 트랜잭션에서
        # scan watermark를 진행한다. 응답 유실 뒤 행이 다시 수정돼도 같은 source seq는
        # 절대로 다른 payload로 재구성되지 않는다.
        for _ in range(MAX_ROUNDS_PER_CYCLE):
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    state = get_sync_state(cursor, self.client_id)
                    since = int(state["last_pushed_seq"])
                    changes, next_seq = collect_local_changes(
                        cursor,
                        since,
                        SYNC_BATCH_LIMIT,
                        columns_by_table or None,
                        peer=self.client_id,
                    )
                    if not changes:
                        break
                    for change in changes:
                        enqueue_sync_retry(
                            cursor,
                            self.client_id,
                            "push",
                            change,
                            "pending delivery",
                        )
                    update_sync_state(cursor, self.client_id, last_pushed_seq=next_seq)

            if len(changes) < SYNC_BATCH_LIMIT:
                break

        self._merge_push_report(report, self._retry_push_queue())
        return report

    @staticmethod
    def _merge_push_report(target: dict, source: dict) -> None:
        target["rounds"] += int(source.get("rounds") or 0)
        target["applied"] += int(source.get("applied") or 0)
        target["skipped"] += int(source.get("skipped") or 0)
        for key in ("conflicts", "duplicates", "deferred", "rejected"):
            target[key].extend(source.get(key) or [])

    def _retry_push_queue(self) -> dict:
        """이전 cycle에서 실패한 push를 새 change_log 구간과 독립적으로 재시도한다."""
        report = {
            "applied": 0,
            "skipped": 0,
            "conflicts": [],
            "duplicates": [],
            "deferred": [],
            "rejected": [],
            "rounds": 0,
        }
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                queued = list_sync_retries(
                    cursor, self.client_id, "push", SYNC_BATCH_LIMIT
                )
                delivery_client_id = (
                    f"{self.client_id}:{get_client_epoch(cursor, self.client_id)}"
                )

        for entry in queued:
            change = entry["change"]
            response = self.peer.push(delivery_client_id, [change])
            report["rounds"] += 1
            report["applied"] += int(response.get("applied") or 0)
            report["skipped"] += int(response.get("skipped") or 0)
            report["conflicts"].extend(response.get("conflicts") or [])
            report["duplicates"].extend(response.get("duplicates") or [])
            report["deferred"].extend(response.get("deferred") or [])
            report["rejected"].extend(response.get("rejected") or [])
            result = next(iter(response.get("results") or []), None)
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    self._mirror_remote_issues(cursor, response)
                    if result and result.get("status") in (
                        "applied",
                        "skipped",
                        "unchanged",
                        "conflict",
                    ):
                        delete_sync_retry(
                            cursor, self.client_id, "push", int(entry["seq"])
                        )
                        self._remember_result_clock(cursor, result)
                    elif result and result.get("status") == "rejected":
                        reason = result.get("reason") or "peer rejected change"
                        mark_sync_retry_dead(
                            cursor,
                            self.client_id,
                            "push",
                            int(entry["seq"]),
                            reason,
                        )
                        record_issue(
                            cursor,
                            "sync_dead_letter",
                            ref_table=entry.get("table_name"),
                            ref_id=entry.get("row_id"),
                            detail={
                                "direction": "push",
                                "seq": int(entry["seq"]),
                                "reason": reason,
                            },
                        )
                    else:
                        reason = (
                            (result or {}).get("reason")
                            or next(iter(response.get("deferred") or []), {}).get("reason")
                            or next(iter(response.get("rejected") or []), {}).get("reason")
                            or "peer did not return a sequence-aware result"
                        )
                        mark_sync_retry_failed(
                            cursor,
                            self.client_id,
                            "push",
                            int(entry["seq"]),
                            reason,
                        )
        return report

    def _mirror_remote_issues(self, cursor, response: dict) -> None:
        for conflict in response.get("conflicts") or []:
            record_issue(
                cursor,
                "conflict",
                ref_table=conflict.get("table"),
                ref_id=conflict.get("rowId"),
                detail={**conflict, "source": "peer"},
            )
        for duplicate in response.get("duplicates") or []:
            kind = duplicate.get("kind")
            if not kind:
                continue
            record_issue(
                cursor,
                kind,
                ref_table=duplicate.get("table"),
                ref_id=duplicate.get("rowId"),
                peer_ref_id=duplicate.get("peerRowId"),
                detail={**duplicate, "source": "peer"},
            )

    def _pull_rounds(self, columns_by_table: dict[str, tuple[str, ...]]) -> dict:
        applied = skipped = rounds = 0
        conflicts: list[dict] = []
        duplicates: list[dict] = []
        deferred: list[dict] = []
        rejected: list[dict] = []
        applied_rows: list[dict] = []

        retry_report = self._retry_pull_queue(columns_by_table)
        applied += retry_report["applied"]
        skipped += retry_report["skipped"]
        conflicts.extend(retry_report["conflicts"])
        duplicates.extend(retry_report["duplicates"])
        deferred.extend(retry_report["deferred"])
        rejected.extend(retry_report["rejected"])
        applied_rows.extend(retry_report["appliedRows"])

        for _ in range(MAX_ROUNDS_PER_CYCLE):
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    state = get_sync_state(cursor, self.client_id)
                    since = int(state["last_pulled_seq"])

            response = self.peer.changes(since, SYNC_BATCH_LIMIT)
            changes = response.get("changes") or []
            next_seq = int(response.get("nextSeq") or since)
            if not changes and next_seq <= since:
                break

            with get_db_connection(sync_applying=True) as conn:
                with conn.cursor() as cursor:
                    self._add_pull_bases(cursor, changes)
                    outcome = apply_changes(
                        cursor,
                        changes,
                        incoming_side=SIDE_SERVER,
                        columns_by_table=columns_by_table or None,
                    )
                    result_by_seq = {
                        int(result["seq"]): result
                        for result in outcome.results
                        if result.get("seq") is not None
                    }
                    change_by_seq = {
                        int(change["seq"]): change
                        for change in changes
                        if change.get("seq") is not None
                    }
                    for seq, change in change_by_seq.items():
                        result = result_by_seq.get(seq)
                        if result is None:
                            enqueue_sync_retry(
                                cursor,
                                self.client_id,
                                "pull",
                                change,
                                "missing sequence-aware apply result",
                            )
                        elif result["status"] in ("deferred", "rejected"):
                            enqueue_sync_retry(
                                cursor,
                                self.client_id,
                                "pull",
                                change,
                                result.get("reason") or result["status"],
                            )
                            if result["status"] == "rejected":
                                mark_sync_retry_dead(
                                    cursor,
                                    self.client_id,
                                    "pull",
                                    seq,
                                    result.get("reason") or "local apply rejected change",
                                )
                                record_issue(
                                    cursor,
                                    "sync_dead_letter",
                                    ref_table=change.get("table"),
                                    ref_id=change.get("rowId"),
                                    detail={
                                        "direction": "pull",
                                        "seq": seq,
                                        "reason": result.get("reason"),
                                    },
                                )
                        else:
                            self._remember_result_clock(
                                cursor,
                                result,
                                peer_seq=seq,
                                clock_override=(change.get("row") or {}).get(
                                    "updated_at_utc"
                                ),
                            )
                    update_sync_state(
                        cursor, self.client_id, last_pulled_seq=next_seq
                    )

            applied += outcome.applied
            skipped += outcome.skipped
            conflicts.extend(outcome.conflicts)
            duplicates.extend(outcome.duplicates)
            deferred.extend(outcome.deferred)
            rejected.extend(outcome.rejected)
            rounds += 1

            by_id = {(change.get("table"), change.get("rowId")): change for change in changes}
            for ref in outcome.applied_refs:
                change = by_id.get((ref["table"], ref["rowId"]))
                applied_rows.append({**ref, "row": (change or {}).get("row")})

            if next_seq >= int(response.get("maxSeq") or next_seq):
                break

        return {
            "rounds": rounds,
            "applied": applied,
            "skipped": skipped,
            "conflicts": conflicts,
            "duplicates": duplicates,
            "deferred": deferred,
            "rejected": rejected,
            "appliedRows": applied_rows,
        }

    def _retry_pull_queue(
        self, columns_by_table: dict[str, tuple[str, ...]]
    ) -> dict:
        report = {
            "applied": 0,
            "skipped": 0,
            "conflicts": [],
            "duplicates": [],
            "deferred": [],
            "rejected": [],
            "appliedRows": [],
        }
        with get_db_connection(sync_applying=True) as conn:
            with conn.cursor() as cursor:
                queued = list_sync_retries(
                    cursor, self.client_id, "pull", SYNC_BATCH_LIMIT
                )
                if not queued:
                    return report
                changes = [entry["change"] for entry in queued]
                self._add_pull_bases(cursor, changes)
                outcome = apply_changes(
                    cursor,
                    changes,
                    incoming_side=SIDE_SERVER,
                    columns_by_table=columns_by_table or None,
                )
                result_by_seq = {
                    int(result["seq"]): result
                    for result in outcome.results
                    if result.get("seq") is not None
                }
                for entry in queued:
                    seq = int(entry["seq"])
                    result = result_by_seq.get(seq)
                    if result and result["status"] in ("applied", "skipped"):
                        delete_sync_retry(cursor, self.client_id, "pull", seq)
                        self._remember_result_clock(
                            cursor,
                            result,
                            peer_seq=seq,
                            clock_override=(
                                (entry["change"].get("row") or {}).get(
                                    "updated_at_utc"
                                )
                            ),
                        )
                    elif result and result["status"] == "rejected":
                        mark_sync_retry_dead(
                            cursor,
                            self.client_id,
                            "pull",
                            seq,
                            result.get("reason") or "local apply rejected change",
                        )
                        record_issue(
                            cursor,
                            "sync_dead_letter",
                            ref_table=entry.get("table_name"),
                            ref_id=entry.get("row_id"),
                            detail={
                                "direction": "pull",
                                "seq": seq,
                                "reason": result.get("reason"),
                            },
                        )
                    else:
                        mark_sync_retry_failed(
                            cursor,
                            self.client_id,
                            "pull",
                            seq,
                            (result or {}).get("reason")
                            or "missing sequence-aware apply result",
                        )

        report["applied"] = outcome.applied
        report["skipped"] = outcome.skipped
        report["conflicts"] = outcome.conflicts
        report["duplicates"] = outcome.duplicates
        report["deferred"] = outcome.deferred
        report["rejected"] = outcome.rejected
        by_id = {
            (change.get("table"), change.get("rowId")): change for change in changes
        }
        report["appliedRows"] = [
            {
                **ref,
                "row": (by_id.get((ref["table"], ref["rowId"])) or {}).get("row"),
            }
            for ref in outcome.applied_refs
        ]
        return report

    def _add_pull_bases(self, cursor, changes: list[dict]) -> None:
        for change in changes:
            if change.get("baseUpdatedAtUtc") is not None:
                continue
            table = change.get("table")
            row_id = change.get("rowId")
            if table and row_id:
                change["baseUpdatedAtUtc"] = get_row_sync_clock(
                    cursor, self.client_id, table, row_id
                )

    def _remember_result_clock(
        self,
        cursor,
        result: dict,
        peer_seq: int | None = None,
        clock_override: str | None = None,
    ) -> None:
        clock = clock_override or result.get("effectiveUpdatedAtUtc")
        table = result.get("table")
        row_id = result.get("rowId")
        if clock and table and row_id:
            set_row_sync_clock(
                cursor, self.client_id, table, row_id, clock, peer_seq=peer_seq
            )

    def _store_error(self, message: str) -> None:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                update_sync_state(cursor, self.client_id, last_error=message[:1000])

    def _store_blocked(self, exc: SyncBlocked) -> None:
        """중단 사유를 기록한다. 검사 함수의 커넥션은 raise 로 롤백되므로 여기서 남긴다."""
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                update_sync_state(cursor, self.client_id, last_error=exc.message[:1000])
                if exc.kind in ("schema", "clock", "identity"):
                    record_issue(
                        cursor,
                        exc.kind,
                        detail={"message": exc.message, **exc.detail},
                    )

    # -- 로컬 재발행 -------------------------------------------------------

    async def _rebroadcast(self, report: dict) -> None:
        """pull 적용 결과를 로컬 Socket.IO 로 재발행해 열려 있는 탭을 갱신한다."""
        server = self.realtime_server or _lookup_realtime_server()
        if server is None:
            return
        pull = report.get("pull") or {}
        applied_rows = pull.get("appliedRows") or []
        if not applied_rows:
            return

        for entry in applied_rows:
            if entry.get("table") != "memos":
                continue
            row = entry.get("row") or {}
            await server.emit_memo_pulled(
                entry["rowId"],
                row.get("content"),
                row.get("title"),
                row.get("updated_at_utc"),
            )

        await server.emit_sync_applied(
            {
                "origin": "sync-pull",
                "appliedAt": iso_utc(utcnow_naive()),
                "changes": [
                    {
                        "table": entry["table"],
                        "rowId": entry["rowId"],
                        "updatedAtUtc": (entry.get("row") or {}).get("updated_at_utc"),
                    }
                    for entry in applied_rows
                ],
                "memoIds": [
                    entry["rowId"] for entry in applied_rows if entry.get("table") == "memos"
                ],
                "projectIds": [
                    entry["rowId"] for entry in applied_rows if entry.get("table") == "projects"
                ],
                "conflicts": pull.get("conflicts") or [],
                "duplicates": pull.get("duplicates") or [],
            }
        )

    # -- 원격 소켓 구독 ----------------------------------------------------

    async def _run_socket(self) -> None:
        """원격 전역 룸 `sync:<accountId>` 를 구독해 알림 즉시 pull 한다.

        구독은 **가속기**일 뿐이다. 실패하더라도 폴링 안전망(`SYNC_POLL_SECONDS`)과
        로컬 쓰기 감지가 동기화를 계속 굴린다 — 지연만 늘어난다.
        """
        if socketio is None:
            logger.warning("python-socketio missing; sync falls back to polling only")
            return

        attempt = 0
        while not self._stopping.is_set():
            # 실패한 클라이언트를 재사용하면 engineio 내부 상태가 남아 다음 connect 가
            # 조용히 실패한다. 시도마다 새로 만든다.
            client = socketio.AsyncClient(reconnection=False)
            self._register_socket_handlers(client)
            self._socket = client
            try:
                await client.connect(
                    self.peer.socket_url,
                    socketio_path="api/socket.io",
                    headers=self.peer.auth_headers(),
                    transports=["websocket", "polling"],
                    wait_timeout=10,
                )
                attempt = 0
                logger.info("subscribed to peer sync room (%s)", self.peer.root)
                await client.wait()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # noqa: BLE001
                logger.log(
                    logging.WARNING if attempt == 0 else logging.DEBUG,
                    "sync socket subscribe failed (%s) — polling safety net remains",
                    exc,
                )
                attempt += 1
            finally:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._socket = None

            if self._stopping.is_set():
                return
            await asyncio.sleep(min(SOCKET_RETRY_SECONDS * max(attempt, 1), 60))

    def _register_socket_handlers(self, client) -> None:
        @client.on("syncChanged")
        async def on_sync_changed(data):  # noqa: ARG001
            # 원격 변경 알림 → 즉시 pull
            self._wake.set()

        @client.on("syncLockChanged")
        async def on_sync_lock_changed(data):
            # 원격에서 락이 바뀌면 로컬 브라우저에도 알린다 (편집 충돌 방지)
            server = self.realtime_server or _lookup_realtime_server()
            if server is None or not isinstance(data, dict):
                return
            memo_id = data.get("memoId")
            if memo_id:
                await server.emit_lock_state(memo_id, data.get("holder"))


def _lookup_realtime_server():
    try:
        from .realtime import get_realtime_server
    except ImportError:  # pragma: no cover
        from services.realtime import get_realtime_server
    return get_realtime_server()


_daemon: SyncDaemon | None = None


def get_sync_daemon() -> SyncDaemon:
    global _daemon
    if _daemon is None:
        _daemon = SyncDaemon()
    return _daemon
