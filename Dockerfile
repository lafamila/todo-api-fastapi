# Standalone deploy image for the todo-api-fastapi backend.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Seoul \
    PYTHONPATH=/app/src

WORKDIR /app

# system deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# deps
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# app source ('.env'는 이미지에 복사하지 않고 런타임 env 로 주입)
COPY src/ ./src/

EXPOSE 8000

# 전용 health 라우트가 없어 FastAPI 기본 /docs 로 liveness 확인 (라우트 추가 시 /api/health 로 교체)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/docs', timeout=4)" || exit 1

# 0.0.0.0:8000 로 바인딩
CMD ["uvicorn", "src.__main__:app", "--host", "0.0.0.0", "--port", "8000"]
