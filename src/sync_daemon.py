"""동기화 데몬 단독 실행 진입점 — `python -m src.sync_daemon`.

평소에는 API 프로세스 안에서 도는 것이 기본이다 (`SYNC_DAEMON_AUTOSTART=true`).
pull 적용 후 열려 있는 브라우저 탭을 갱신하려면 로컬 Socket.IO 서버에 재발행해야 하고,
그 서버는 API 프로세스 메모리에 있기 때문이다.

이 진입점은 디버깅·수동 1회 실행용이며 **로컬 재발행이 빠진다** (탭은 다음 새로고침이나
폴링으로 갱신된다).

    python -m src.sync_daemon --once     # 한 사이클만 돌고 리포트 출력
    python -m src.sync_daemon            # 계속 돈다
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

try:
    from .config import sync_role
    from .services.sync_daemon import SyncDaemon
except ImportError:  # pragma: no cover
    from config import sync_role
    from services.sync_daemon import SyncDaemon


async def _run(once: bool) -> int:
    daemon = SyncDaemon()
    if not daemon.peer.configured:
        print(
            "SYNC_PEER_URL / SYNC_KEY_ID / SYNC_SECRET 가 모두 필요합니다 "
            f"(현재 역할: {sync_role()})",
            file=sys.stderr,
        )
        return 2

    if once:
        report = await daemon.run_once()
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report.get("ok") else 1

    await daemon.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await daemon.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="todo 동기화 데몬 (단독 실행)")
    parser.add_argument("--once", action="store_true", help="한 사이클만 실행하고 종료")
    parser.add_argument("--verbose", action="store_true", help="디버그 로그")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_run(args.once))


if __name__ == "__main__":
    sys.exit(main())
