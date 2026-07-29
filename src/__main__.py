import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

try:
    from .config import (
        SYNC_DAEMON_AUTOSTART,
        TODO_ALLOWED_ORIGINS,
        runs_sync_daemon,
        serves_sync_peer_api,
        sync_role,
    )
    from .connectors import get_db_connection, init_db
    from .routers.auth import router as auth_router
    from .routers.articles import router as articles_router
    from .routers.daily_tasks import router as daily_tasks_router
    from .routers.memos import router as memos_router
    from .routers.projects import router as projects_router
    from .routers.sync import router as sync_router
    from .services.realtime import MemoRealtimeServer, set_realtime_server
    from .services.session_auth import get_session_service
    from .services.sync_auth import AccountResolutionError, resolve_account_id
    from .services.sync_daemon import SyncDaemon
    from .services.sync_store import max_change_seq
except ImportError:  # pragma: no cover
    from config import (
        SYNC_DAEMON_AUTOSTART,
        TODO_ALLOWED_ORIGINS,
        runs_sync_daemon,
        serves_sync_peer_api,
        sync_role,
    )
    from connectors import get_db_connection, init_db
    from routers.auth import router as auth_router
    from routers.articles import router as articles_router
    from routers.daily_tasks import router as daily_tasks_router
    from routers.memos import router as memos_router
    from routers.projects import router as projects_router
    from routers.sync import router as sync_router
    from services.realtime import MemoRealtimeServer, set_realtime_server
    from services.session_auth import get_session_service
    from services.sync_auth import AccountResolutionError, resolve_account_id
    from services.sync_daemon import SyncDaemon
    from services.sync_store import max_change_seq


logger = logging.getLogger(__name__)

_sync_daemon: SyncDaemon | None = None
# 마지막으로 sync 룸에 알린 change_log seq — 매 요청마다 두 번 조회하지 않기 위한 캐시
_last_notified_seq = 0


def _configure_app_logging() -> None:
    """앱 로거를 uvicorn 핸들러에 붙인다.

    붙이지 않으면 동기화 데몬의 경고("소켓 구독 실패", "스키마 드리프트로 중단" 등)가
    어디에도 출력되지 않아 운영 중 원인 파악이 불가능하다.
    """
    # uvicorn 의 기본 설정은 핸들러를 `uvicorn` 로거에 달고 `uvicorn.error` 는 그곳으로
    # 전파시킨다 — 그래서 핸들러는 `uvicorn` 에서 가져와야 한다.
    uvicorn_logger = logging.getLogger("uvicorn")
    root = logging.getLogger()
    if root.handlers or not uvicorn_logger.handlers:
        return
    for handler in uvicorn_logger.handlers:
        root.addHandler(handler)
    root.setLevel(uvicorn_logger.level or logging.INFO)
    # socket.io/engine.io 는 INFO 가 매우 시끄럽다
    logging.getLogger("engineio").setLevel(logging.WARNING)
    logging.getLogger("socketio").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 시 데이터베이스 초기화 + (클라이언트 역할이면) 동기화 데몬 기동"""
    global _sync_daemon, _last_notified_seq

    _configure_app_logging()
    init_db()
    realtime_server.bind_loop()
    print(f"Todo API Server started successfully (sync role: {sync_role()})")

    if serves_sync_peer_api():
        _last_notified_seq = await asyncio.to_thread(_read_max_seq)

    if runs_sync_daemon() and SYNC_DAEMON_AUTOSTART:
        # 데몬은 API 프로세스 안에서 돈다 — pull 적용 후 로컬 Socket.IO 로 재발행해야
        # 열려 있는 탭이 새로고침 없이 갱신되고, 그 소켓 서버는 이 프로세스 메모리에 있다.
        _sync_daemon = SyncDaemon(realtime_server=realtime_server)
        await _sync_daemon.start()

    try:
        yield
    finally:
        if _sync_daemon is not None:
            await _sync_daemon.stop()


fastapi_app = FastAPI(title="Todo API", lifespan=lifespan)

fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=TODO_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@fastapi_app.middleware("http")
async def notify_sync_change(request: Request, call_next):
    """일반 CRUD 쓰기 뒤에 전역 룸 `sync:<accountId>` 로 알림을 보낸다.

    쓰기 지점 20여 곳을 각각 고치지 않고 한 곳에서 처리한다. 알림은 API 레이어에서
    나가므로 API 밖 변경(수동 SQL 등)은 잡지 못한다 — 그쪽은 폴링 안전망이 담당한다.
    """
    response = await call_next(request)
    if not serves_sync_peer_api():
        return response
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return response
    if request.url.path.startswith("/api/sync/"):
        return response
    if response.status_code >= 400:
        return response

    try:
        await _emit_if_advanced()
    except Exception:  # noqa: BLE001 - 알림 실패가 응답을 막지 않는다
        logger.debug("sync change notification failed", exc_info=True)
    return response


def _read_max_seq() -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            return max_change_seq(cursor)


def _read_seq_and_account() -> tuple[int, str | None]:
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            seq = max_change_seq(cursor)
            try:
                account_id = resolve_account_id(cursor)
            except AccountResolutionError:
                account_id = None
            return seq, account_id


async def _emit_if_advanced() -> None:
    global _last_notified_seq
    seq, account_id = await asyncio.to_thread(_read_seq_and_account)
    if account_id is None or seq <= _last_notified_seq:
        return
    _last_notified_seq = seq
    await realtime_server.emit_sync_changed(account_id, seq)


fastapi_app.include_router(auth_router)
fastapi_app.include_router(projects_router)
fastapi_app.include_router(memos_router)
fastapi_app.include_router(articles_router)
fastapi_app.include_router(daily_tasks_router)
fastapi_app.include_router(sync_router)

realtime_server = MemoRealtimeServer(get_session_service(), TODO_ALLOWED_ORIGINS)
set_realtime_server(realtime_server)
app = realtime_server.wrap_asgi(fastapi_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
