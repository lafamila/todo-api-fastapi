"""동기화 데몬의 프로세스 전역 런타임 상태.

데몬(백그라운드 태스크), `/api/sync/status`(HTTP), 락 위임(Socket.IO 핸들러)이 모두
"지금 온라인인가"를 알아야 한다. 그 판단을 한 곳에 모은다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock

try:
    from ..timeutil import iso_utc, utcnow_naive
except ImportError:  # pragma: no cover
    from timeutil import iso_utc, utcnow_naive


@dataclass
class SyncRuntime:
    online: bool = False
    last_error: str | None = None
    last_error_kind: str | None = None
    last_cycle_at: datetime | None = None
    last_ok_at: datetime | None = None
    peer_schema_version: int | None = None
    clock_skew_seconds: float | None = None
    blocked_reason: str | None = None
    daemon_running: bool = False
    _lock: RLock = field(default_factory=RLock, repr=False)
    _wake: asyncio.Event | None = field(default=None, repr=False)

    def bind_wake_event(self, event: asyncio.Event) -> None:
        self._wake = event

    def request_cycle(self) -> None:
        """데몬을 즉시 한 바퀴 돌게 깨운다 (소켓 알림·수동 트리거)."""
        event = self._wake
        if event is None:
            return
        loop = getattr(event, "_loop", None)
        try:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(event.set)
            else:  # pragma: no cover - 같은 루프 안에서 호출된 경우
                event.set()
        except RuntimeError:  # pragma: no cover - 루프가 이미 닫힘
            pass

    def mark_online(self) -> None:
        with self._lock:
            self.online = True
            self.last_error = None
            self.last_error_kind = None
            self.last_ok_at = utcnow_naive()

    def mark_offline(self, reason: str) -> None:
        with self._lock:
            self.online = False
            self.last_error = reason
            self.last_error_kind = "offline"

    def mark_blocked(self, kind: str, reason: str) -> None:
        """신원/스키마/시계 편차로 동기화를 중단한 상태 (네트워크는 살아있다)."""
        with self._lock:
            self.last_error = reason
            self.last_error_kind = kind
            self.blocked_reason = reason

    def clear_blocked(self) -> None:
        with self._lock:
            self.blocked_reason = None

    def mark_cycle(self) -> None:
        with self._lock:
            self.last_cycle_at = utcnow_naive()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "online": self.online,
                "daemonRunning": self.daemon_running,
                "lastError": self.last_error,
                "lastErrorKind": self.last_error_kind,
                "lastCycleAt": iso_utc(self.last_cycle_at),
                "lastOkAt": iso_utc(self.last_ok_at),
                "peerSchemaVersion": self.peer_schema_version,
                "clockSkewSeconds": self.clock_skew_seconds,
                "blockedReason": self.blocked_reason,
            }


_runtime = SyncRuntime()


def get_sync_runtime() -> SyncRuntime:
    return _runtime
