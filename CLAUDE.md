# todo-api-fastapi

FastAPI backend for todo/project/memo/daily-task/article data. Raw SQL via PyMySQL against MySQL.

> 이 파일이 본 레포의 canonical 가이드입니다. `AGENTS.md` 는 codex 호환용 stub 입니다.

## 워크스페이스 대원칙 (canonical)

이 레포는 `../CLAUDE.md` 의 **DEVELOPMENT PRINCIPLES** 섹션을 따른다. 핵심 재진술:

1. **인증** — `auth-api-nest` access token 을 검증하는 resource server 로 전환 중. 로컬 `users`/password/JWT 는 제거 대상이다.
2. **기능 단위 커밋** — 한 기능이 계획-구현-검토를 통과하면 즉시 1개의 커밋. 여러 기능을 묶지 않는다.
3. **Agent co-author 제외** — Codex, Claude, OmX 등 agent/tool 저자를 `Co-authored-by` trailer 로 추가하지 않는다. 사용자가 명시적으로 요청한 경우만 예외.
4. **계획 → 구현 → 검토** — 계획 단계에서 검토 통과 기준(어떤 테스트/명령이 통과해야 "done"인지)을 명시한다.
5. **Docker 빌드 가능** — DEPLOY. 루트 `docker-compose.yml` 의 `fastapi` 서비스로 등록됨 (포트 8000). Dockerfile 유지 필수.
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
├── __main__.py           # FastAPI app setup and router registration
├── __init__.py           # Empty
└── connectors/
    └── __init__.py       # DB config, get_db_connection() context manager, init_db() DDL
scripts/
└── export_topic_data.py  # Read-only legacy topic export for topic-api-fastapi migration
Dockerfile                # Python 3.11-slim, port 8000
requirements.txt          # fastapi 0.115.5, uvicorn, pymysql, pydantic 2, python-dotenv
```

Topic collection/insight data and embedding similarity search moved to `../topic-api-fastapi`.
This repo keeps only a read-only legacy export script until migration verification is complete.

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add API endpoint | `src/__main__.py` | Add route function + Pydantic request model in same file |
| Change DB schema | `src/connectors/__init__.py` `init_db()` | DDL in raw SQL, auto-runs on startup |
| DB connection | `src/connectors/__init__.py` `get_db_connection()` | Context manager with auto-commit/rollback |
| Environment vars | `.env` → `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | Loaded via `python-dotenv` |
| Export legacy topic data | `scripts/export_topic_data.py` | Read-only JSON export for topic service import |

## DATABASE SCHEMA

```sql
projects (id PK, name, icon, is_secret, password, created_at, updated_at)
memos (id PK, project_id FK→projects, title, content LONGTEXT, created_at, updated_at)
memo_versions (id PK, memo_id FK→memos, content LONGTEXT, version INT, created_at)
```

- `VARCHAR(50)` UUIDs as primary keys (generated via `uuid.uuid4()`)
- Cascading deletes: project → memos → memo_versions
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

- **Single file API** — all routes in `__main__.py` (no router splitting)
- **snake_case DB columns** → **camelCase JSON responses** (manual dict mapping in each route)
- **Pydantic models**: `Create{Entity}Request`, `Verify{Action}Request` for inputs; `Project`, `Memo` for response shapes
- **Context manager** for DB: `with get_db_connection() as conn:` (auto-commit on success, rollback on error)
- **DictCursor** — all `cursor.fetchone()`/`fetchall()` return dicts
- **Korean docstrings** on all route functions
- **All routes under `/api/`** prefix

## ANTI-PATTERNS

- **Project password is legacy** — 중앙 auth 전환 후 제거 대상.
- **`@app.on_event("startup")`** is deprecated — should migrate to `lifespan` context manager
- **No input sanitization** on f-string in `init_db()`: `f"CREATE DATABASE IF NOT EXISTS {DB_CONFIG['database']}"` (SQL injection risk if DB_NAME is user-controlled)
- **Memo versioning** saves old content before update but **no version diffing** — stores full content copies
- **No pagination** on any list endpoint

## API ENDPOINTS

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/projects` | List all projects (excludes password) |
| POST | `/api/projects` | Create project |
| POST | `/api/projects/{id}/verify` | Verify project password |
| GET | `/api/projects/{id}/memos` | List project's memos |
| POST | `/api/memos` | Create memo |
| GET | `/api/memos/{id}` | Get memo detail |
| PUT | `/api/memos/{id}` | Update memo (saves version history) |
| GET | `/api/memos/{id}/versions` | List memo versions |
| GET | `/api/memos/{id}/versions/{v}` | Get specific version |
