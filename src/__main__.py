from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from .connectors import init_db
    from .routers.articles import router as articles_router
    from .routers.memos import router as memos_router
    from .routers.projects import router as projects_router
    from .routers.topics import router as topics_router
    from .routers.topics import sources_router as topic_sources_router
    from .routers.daily_tasks import router as daily_tasks_router
except ImportError:  # pragma: no cover
    from connectors import init_db
    from routers.articles import router as articles_router
    from routers.memos import router as memos_router
    from routers.projects import router as projects_router
    from routers.topics import router as topics_router
    from routers.topics import sources_router as topic_sources_router
    from routers.daily_tasks import router as daily_tasks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 시 데이터베이스 초기화"""
    init_db()
    print("Todo API Server started successfully")
    yield


app = FastAPI(title="Todo API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects_router)
app.include_router(memos_router)
app.include_router(articles_router)
app.include_router(topics_router)
app.include_router(topic_sources_router)
app.include_router(daily_tasks_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
