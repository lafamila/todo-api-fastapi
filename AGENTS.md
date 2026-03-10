# todo-api-fastapi

FastAPI backend owning all data. Raw SQL via PyMySQL against MySQL. Single-file API.

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

- **Passwords stored in plaintext** — `project["password"] == data.password` (no hashing)
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