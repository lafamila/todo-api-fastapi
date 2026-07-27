# todo-api-fastapi

FastAPI backend for todo/project/memo/daily-task/article data. Raw SQL via PyMySQL against MySQL.

> 이 파일이 본 레포의 canonical 가이드입니다. `AGENTS.md` 는 codex 호환용 stub 입니다.

- **Lifecycle**: DEPLOY
- **Status**: active
- **Port**: 8000
- **Auth**: `auth-api-nest-oidc-session` (중앙 OIDC 로그인 — 세션 쿠키/opaque 세션은 이 API 가 소유)

## 워크스페이스 대원칙 (canonical)

이 레포는 `../CLAUDE.md` 의 **DEVELOPMENT PRINCIPLES** 섹션을 따른다. 핵심 재진술:

1. **인증** — `auth-api-nest` access token 을 검증하는 resource server 로 전환 중. 로컬 `users`/password/JWT 는 제거 대상이다.
2. **기능 단위 커밋** — 한 기능이 계획-구현-검토를 통과하면 즉시 1개의 커밋. 여러 기능을 묶지 않는다.
3. **Agent co-author 제외** — Codex, Claude, OmX 등 agent/tool 저자를 `Co-authored-by` trailer 로 추가하지 않는다. 사용자가 명시적으로 요청한 경우만 예외.
4. **계획 → 구현 → 검토** — 계획 단계에서 검토 통과 기준(어떤 테스트/명령이 통과해야 "done"인지)을 명시한다.
5. **Docker 빌드 가능** — DEPLOY. 이 레포는 포트 8000의 독립 배포 backend 이며, root `docker-compose*.yml` 은 앱 컨테이너가 아니라 공통 infra 용도로만 취급한다. Dockerfile 유지 필수.
6. **Cross-repo 영향 보고** — 이 레포의 변경이 다른 repo, 공통 API 계약, auth claim/permission, env var, Docker/deploy 설정, 공통 문서에 영향을 준다고 판단되면 현재 orchestrator 에게 반드시 보고한다. 직접 보고할 수 없으면 워크스페이스 루트 `../.idea/` 에 `{REPO_NAME}_CROSS_REPO_IMPACT_{YYYYMMDD}.md` 형식의 handoff 문서를 남긴다.
7. **사용자 결정 필요사항 에스컬레이션** — 사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않고 작업을 중단한 뒤 현재 orchestrator 에게 전달하여 결정받고 진행한다. orchestrator 에 보고할 수 없으면 workspace root `../.idea/` 에 handoff 문서를 남긴다.

## Feature Workflow (대원칙 #3 의 이 레포 적용)

1. `.idea/` 또는 신규 계획 문서에서 기능을 선택
2. 계획서 작성 — 변경 파일, 인터페이스, **검토 통과 기준** 명시
3. 구현
4. 통과 기준 만족 여부 직접 실행/테스트 (uvicorn 기동 후 엔드포인트 호출 확인, DB DDL 영향이 있으면 init_db 재실행)
5. 통과 시 1개의 커밋으로 마무리

## STRUCTURE

```
src/
├── __main__.py           # FastAPI app 생성(lifespan), CORS, 라우터 등록, Socket.IO ASGI wrap
├── config.py             # .env 로딩 — DB_*, AUTH_*, TODO_*, LIVEKIT_* 설정값
├── auth_utils.py         # 토큰/권한 유틸
├── token_verifier.py     # auth-api-nest JWKS 검증
├── utils.py
├── connectors/
│   └── __init__.py       # DB config, get_db_connection() context manager, init_db() DDL
├── models/               # Pydantic 모델 (auth, base, daily_tasks)
├── routers/              # APIRouter 모듈 — auth, projects, memos, articles, daily_tasks
└── services/             # session_auth(OIDC 세션), realtime(Socket.IO), livekit(토큰 발급)
scripts/
├── export_topic_data.py  # Read-only legacy topic export for topic-api-fastapi migration
└── migrate_legacy_todo.py # 레거시 todo DB(:3030 스택) → 이 스키마 마이그레이션 (additive upsert, 접속정보 CLI 파라미터)
Dockerfile                # Python 3.11-slim, port 8000, HEALTHCHECK 포함
requirements.txt          # fastapi 0.115.5, uvicorn, pymysql, pydantic 2, python-dotenv 등
```

