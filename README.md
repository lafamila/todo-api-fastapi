# Todo Backend API

Todo 백엔드 API 서버입니다. FastAPI와 MariaDB를 사용하여 구현되었습니다.

이 레포는 `todo-api-fastapi` 단독 배포를 기준으로 관리합니다. root `docker-compose*.yml` 은 이 앱을 띄우는 용도가 아니라 MariaDB, LiveKit 같은 공통 infra 를 띄우는 용도로만 취급합니다.

## 기술 스택

- **프레임워크**: FastAPI
- **데이터베이스**: MariaDB
- **언어**: Python 3.8+

## 디렉터리 구조

```
todo-api-fastapi/
├── requirements.txt          # 필요 라이브러리
├── .env                      # 환경변수 설정
└── src/
    ├── __main__.py          # FastAPI 메인 코드 (API 엔드포인트)
    └── connectors/
        └── __init__.py      # MariaDB connection 관련 코드
```

## 설치 및 실행

### 1. 필요 라이브러리 설치

```bash
pip install -r requirements.txt
```

### 2. MariaDB 설정

MariaDB는 root infra compose 또는 별도 운영 DB 를 사용하면 됩니다. 로컬 실행 전에는 `.env.example` 을 복사해 `.env` 를 만들고 환경변수를 채우세요:

```env
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your_password
DB_NAME=todo
```

Todo 로그인은 auth-api-nest hosted OIDC login 을 통해 시작됩니다. todo-api-fastapi 는 브라우저로부터 중앙 계정 ID/PW 를 받지 않고, `/api/session/oidc/start` 에서 authorize URL 을 발급한 뒤 `TODO_OIDC_REDIRECT_URI` callback 에서 code 를 교환해 todo 세션 쿠키를 만듭니다.

Auth 계정 검색 기능을 사용하려면 `auth-api-nest` 에 todo 서비스 onboarding request 를 제출하고, `/admin` 에서 승인된 뒤 표시되는 todo 서비스용 service credential 을 서버 환경변수로 주입해야 합니다. 이 값은 승인/rotate 시 한 번만 표시되는 서버 전용 secret 이며 프론트엔드에 노출하면 안 됩니다.

```env
AUTH_API_BASE_URL=http://localhost:3032
TODO_OIDC_CLIENT_ID=todo-web
TODO_OIDC_CLIENT_SECRET=todo_oidc_client_secret
TODO_OIDC_REDIRECT_URI=http://localhost:8000/api/todo/session/callback
TODO_WEB_BASE_URL=http://localhost:3034
AUTH_SERVICE_KEY_ID=todo_service_key_id
AUTH_SERVICE_SECRET=todo_service_secret
```

Todo OIDC client, permission definitions, service credential scope 변경은 auth admin 에서 직접 수정하지 않고 todo 서비스가 service onboarding update request 를 제출한 뒤 승인받는 방식으로 진행합니다.

### 3. 서버 실행

로컬 개발 실행:

```bash
python -m src
```

또는

```bash
cd src
python __main__.py
```

서버는 `http://localhost:8000`에서 실행됩니다.

### 4. Docker 이미지 빌드

독립 배포용 이미지 빌드:

```bash
docker build -t todo-api-fastapi .
```

이미지 실행 시 `.env` 또는 운영 환경변수를 런타임에 주입하세요. 이 레포는 더 이상 root compose 의 `fastapi` 앱 서비스명을 기준으로 설명하지 않습니다.

## 배포/연동 메모

- DB 는 root infra compose 의 MariaDB/MySQL 또는 운영 DB 를 사용합니다.
- Auth 는 독립 배포된 `auth-api-nest` 의 URL/JWKS/service credential 을 사용합니다.
- LiveKit 토큰 API 를 쓰는 경우 `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` 를 별도 주입해야 합니다.
- 프론트엔드 origin 과 세션 쿠키 동작은 `TODO_ALLOWED_ORIGINS`, `TODO_SESSION_COOKIE_*`, `TODO_WEB_BASE_URL` 로 맞춥니다.

## API 엔드포인트

### Session API

- `POST /api/session/oidc/start` - auth-api authorize URL 생성
- `GET /api/todo/session/callback` - auth callback code 를 todo 세션 쿠키로 교환
- `GET /api/session/me` - 현재 todo 세션 조회
- `POST /api/session/logout` - todo 세션 종료

### Projects API

- `GET /api/projects` - 모든 프로젝트 조회
- `POST /api/projects` - 프로젝트 생성
- `POST /api/projects/{id}/verify` - 프로젝트 비밀번호 검증
- `GET /api/projects/{id}/memos` - 특정 프로젝트의 메모 목록 조회

### Memos API

- `POST /api/memos` - 메모 생성
- `GET /api/memos/{id}` - 메모 조회
- `PUT /api/memos/{id}` - 메모 업데이트

## 데이터베이스 스키마

### projects 테이블

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | VARCHAR(50) | 프로젝트 ID (PK) |
| name | VARCHAR(255) | 프로젝트 이름 |
| icon | VARCHAR(10) | 프로젝트 아이콘 |
| is_secret | BOOLEAN | 비밀 프로젝트 여부 |
| password | VARCHAR(255) | 프로젝트 비밀번호 |
| created_at | DATETIME | 생성 시간 |
| updated_at | DATETIME | 수정 시간 |

### memos 테이블

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | VARCHAR(50) | 메모 ID (PK) |
| project_id | VARCHAR(50) | 프로젝트 ID (FK) |
| title | VARCHAR(255) | 메모 제목 |
| content | LONGTEXT | 메모 내용 |
| created_at | DATETIME | 생성 시간 |
| updated_at | DATETIME | 수정 시간 |

## API 문서

서버 실행 후 다음 URL에서 Swagger UI를 통해 API 문서를 확인할 수 있습니다:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## CORS 설정

프론트엔드 개발 서버 origin 은 `TODO_ALLOWED_ORIGINS` 환경변수에서 관리합니다.

## 자동 초기화

서버 시작 시 자동으로:
- 데이터베이스가 존재하지 않으면 생성
- 필요한 테이블이 존재하지 않으면 생성

따라서 처음 실행 시 별도의 마이그레이션 작업이 필요하지 않습니다.
