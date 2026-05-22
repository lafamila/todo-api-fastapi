from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from .config import TODO_ALLOWED_ORIGINS
    from .connectors import init_db
    from .routers.auth import router as auth_router
    from .routers.articles import router as articles_router
    from .routers.daily_tasks import router as daily_tasks_router
    from .routers.memos import router as memos_router
    from .routers.projects import router as projects_router
    from .services.realtime import MemoRealtimeServer
    from .services.session_auth import get_session_service
except ImportError:  # pragma: no cover
    from config import TODO_ALLOWED_ORIGINS
    from connectors import init_db
    from routers.auth import router as auth_router
    from routers.articles import router as articles_router
    from routers.daily_tasks import router as daily_tasks_router
    from routers.memos import router as memos_router
    from routers.projects import router as projects_router
    from services.realtime import MemoRealtimeServer
    from services.session_auth import get_session_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 시 데이터베이스 초기화"""
    init_db()
    print("Todo API Server started successfully")
    yield


fastapi_app = FastAPI(title="Todo API", lifespan=lifespan)

fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=TODO_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

fastapi_app.include_router(auth_router)
fastapi_app.include_router(projects_router)
fastapi_app.include_router(memos_router)
fastapi_app.include_router(articles_router)
fastapi_app.include_router(daily_tasks_router)

realtime_server = MemoRealtimeServer(get_session_service(), TODO_ALLOWED_ORIGINS)
app = realtime_server.wrap_asgi(fastapi_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