Topic collection/insight data and embedding similarity search moved to `../topic-api-fastapi`.
This repo keeps only a read-only legacy export script until migration verification is complete.

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add API endpoint | `src/routers/{domain}.py` | 라우터 모듈에 route + Pydantic request model 추가, `__main__.py` 에 등록 |
| Change DB schema | `src/connectors/__init__.py` `init_db()` | DDL in raw SQL, auto-runs on startup |
| DB connection | `src/connectors/__init__.py` `get_db_connection()` | Context manager with auto-commit/rollback |
| Environment vars | `.env` / `.env.example` → `DB_*`, `AUTH_*`, `TODO_*`, `LIVEKIT_*` | Loaded via `python-dotenv`; deploy 시 런타임 env 주입 |
| Export legacy topic data | `scripts/export_topic_data.py` | Read-only JSON export for topic service import |

## DATABASE SCHEMA

`src/connectors/__init__.py` `init_db()` 의 DDL 이 canonical 이다. 현재 7개 테이블:

```text
projects               (id PK, name, icon, status, is_secret, password[legacy], created_at, updated_at)
memos                  (id PK, project_id FK→projects, title, content LONGTEXT, status, deleted_at, ...)
memo_versions          (id PK, memo_id FK→memos, content LONGTEXT, version INT, created_at)
articles               (id PK, memo_id FK→memos UNIQUE, project_id FK→projects, author_id, author_slug, title, content, published_version, ...)
project_members        (id PK, project_id FK→projects, user_id, username, display_name, email, role, invited_at; UNIQUE(project_id,user_id))
daily_task_types       (id PK, name UNIQUE, icon, color, display_order, is_active, ...)
daily_task_completions (id PK, task_type_id FK→daily_task_types, completed_date DATE, total_active_count; UNIQUE(task_type_id,completed_date))
```

- **스키마 드리프트 주의**: 운영/로컬 실DB 에는 `projects.owner_id`, `memos.created_by` 컬럼이 존재하지만
  `init_db()` DDL 에는 없다 (과거 ALTER 산물). 마이그레이션 스크립트는 이를 런타임에 감지해 대응한다.
- 레거시 todo(:3030) 데이터 이관: `scripts/migrate_legacy_todo.py` — additive/idempotent upsert
  (`legacy-*` 결정적 id), `--replace` 는 `--confirm-replace <db>` 필수, 대상 접속정보는 CLI 파라미터로
  원격(prod) DB 지정 가능. detail 히스토리는 `memo_versions` 로 보존된다.
- `VARCHAR(50)` UUIDs as primary keys (generated via `uuid.uuid4()`)
- Cascading deletes: project → memos → memo_versions / articles / project_members, daily_task_types → daily_task_completions
- InnoDB, utf8mb4

## TODO AUTHZ CONTRACT

서비스 권한(`auth-api-nest` service permission claim)과 프로젝트 역할(`project_members.role`)은 별도 축이다.

### Service Permission

| Permission | Meaning | Main capabilities |
|------------|---------|-------------------|
| `owner` | 슈퍼관리자. Teddy 개인 계정. | 전체 todo 서비스 관리, 모든 프로젝트/게시글 관리, Daily Task Tracker 관리 |
| `admin` | todo 서비스 관리자. | 자기 프로젝트 생성, 자기 프로젝트 멤버 검색/초대, `editor`/`viewer` 지정, 자기 slug 게시판에 publish/delete |
| `user` | todo 서비스 접근 가능 일반 사용자. | 초대받은 프로젝트에서 프로젝트 역할에 따라 활동 |
| `visitor` | todo 서비스 접근 신청 필요. | protected todo API 접근 불가 |

