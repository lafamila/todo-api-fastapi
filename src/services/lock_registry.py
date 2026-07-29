"""메모 편집 락 레지스트리 — 소켓 핸들러와 위임용 HTTP 엔드포인트가 **같은** 저장소를 쓴다.

노드 안에서만 유효한 락은 온라인 동시 편집을 막지 못한다. 그래서 서버 역할이 이 레지스트리를
단일 진실로 들고, 클라이언트 역할은 온라인일 때 락/언락/보유자조회를 서버에 위임한다.
그 결과 **온라인 동시 편집 충돌이 구조적으로 사라지고**, 충돌은 진짜 오프라인 편집만 남는다.

락은 TTL 임대다. 프로세스가 죽거나 노드가 사라져도 임대가 만료되면 자동으로 풀린다.
"""

from __future__ import annotations

from dataclasses import dataclass
from secrets import token_urlsafe
from threading import RLock, Timer
from time import monotonic
from typing import Callable

try:
    from ..config import SYNC_LOCK_TTL_SECONDS
except ImportError:  # pragma: no cover
    from config import SYNC_LOCK_TTL_SECONDS


@dataclass
class LockHolder:
    owner_key: str
    user_id: str
    display_name: str
    lease_token: str
    generation: int
    expires_at: float

    def to_dict(self) -> dict:
        return {
            "ownerKey": self.owner_key,
            "userId": self.user_id,
            "displayName": self.display_name,
            "leaseToken": self.lease_token,
            "generation": self.generation,
        }


# (memoId, 새 보유자|None, 직전 보유자|None) — 직전 보유자로 변경의 출처(로컬 소켓 vs 위임 피어)를 판별한다
ChangeListener = Callable[[str, dict | None, dict | None], None]


class LockRegistry:
    def __init__(self, ttl_seconds: float | None = None) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else SYNC_LOCK_TTL_SECONDS
        self._locks: dict[str, LockHolder] = {}
        self._generations: dict[str, int] = {}
        self._expiry_timers: dict[str, Timer] = {}
        self._lock = RLock()
        self._listeners: list[ChangeListener] = []

    def on_change(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    def _notify(self, memo_id: str, holder: dict | None, previous: dict | None = None) -> None:
        for listener in list(self._listeners):
            try:
                listener(memo_id, holder, previous)
            except Exception:  # noqa: BLE001 - 알림 실패가 락 동작을 막지 않는다
                pass

    def _live_holder(self, memo_id: str) -> LockHolder | None:
        holder = self._locks.get(memo_id)
        if holder is None:
            return None
        if holder.expires_at <= monotonic():
            return None
        return holder

    def _schedule_expiry(self, memo_id: str, holder: LockHolder) -> None:
        previous_timer = self._expiry_timers.pop(memo_id, None)
        if previous_timer is not None:
            previous_timer.cancel()
        timer = Timer(
            max(holder.expires_at - monotonic(), 0),
            self._expire,
            args=(memo_id, holder.owner_key, holder.expires_at),
        )
        timer.daemon = True
        self._expiry_timers[memo_id] = timer
        timer.start()

    def _expire(self, memo_id: str, owner_key: str, expires_at: float) -> None:
        """정확한 임대 세대가 만료됐을 때만 해제하고 클라이언트에 알린다."""
        expired: LockHolder | None = None
        with self._lock:
            current = self._locks.get(memo_id)
            if (
                current is not None
                and current.owner_key == owner_key
                and current.expires_at == expires_at
            ):
                if current.expires_at <= monotonic():
                    expired = self._locks.pop(memo_id)
                    self._expiry_timers.pop(memo_id, None)
                else:
                    # Timer가 드물게 조금 일찍 깨어나면 남은 임대를 다시 예약한다.
                    self._schedule_expiry(memo_id, current)
        if expired is not None:
            self._notify(memo_id, None, expired.to_dict())

    def holder(self, memo_id: str) -> dict | None:
        with self._lock:
            holder = self._live_holder(memo_id)
            return holder.to_dict() if holder else None

    def validate(
        self,
        memo_id: str,
        lease_token: str | None,
        *,
        user_id: str | None = None,
        owner_key: str | None = None,
    ) -> bool:
        """현재 임대의 opaque token 과 선택적 소유자 신원을 함께 검증한다."""
        if not lease_token:
            return False
        with self._lock:
            holder = self._live_holder(memo_id)
            return bool(
                holder
                and holder.lease_token == lease_token
                and (user_id is None or holder.user_id == user_id)
                and (owner_key is None or holder.owner_key == owner_key)
            )

    def acquire(
        self, memo_id: str, owner_key: str, user_id: str, display_name: str
    ) -> tuple[bool, dict]:
        """`(획득 여부, 현재 보유자)`.

        같은 owner의 heartbeat는 token/generation을 유지하고 만료만 연장한다. 저장
        요청과 heartbeat가 교차할 때 막 발급받은 token이 무효화되는 race를 피한다.
        """
        with self._lock:
            current = self._live_holder(memo_id)
            if current is not None and current.owner_key != owner_key:
                return False, current.to_dict()
            if current is not None:
                holder = current
                holder.expires_at = monotonic() + self._ttl
                holder.user_id = user_id
                holder.display_name = display_name
            else:
                generation = self._generations.get(memo_id, 0) + 1
                self._generations[memo_id] = generation
                holder = LockHolder(
                    owner_key=owner_key,
                    user_id=user_id,
                    display_name=display_name,
                    lease_token=token_urlsafe(32),
                    generation=generation,
                    expires_at=monotonic() + self._ttl,
                )
            self._locks[memo_id] = holder
            self._schedule_expiry(memo_id, holder)
            changed = current is None
        if changed:
            self._notify(memo_id, holder.to_dict(), None)
        return True, holder.to_dict()

    def release(self, memo_id: str, owner_key: str | None = None, force: bool = False) -> dict | None:
        """해제된 보유자를 반환한다 (해제하지 않았으면 None)."""
        with self._lock:
            current = self._live_holder(memo_id)
            if current is None:
                return None
            if not force and owner_key is not None and current.owner_key != owner_key:
                return None
            self._locks.pop(memo_id, None)
            timer = self._expiry_timers.pop(memo_id, None)
            if timer is not None:
                timer.cancel()
        self._notify(memo_id, None, current.to_dict())
        return current.to_dict()

    def release_all_for_owner(self, owner_key: str) -> list[str]:
        """소켓 종료 등으로 owner 가 사라졌을 때 그 owner 의 모든 락을 푼다."""
        with self._lock:
            released = [
                (memo_id, holder.to_dict())
                for memo_id, holder in list(self._locks.items())
                if holder.owner_key == owner_key
            ]
            for memo_id, _ in released:
                self._locks.pop(memo_id, None)
                timer = self._expiry_timers.pop(memo_id, None)
                if timer is not None:
                    timer.cancel()
        for memo_id, holder in released:
            self._notify(memo_id, None, holder)
        return [memo_id for memo_id, _ in released]

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                memo_id: holder.to_dict()
                for memo_id in list(self._locks)
                if (holder := self._live_holder(memo_id)) is not None
            }


_registry = LockRegistry()


def get_lock_registry() -> LockRegistry:
    return _registry
