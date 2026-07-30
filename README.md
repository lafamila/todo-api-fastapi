# Todo Backend API

Todo 백엔드 API 서버입니다. FastAPI와 MariaDB를 사용하여 구현되었습니다.

이 레포는 `todo-api-fastapi` 단독 배포를 기준으로 관리합니다. root `docker-compose*.yml` 은 이 앱을 띄우는 용도가 아니라 MariaDB, LiveKit 같은 공통 infra 를 띄우는 용도로만 취급합니다.

## 기술 스택

- **프레임워크**: FastAPI
- **데이터베이스**: MariaDB
- **언어**: Python 3.13

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
DB_PORT=33306
DB_USER=root
DB_PASSWORD=
DB_NAME=teddynote
```

Todo 로그인은 auth-api-nest hosted OIDC login 을 통해 시작됩니다. todo-api-fastapi 는 브라우저로부터 중앙 계정 ID/PW 를 받지 않고, `/api/session/oidc/start` 에서 authorize URL 을 발급한 뒤 `TODO_OIDC_REDIRECT_URI` callback 에서 code 를 교환해 todo 세션 쿠키를 만듭니다.

Auth 계정 검색 기능을 사용하려면 `auth-api-nest` 에 todo 서비스 onboarding request 를 제출하고, `/admin` 에서 승인된 뒤 표시되는 todo 서비스용 service credential 을 서버 환경변수로 주입해야 합니다. 이 값은 승인/rotate 시 한 번만 표시되는 서버 전용 secret 이며 프론트엔드에 노출하면 안 됩니다.

```env
AUTH_API_BASE_URL=http://localhost:3032
TODO_OIDC_CLIENT_ID=todo-web
TODO_OIDC_CLIENT_SECRET=
TODO_OIDC_REDIRECT_URI=http://localhost:8000/api/todo/session/callback
TODO_WEB_BASE_URL=http://localhost:3034
AUTH_SERVICE_KEY_ID=todo_service_key_id
AUTH_SERVICE_SECRET=
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

### 4. Docker

운영 이미지는 Python 3.13 slim 기반이며 non-root 사용자로 실행됩니다.
이미지만 독립적으로 빌드하려면 다음 명령을 사용합니다.

```bash
docker build -t todo-api-fastapi .
```

로컬 Docker 스택은 workspace root 의 `todoctl` 로 조작합니다 (`TODO_MODE`
2×2 체계 — URL/DB/쿠키/sync 역할은 `src/config.py` 의 모드 프리셋이 결정하고,
compose 는 모드 선언과 비밀 파일만 주입합니다. 상세: `CLAUDE.md` TODO_MODE 섹션).

```bash
# Workspace root
.scripts/todoctl status
.scripts/todoctl up local          # prod-local (:20022/:3030) — 고정 이미지 실사용
.scripts/todoctl up dev            # dev 페어 (:20023/:30333 ↔ :20024/:30334) — 핫리로드
.scripts/todoctl local update      # origin/main → 이미지 재빌드 → prod-local 반영
.scripts/todoctl down dev|local
```

비밀 파일은 이 레포의 `.env.local`(prod-local)·`.env.dev`(dev 페어 공유,
로컬 auth 기준)이며 untracked 입니다. 최초 생성은 `todoctl setup local|dev`
가 안내합니다. 실제 비밀번호, OIDC client secret, service credential,
LiveKit secret 은 이미지나 Compose 파일에 기록하지 마세요.

운영에서는 app Compose를 사용하지 않습니다. `/volume1/www` 아래에 두 레포와
workspace `.scripts`가 있다고 가정하고 다음 스크립트가 pull → build → 기존
container 제거 → `teddy-infra` network에서 run → health 확인을 수행합니다.

```bash
cd /volume1/www
./.scripts/deploy-todo-prod.sh --dry-run
./.scripts/deploy-todo-prod.sh
```

운영 `.env` 는 `TODO_MODE=prod-prod` 선언과 비밀값만 남기는 형태가 표준입니다
(DB/URL/쿠키는 프리셋이 결정 — `DB_HOST=teddy-mysql` 포함). 기동 시 preflight 가
누락·형식 오류를 즉시 거부합니다.

## 배포/연동 메모

- DB 는 root infra compose 의 MariaDB/MySQL 또는 운영 DB 를 사용합니다.
- Auth 는 독립 배포된 `auth-api-nest` 의 URL/JWKS/service credential 을 사용합니다.
- LiveKit 토큰 API 를 쓰는 경우 `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` 를 별도 주입해야 합니다.
- 프론트엔드 origin 과 세션 쿠키 동작은 `TODO_ALLOWED_ORIGINS`, `TODO_SESSION_COOKIE_*`, `TODO_WEB_BASE_URL` 로 맞춥니다.

## 오프라인 동기화 (`SYNC_*`)

같은 이미지가 양쪽에 배포되고 **역할은 env 로 갈립니다**. 자세한 설계·정책은
`CLAUDE.md` 의 **OFFLINE SYNC** 섹션을 보세요.

| 배포 대상 | 필수 env |
|---|---|
| 운영(NAS) = 동기화 **서버** | `SYNC_ENABLED=true`, `SYNC_PEER_URL` **비움**, `SYNC_ACCOUNT_ID`, `AUTH_SERVICE_KEY_ID`/`AUTH_SERVICE_SECRET`(자기 인증용), 선택 `AUTH_VERIFY_URL` |
| 노트북 = 동기화 **클라이언트** | `SYNC_ENABLED=true`, `SYNC_PEER_URL=https://todo.lafamila.xyz`, `SYNC_KEY_ID`/`SYNC_SECRET`(auth 발급 scope `sync`), `SYNC_CLIENT_ID`, `TODO_LOCAL_SESSION_ENABLED=true` |
| 개발용 2번째 스택 | `SYNC_ENABLED=false` (그 외 `SYNC_*` 불필요) |

튜닝값은 전부 기본값이 있습니다: `SYNC_POLL_SECONDS`(60), `SYNC_PUSH_DEBOUNCE_MS`(1000),
`SYNC_OFFLINE_BACKOFF_SECONDS`(30), `SYNC_CLOCK_SKEW_LIMIT_SECONDS`(5),
`SYNC_VERIFY_CACHE_SECONDS`(300), `SYNC_BATCH_LIMIT`(500), `SYNC_ALLOW_SCHEMA_DRIFT`(false),
`SYNC_DAEMON_AUTOSTART`(true), `SYNC_LOCK_TTL_SECONDS`(120), `SYNC_HTTP_TIMEOUT_SECONDS`(15),
`SYNC_REQUIRED_SCOPE`(sync), `SYNC_BACKUP_DIR`(`../.backups/db`). 전체 목록은 `.env.example`.

```bash
# 상태 진단 (신원·스키마·커서·이슈·트리거)
venv/bin/python -m src.sync_cli doctor

# 최초 1회: 로컬 owner id 를 원격 계정 id 로 맞춤
venv/bin/python -m src.sync_cli link-identity --dry-run
venv/bin/python -m src.sync_cli link-identity

# 일시 정지 / 재개
venv/bin/python -m src.sync_cli pause
venv/bin/python -m src.sync_cli resume
```

`updated_at_utc` 백필은 서버 기동 시 `init_db()` 가 멱등하게 처리합니다.
명시적으로 확인/실행하려면 `venv/bin/python scripts/backfill_updated_at_utc.py --dry-run`.

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