`is_admin` 호환 필드는 `owner`/`admin` 에만 true 로 매핑한다. `is_super_admin` 호환 필드는 `owner` 에만 true 로 매핑한다.

### Project Role

| Role | Scope | Capabilities |
|------|-------|--------------|
| `owner` | 특정 프로젝트 | 멤버 관리, 메모 생성/수정/삭제, publish/delete |
| `editor` | 특정 프로젝트 | 메모 생성/수정 |
| `viewer` | 특정 프로젝트 | 읽기 전용 |

프로젝트 생성자는 자동으로 project `owner` 가 된다. 초대 가능한 역할은 `editor` 또는 `viewer` 이다.

### Article Board

게시글은 service `admin`/`owner` 가 자신의 프로젝트 메모를 자신의 `authorSlug` 게시판으로 발행한다. `authorSlug` 는 auth token 의 `preferred_username`/email/sub 기반으로 생성하며, 공개 목록은 전체 또는 slug 별로 조회할 수 있다.

## CONVENTIONS

- **Router 분할** — 라우트는 `src/routers/{domain}.py` 의 `APIRouter` 로 나뉘고 `__main__.py` 에서 등록한다 (과거 single-file 패턴은 폐기됨)
- **snake_case DB columns** → **camelCase JSON responses** (manual dict mapping in each route)
- **Pydantic models**: `Create{Entity}Request`, `Verify{Action}Request` for inputs; `Project`, `Memo` for response shapes
- **Context manager** for DB: `with get_db_connection() as conn:` (auto-commit on success, rollback on error)
- **DictCursor** — all `cursor.fetchone()`/`fetchall()` return dicts
- **Korean docstrings** on all route functions
- **All routes under `/api/`** prefix

## ANTI-PATTERNS

- **Project password is legacy** — 중앙 auth 전환 후 제거 대상.
- ~~`@app.on_event("startup")`~~ — **해결됨**: `lifespan` context manager 로 전환 완료. 새 코드에서 `on_event` 재도입 금지.
- **No input sanitization** on f-string in `init_db()`: `f"CREATE DATABASE IF NOT EXISTS {DB_CONFIG['database']}"` (SQL injection risk if DB_NAME is user-controlled)
- **Memo versioning** saves old content before update but **no version diffing** — stores full content copies
- **No pagination** on any list endpoint

## API ENDPOINTS

`src/routers/` 의 라우트 정의가 canonical 이다 (총 34개, 전부 `/api/*`). 카테고리 요약:

| Category | Router | Routes |
|----------|--------|--------|
| Auth/session | `routers/auth.py` | `POST /api/session/oidc/start`, `POST /api/session/logout`, `GET /api/session/me`, `POST /api/session/service-application`, `GET /api/users/search`, `POST /api/livekit/token` |
| Projects | `routers/projects.py` | `GET/POST /api/projects`, `POST /api/projects/{id}/verify`, 멤버 관리 `GET/POST /api/projects/{id}/members`, `DELETE /api/projects/{id}/members/{userId}` |
| Memos | `routers/memos.py` | `GET /api/projects/{id}/memos`, `POST /api/memos`, `GET/PUT/DELETE /api/memos/{id}`, `POST /api/memos/bulk-delete`, 버전 `GET /api/memos/{id}/versions[/{v}]` |
| Articles | `routers/articles.py` | `POST/GET /api/articles`, `GET /api/articles/boards/{slug}`, `GET/DELETE /api/articles/{id}`, `GET /api/memos/{id}/article` |
| Daily tasks | `routers/daily_tasks.py` | `/api/daily-tasks` prefix — `POST/GET/PUT/DELETE .../types[/{id}]`, `POST .../complete`, `DELETE .../complete/{typeId}/{date}`, `GET .../calendar[/{date}]` |

Socket.IO realtime 서버(`services/realtime.py`)가 FastAPI app 을 ASGI wrap 한다. 전용 health 엔드포인트는 없다 (Docker HEALTHCHECK 는 `GET /docs` 사용).
