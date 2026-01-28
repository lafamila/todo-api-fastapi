# backend/Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Seoul

WORKDIR /app

# system deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# deps
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# app source ('.env'는 이미지에 복사하지 않음)
COPY src/ ./src/

EXPOSE 20022

# 0.0.0.0:20022 로 바인딩
CMD ["uvicorn", "src.__main__:app", "--host", "0.0.0.0", "--port", "20022"]
