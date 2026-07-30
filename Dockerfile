# Standalone deploy image for the todo-api-fastapi backend.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Seoul \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# `.env`는 이미지에 복사하지 않고 런타임에만 주입한다.
COPY --chown=app:app src/ ./src/
# COPY 는 빌드 컨텍스트의 모드 비트를 보존한다 — NAS 체크아웃 umask 가 제한적이면
# 디렉토리 x 비트가 빠져 소유자(app)도 import 를 못 한다. 모드를 정규화한다.
RUN chmod -R u+rwX,go+rX ./src

EXPOSE 8000

# 전용 health 라우트가 없어 FastAPI 기본 /docs 로 liveness 확인 (라우트 추가 시 /api/health 로 교체)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/docs', timeout=4)" || exit 1

# 빌드된 소스의 git ref. todoctl 이 prod-local 이미지를 만들 때 주입하고 `todoctl status` 가 읽는다.
# 마지막에 둬서 ref 가 바뀌어도 위 레이어 캐시를 깨지 않는다. 미주입 시 빈 값.
ARG TODO_BUILD_REF=""
ENV TODO_BUILD_REF=${TODO_BUILD_REF}
LABEL org.opencontainers.image.revision=${TODO_BUILD_REF}

USER app

CMD ["uvicorn", "src.__main__:app", "--host", "0.0.0.0", "--port", "8000"]
