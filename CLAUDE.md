# todo-api-fastapi

FastAPI backend owning all data. Raw SQL via PyMySQL against MySQL. Single-file API.

> 이 파일이 본 레포의 canonical 가이드입니다. `AGENTS.md` 는 codex 호환용 stub 입니다.

## 워크스페이스 대원칙 (canonical)

이 레포는 `../CLAUDE.md` 의 **DEVELOPMENT PRINCIPLES** 섹션을 따른다. 핵심 재진술:

1. **인증** — 현재 LEGACY_LOCAL_AUTH — 프로젝트 단위 password 평문 저장. `auth-api-nest/.idea/oauth-blueprint.md` Phase 3 에서 `auth-api-nest` 의 access token 검증으로 교체 예정.
2. **기능 단위 커밋** — 한 기능이 계획-구현-검토를 통과하면 즉시 1개의 커밋. 여러 기능을 묶지 않는다.
3. **계획 → 구현 → 검토** — 계획 단계에서 검토 통과 기준(어떤 테스트/명령이 통과해야 "done"인지)을 명시한다.
4. **Docker 빌드 가능** — DEPLOY. 루트 `docker-compose.yml` 의 `fastapi` 서비스로 등록됨 (포트 8000). Dockerfile 유지 필수.

## Feature Workflow (대원칙 #3 의 이 레포 적용)

1. `.idea/` 또는 신규 계획 문서에서 기능을 선택
2. 계획서 작성 — 변경 파일, 인터페이스, **검토 통과 기준** 명시
3. 구현
4. 통과 기준 만족 여부 직접 실행/테스트 (uvicorn 기동 후 엔드포인트 호출 확인, DB DDL 영향이 있으면 init_db 재실행)
5. 통과 시 1개의 커밋으로 마무리

## STRUCTURE

```
src/
├── __main__.py           # ALL routes + Pydantic models + startup event (single file, 394 lines)
├── __init__.py           # Empty
└── connectors/
    └── __init__.py       # DB config, get_db_connection() context manager, init_db() DDL
Dockerfile                # Python 3.11-slim, port 8000
requirements.txt          # fastapi 0.115.5, uvicorn, pymysql, pydantic 2, python-dotenv
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add API endpoint | `src/__main__.py` | Add route function + Pydantic request model in same file |
| Change DB schema | `src/connectors/__init__.py` `init_db()` | DDL in raw SQL, auto-runs on startup |
| DB connection | `src/connectors/__init__.py` `get_db_connection()` | Context manager with auto-commit/rollback |
| Environment vars | `.env` → `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | Loaded via `python-dotenv` |

## DATABASE SCHEMA

```sql
projects (id PK, name, icon, is_secret, password, created_at, updated_at)
memos (id PK, project_id FK→projects, title, content LONGTEXT, created_at, updated_at)
memo_versions (id PK, memo_id FK→memos, content LONGTEXT, version INT, created_at)
```

- `VARCHAR(50)` UUIDs as primary keys (generated via `uuid.uuid4()`)
- Cascading deletes: project → memos → memo_versions
- InnoDB, utf8mb4

## CONVENTIONS

- **Single file API** — all routes in `__main__.py` (no router splitting)
- **snake_case DB columns** → **camelCase JSON responses** (manual dict mapping in each route)
- **Pydantic models**: `Create{Entity}Request`, `Verify{Action}Request` for inputs; `Project`, `Memo` for response shapes
- **Context manager** for DB: `with get_db_connection() as conn:` (auto-commit on success, rollback on error)
- **DictCursor** — all `cursor.fetchone()`/`fetchall()` return dicts
- **Korean docstrings** on all route functions
- **All routes under `/api/`** prefix

## ANTI-PATTERNS

- **Passwords stored in plaintext** — `project["password"] == data.password` (no hashing). 대원칙 #1 에서 auth-api-nest 통합 시 제거 예정.
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
