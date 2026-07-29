# todo-api-fastapi

FastAPI backend for todo/project/memo/daily-task/article data. Raw SQL via PyMySQL against MySQL.

> 이 파일이 본 레포의 canonical 가이드입니다. `AGENTS.md` 는 codex 호환용 stub 입니다.

- **Lifecycle**: DEPLOY
- **Status**: active
- **Port**: 8000
- **Auth**: `auth-api-nest-oidc-session` (중앙 OIDC 로그인 — 세션 쿠키/opaque 세션은 이 API 가 소유) + 동기화 피어용 **auth 발급 service credential**(scope `sync`) 검증

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
├── timeutil.py           # 시간대 명시 유틸 (원칙 8) — UTC 판정값 / Asia/Seoul 표시값 분리
├── sync_schema.py        # 동기화 필드 화이트리스트 + SCHEMA_VERSION + NFC 정규화
├── sync_cli.py           # `python -m src.sync_cli` — doctor/link-identity/pause/resume/bootstrap
├── sync_daemon.py        # `python -m src.sync_daemon` — 데몬 단독 실행 진입점
├── utils.py
├── connectors/
│   └── __init__.py       # DB config, get_db_connection(sync_applying=), init_db() DDL+트리거
├── models/               # Pydantic 모델 (auth, base, daily_tasks)
├── routers/              # APIRouter 모듈 — auth, projects, memos, articles, daily_tasks, sync
└── services/             # session_auth(OIDC+오프라인 세션), realtime(Socket.IO), livekit,
                          # merge(중복 병합), lock_registry(락 단일 진실),
                          # sync_auth(credential 검증) · sync_store(change_log/state/issues) ·
                          # sync_apply(LWW·중복 정책) · sync_peer(원격 호출) ·
                          # sync_daemon(push/pull 루프) · sync_runtime(온라인 상태) · http_json
scripts/
├── export_topic_data.py  # Read-only legacy topic export for topic-api-fastapi migration
├── backfill_updated_at_utc.py # naive → UTC 백필 (init_db 도 같은 일을 멱등하게 한다)
└── migrate_legacy_todo.py # 레거시 todo DB(:3030 스택) → 이 스키마 마이그레이션 (additive upsert, 접속정보 CLI 파라미터)
                           # + mirror 모드로 부트스트랩 적재(--wipe-daily-tasks/--sync-applying)
tests/
├── scratch_db.py         # 스크래치 DB 가드 — 실사용 DB 를 지우려는 테스트를 거부한다
├── test_sync_schema.py   # 화이트리스트·정규화·시간 유틸 (DB 불필요)
├── test_sync_apply.py    # 충돌/중복/의존성/트리거 (실제 MySQL 스크래치 DB)
└── test_sync_daemon.py   # 스키마 handshake 3케이스 / 시계 편차 / 신원 불일치
Dockerfile                # Python 3.13-slim production image, non-root, port 8000
Dockerfile.dev            # Python 3.13 reload development image
.dockerignore             # secrets, venv, caches, VCS metadata excluded
requirements.txt          # fastapi 0.115.5, uvicorn, pymysql, pydantic 2, python-dotenv 등
```

로컬 API+Web 통합 실행은 workspace root `../.scripts/todo/compose.yml`, 운영
배포는 `../.scripts/deploy-todo-prod.sh`가 담당한다. Production app compose는
두지 않는다.

Topic collection/insight data and embedding similarity search moved to `../topic-api-fastapi`.
This repo keeps only a read-only legacy export script until migration verification is complete.

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Add API endpoint | `src/routers/{domain}.py` | 라우터 모듈에 route + Pydantic request model 추가, `__main__.py` 에 등록 |
| Change DB schema | `src/connectors/__init__.py` `init_db()` | DDL in raw SQL, auto-runs on startup |
| DB connection | `src/connectors/__init__.py` `get_db_connection()` | Context manager with auto-commit/rollback |
| Environment vars | `.env` / `.env.example` → `DB_*`, `AUTH_*`, `TODO_*`, `LIVEKIT_*`, `SYNC_*` | Loaded via `python-dotenv`; deploy 시 런타임 env 주입. `.env.example` 을 먼저 고친다 (원칙 7) |
| 동기화 정책 변경 | `src/sync_schema.py` + `src/services/sync_apply.py` | 컬럼 추가 시 화이트리스트와 `SCHEMA_VERSION` 을 **함께** 올린다 |
| 동기화 상태 진단 | `python -m src.sync_cli doctor` | 신원·스키마·커서·이슈·트리거를 한 번에 본다 |
| Export legacy topic data | `scripts/export_topic_data.py` | Read-only JSON export for topic service import |

## DATABASE SCHEMA

`src/connectors/__init__.py` `init_db()` 의 DDL 이 canonical 이다. 데이터 테이블 7개 +
동기화용 4개(`change_log`·`sync_state`·`sync_issues`·`local_identity`, 위 OFFLINE SYNC 참조):

```text
projects               (id PK, owner_id, name, icon, status, is_secret, password[legacy], created_at, updated_at, updated_at_utc, deleted_at)
memos                  (id PK, project_id FK→projects, created_by, title, content LONGTEXT, status, deleted_at, created_at, updated_at, updated_at_utc)
memo_versions          (id PK, memo_id FK→memos, content LONGTEXT, version INT, note, created_at, updated_at_utc)
articles               (id PK, memo_id FK→memos UNIQUE, project_id FK→projects, author_id, author_slug, title, content, published_version, ...)
project_members        (id PK, project_id FK→projects, user_id, username, display_name, email, role, invited_at, updated_at_utc, deleted_at; UNIQUE(project_id,user_id))
daily_task_types       (id PK, name UNIQUE, icon, color, display_order, is_active, ...)
daily_task_completions (id PK, task_type_id FK→daily_task_types, completed_date DATE, total_active_count; UNIQUE(task_type_id,completed_date))
```

- **스키마 드리프트 주의**: 운영/로컬 실DB 에는 `projects.owner_id`, `memos.created_by` 컬럼이 존재하지만
  `init_db()` DDL 에는 없다 (과거 ALTER 산물). 마이그레이션 스크립트는 이를 런타임에 감지해 대응한다.
- 레거시 todo(:3030) 데이터 이관: `scripts/migrate_legacy_todo.py` — additive/idempotent upsert
  (`legacy-*` 결정적 id), `--replace` 는 `--confirm-replace <db>` 필수, 대상 접속정보는 CLI 파라미터로
  원격(prod) DB 지정 가능. detail 히스토리는 `memo_versions` 로 보존된다.
- `VARCHAR(50)` UUIDs as primary keys (generated via `uuid.uuid4()`) — 두 노드에서 만든 행이
  PK 충돌하지 않으므로 동기화 병합의 전제가 이미 충족되어 있다
- Cascading deletes: project → memos → memo_versions / articles / project_members, daily_task_types → daily_task_completions
  ⚠️ **동기화 대상 테이블은 CASCADE 하드 삭제 경로를 쓰지 않는다** — InnoDB 는 CASCADE 삭제로
  트리거를 발동시키지 않아 tombstone 이 남지 않고, 상대 노드에서 행이 되살아난다. soft delete 만 쓴다
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

## OFFLINE SYNC (오프라인 양방향 동기화)

Root plan: `../.idea/TODO_OFFLINE_SYNC_PLAN.md` · repo 실행 계획: `.idea/TODO_OFFLINE_SYNC_TODO_API_FASTAPI_PLAN.md`

NAS 원격을 **단일 진실**로 두고, 노트북 로컬 스택을 outbox 를 가진 복제본으로 만든다.
같은 코드베이스가 양쪽에 배포되고 **역할은 env 로 갈린다**:

| `SYNC_ENABLED` | `SYNC_PEER_URL` | 역할 | 하는 일 |
|---|---|---|---|
| `false` | — | `disabled` | 아무것도 안 한다 (개발용 2번째 스택) |
| `true` | 설정됨 | `client` | 데몬(push/pull/소켓 구독) 실행. 피어 API 는 **서빙하지 않는다** |
| `true` | 비어 있음 | `server` | `/api/sync/*` 피어 API 서빙 + credential 검증 |

접속은 **언제나 노트북이 시작한다** (NAS→노트북 인바운드 없음).

### 스키마 (`src/connectors/__init__.py` `init_db()` 가 canonical)

동기화 대상 4개 테이블(`projects`·`memos`·`memo_versions`·`project_members`)에 추가된 것:

```text
updated_at_utc DATETIME(3) NOT NULL   -- 충돌 판정(LWW)의 유일한 근거. 항상 UTC
deleted_at     DATETIME(3) NULL       -- projects / project_members 에 신규 (memos 는 기존)
memo_versions.note VARCHAR(255) NULL  -- "충돌 · 로컬 (07-29 14:02)" / "병합 · {제목} (…)"
```

노드 로컬 테이블 (동기화 대상 아님):

```text
change_log     (seq BIGINT AI PK, table_name, row_id, op ENUM, changed_at_utc)   -- outbox
sync_state     (peer PK, last_pushed_seq, last_pulled_seq, last_ok_at, last_error, paused)
sync_issues    (id PK, kind, ref_table, ref_id, peer_ref_id, detail JSON, detected_at, resolved_at)
               -- kind: conflict | duplicate_project | duplicate_memo | identity | schema | clock
local_identity (account_id PK, login_id, display_name, email, permission, issuer, verified_at_utc)
```

**원칙 8 위반 해소**: 기존 쓰기는 naive `datetime.now()` 를 그대로 넣어 "누가 최신인가"를
판정할 수 없었다. 이제 `src/timeutil.py` 가 시간대를 명시한다 — `updated_at_utc` 는 UTC,
표시용 `updated_at`/`created_at`/`invited_at` 은 `Asia/Seoul` 벽시계. 기존 행은
`init_db()` 가 `CONVERT_TZ(..., '+09:00', '+00:00')` 로 멱등 백필한다
(명시 실행/리포트는 `scripts/backfill_updated_at_utc.py`).

### 트리거 (change_log 를 채운다)

테이블당 insert/update/delete 3개, 총 12개 (`trg_{table}_{ai|au|ad}_change_log`).
앱의 쓰기 지점 20여 곳을 각각 고치는 것보다 누락이 없고, 마이그레이션 스크립트나 손으로 실행한
SQL 까지 잡힌다. 본문은 `IF @sync_applying IS NULL THEN … END IF` 로 감싼다:

- `get_db_connection(sync_applying=True)` → 이 커넥션의 쓰기는 로그에서 제외 (**핑퐁 방지**)
- `change_log_enabled(cursor)` → 그 안에서 **의도적으로** 로그에 남긴다 (충돌 보존 버전)
- `CREATE TRIGGER IF NOT EXISTS` 는 MySQL 8.0 에 없으므로 information_schema 로 존재 여부와
  **본문 동일성**까지 확인해 필요할 때만 재생성한다 (로컬 MariaDB 11.8 / 운영 MySQL 8.0 양쪽 호환)

⚠️ InnoDB 의 **FK CASCADE 삭제는 트리거를 발동시키지 않는다**. 그래서 삭제는 전부 soft delete 로
바꿨다 (`DELETE /api/projects/{id}` 는 메모·멤버 tombstone 까지 함께 남긴다).

### 엔드포인트

**피어 대상** (서버 역할만, service credential 인증):

```text
GET  /api/sync/handshake                      → schemaVersion·accountId·serverTimeUtc·ownerIds·maxSeq·tables·identity
GET  /api/sync/changes?since=<seq>&limit=500  → 계정 스코프로 필터된 변경 + nextSeq
POST /api/sync/push                           → applied/skipped/deferred/conflicts/duplicates
POST /api/sync/locks/{memoId}/acquire|release · GET /api/sync/locks/{memoId}
POST /api/sync/merge/memos|projects/{loserId}/merge-into/{winnerId}
```

**UI 대상** (모든 역할, 브라우저 세션 인증 — `todo-web-next` 가 쓴다):

```text
GET  /api/sync/status · GET /api/sync/issues · POST /api/sync/issues/resolve
POST /api/sync/pause  · POST /api/sync/trigger
```

일반 CRUD 를 재사용하지 **않는다**: 타임스탬프가 다시 써지고, 메모 제목 중복 409 가드가 걸리고,
대량 처리가 안 된다. sync 경로는 그 가드를 우회하고 중복을 `sync_issues` 로 기록한다 —
막으면 오프라인 생성분이 409 로 동기화를 **영구 정지**시킨다.

### 충돌·중복 정책

- `updated_at_utc` **늦은 쪽 승**, **동시각이면 서버(원격) 값 승**
- 진 쪽 메모 본문은 `memo_versions` 에 `note` 와 함께 보존 → 유실 없음. 이 삽입만 `change_log` 에
  남겨 상대 노드도 받는다
- 삭제 vs 편집: soft delete 라 같은 LWW 로 결정 (더 최신인 쪽이 이긴다)
- 적용 순서 `projects → memos → memo_versions → project_members`, FK 부모가 없는 행은 **그 행만** 다음 라운드로
- 중복 판정: `trim` + 유니코드 **NFC** 후 완전일치, **대소문자 구분 유지**
- 병합은 **원격에서 실행하고 로컬은 pull 로 받는다**. 오프라인에서는 409 로 잠근다

### 인증 (왜 service credential 인가)

노트북 전용 credential(같은 serviceKey `todo`, 다른 `keyId`, scope `sync`)을 auth 관리화면에서
발급받아 로컬 `.env` 에 넣는다. prod todo 는 **자기 credential 로 자신을 인증해** auth 의 검증
엔드포인트에 "이 keyId/secret 이 유효·활성이고 `sync` scope 인가"를 묻고 결과를 5분 캐시한다.

```text
POST {AUTH_VERIFY_URL}   headers: x-auth-service-key-id / x-auth-service-secret (= 이 서버 자신)
body {"keyId","secret","requiredScope":"sync"}
판정은 항상 HTTP 200 → {"valid":true,...} | {"valid":false,"reason":"invalid_credential|disabled|scope_missing"}
401 은 호출자(=이 서버) 자신의 인증 실패만 의미한다
```

**prod todo 는 노트북의 secret 을 저장하지 않는다.** 그래서 폐기는 auth 관리화면에서 `disabled` 로
바꾸면 캐시 만료 후 차단되고 **NAS 를 손댈 필요가 없다**. 공유 시크릿(auth 밖에 새 신뢰 축을 만들고
원칙 3·5·6 을 우회)과 `client_credentials` grant(이미 있는 service credential 과 중복이고 미래
사용자 앱은 PKCE+refresh 를 쓴다) 둘 다 채택하지 않았다.

인증은 `src/services/sync_auth.py` 의 **교체 가능한 단계**이고 결과는 항상
`SyncPrincipal(account_id, permission)` 으로 정규화된다. 하위 로직은 `account_id` 만 본다 —
후에 사용자 앱이 access token 으로 같은 엔드포인트를 쓸 때 `register_authenticator()` 로
검증기 하나만 추가하면 된다.

계정 고정: credential 은 기계 신원이라 사용자를 증명하지 않는다. `SYNC_ACCOUNT_ID` 가 있으면 그 값,
없으면 데이터의 distinct owner id 가 정확히 1개일 때 자동 해석한다 (0개/2개 이상이면 503).

### 오프라인 세션

최초 1회 원격 auth 로그인 시 `local_identity` 에 신원을 캐시한다. 이후 auth 가 닿지 않으면
`POST /api/session/local` 이 **무기한** 로컬 세션을 발급하고(`offline: true`), refresh 실패도
502 이상이면 오프라인 세션으로 이어붙인다. `TODO_LOCAL_SESSION_ENABLED=true` + **loopback 요청만**
허용한다. 만료로 인한 보호를 포기하는 선택이므로 로컬 API 는 반드시 `127.0.0.1` 바인딩을 유지해야 한다.

### CLI

```bash
python -m src.sync_cli doctor [--json]                 # 신원·스키마·커서·이슈·트리거 리포트
python -m src.sync_cli link-identity --dry-run          # 로컬 owner id → 원격 계정 id 재작성 계획
python -m src.sync_cli link-identity [--yes] [--map old=new]
python -m src.sync_cli pause | resume | init-db
python -m src.sync_cli bootstrap --dry-run --target-host … --target-database …
python -m src.sync_daemon --once                        # 데몬 1회 실행 (단독, 로컬 재발행 없음)
```

`link-identity` 는 distinct owner id 가 **1개면 자동 해석**하고, 로컬 신원(`project_members` 실측)과
원격 신원(handshake)을 **둘 다 보여주고 확인**을 받는다. **email 일치를 게이트로 쓰지 않는다** —
로컬은 `lafamila325@gmail.com` 이고 원격 계정 email 은 다를 수 있다. 자동 거부는 id 가 2개 이상이거나
NULL 이 섞였을 때만 한다.

### 데몬

`SYNC_DAEMON_AUTOSTART=true`(기본)면 **API 프로세스 안에서** 돈다 — pull 적용 후 열린 탭을 갱신하려면
로컬 Socket.IO 서버에 재발행해야 하고 그 서버가 이 프로세스 메모리에 있기 때문이다.
`python -m src.sync_daemon` 단독 실행도 되지만 그때는 재발행이 빠진다.

로컬 쓰기 감지(디바운스 tick) · 원격 `syncChanged` 소켓 알림 → 즉시 pull · 폴링 안전망 ·
오프라인 백오프. 폴링을 남기는 이유: `change_log` 는 트리거가 채우므로 API 밖 변경(수동 SQL)까지
잡히지만 소켓 알림은 API 레이어에서 나가므로 그런 변경에는 뜨지 않는다.

### 2스택 로컬 실행

```bash
# 실사용(동기화 대상) — DB teddynote
DB_NAME=teddynote SYNC_ENABLED=true SYNC_PEER_URL=https://todo.lafamila.xyz \
  SYNC_KEY_ID=… SYNC_SECRET=… TODO_LOCAL_SESSION_ENABLED=true \
  venv/bin/python -m uvicorn src.__main__:app --host 127.0.0.1 --port 20022

# 개발(동기화 제외) — DB teddynote_dev. SYNC_PEER_URL 을 비우고 SYNC_ENABLED=false
DB_NAME=teddynote_dev SYNC_ENABLED=false \
  venv/bin/python -m uvicorn src.__main__:app --host 127.0.0.1 --port 20023
```

### 테스트

```bash
venv/bin/python -m unittest tests.test_sync_schema tests.test_sync_apply \
    tests.test_sync_daemon tests.test_session_auth tests.test_migrate_legacy_todo
```

`tests/test_sync_apply.py` / `tests/test_sync_daemon.py` 는 **실제 MySQL 스크래치 DB**
(`TODO_SYNC_TEST_DB`, 기본 `teddynote_sync_t`)를 매 테스트마다 비우고 쓴다.
`tests/scratch_db.py` 가 대상 DB 이름을 검증해 실사용/개발 DB(`teddynote`, `teddynote_dev`, `todo`)나
`.env` 의 `DB_NAME` 이면 **실행을 거부**한다. `DB_CONFIG` 는 `src.connectors` import 시점에 고정되므로
import 순서에 의존하지 말고 반드시 `use_scratch_database()` 를 쓴다.

## ANTI-PATTERNS

- **Project password is legacy** — 중앙 auth 전환 후 제거 대상.
- **동기화 대상 테이블에 `datetime.now()` 를 쓰지 말 것** — `src/timeutil.py` 의
  `utcnow_naive()`(UTC 판정용) / `localnow_naive()`(표시용)를 쓴다. 모든 쓰기는
  `updated_at_utc` 를 함께 기록해야 한다 (NOT NULL, 기본값 없음 — 빠지면 INSERT 가 실패한다).
- **동기화 대상 테이블에서 하드 삭제 금지** — tombstone 이 없으면 상대 노드에서 되살아난다.
- **테스트에서 `os.environ["DB_NAME"]` 만 바꾸지 말 것** — `src.connectors` 가 이미 import 되어
  있으면 `DB_CONFIG` 는 `.env` 값으로 고정되어 있고, DELETE 가 실사용 DB 로 날아간다.
  `tests/scratch_db.py` 의 `use_scratch_database()` 를 쓴다.
- ~~`@app.on_event("startup")`~~ — **해결됨**: `lifespan` context manager 로 전환 완료. 새 코드에서 `on_event` 재도입 금지.
- **No input sanitization** on f-string in `init_db()`: `f"CREATE DATABASE IF NOT EXISTS {DB_CONFIG['database']}"` (SQL injection risk if DB_NAME is user-controlled)
- **Memo versioning** saves old content before update but **no version diffing** — stores full content copies
- **No pagination** on any list endpoint

## API ENDPOINTS

`src/routers/` 의 라우트 정의가 canonical 이다 (전부 `/api/*`). 카테고리 요약:

| Category | Router | Routes |
|----------|--------|--------|
| Auth/session | `routers/auth.py` | `POST /api/session/oidc/start`, `POST /api/session/logout`, `GET /api/session/me`, `POST /api/session/local`(오프라인 로그인), `GET /api/session/local/identity`, `POST /api/session/service-application`, `GET /api/users/search`, `POST /api/livekit/token` |
| Projects | `routers/projects.py` | `GET/POST /api/projects`, `DELETE /api/projects/{id}`(soft), `POST /api/projects/{id}/verify`, 병합 `POST /api/projects/{loserId}/merge-into/{winnerId}`, 멤버 관리 `GET/POST /api/projects/{id}/members`, `DELETE /api/projects/{id}/members/{userId}`(soft) |
| Memos | `routers/memos.py` | `GET /api/projects/{id}/memos`, `POST /api/memos`, `GET/PUT/DELETE /api/memos/{id}`, `POST /api/memos/bulk-delete`, 버전 `GET /api/memos/{id}/versions[/{v}]`, 병합 `POST /api/memos/{loserId}/merge-into/{winnerId}` |
| Sync | `routers/sync.py` | 피어: `handshake`·`changes`·`push`·`locks/*`·`merge/*` (service credential) / UI: `status`·`issues`·`issues/resolve`·`pause`·`trigger` (세션) |
| Articles | `routers/articles.py` | `POST/GET /api/articles`, `GET /api/articles/boards/{slug}`, `GET/DELETE /api/articles/{id}`, `GET /api/memos/{id}/article` |
| Daily tasks | `routers/daily_tasks.py` | `/api/daily-tasks` prefix — `POST/GET/PUT/DELETE .../types[/{id}]`, `POST .../complete`, `DELETE .../complete/{typeId}/{date}`, `GET .../calendar[/{date}]` |

Socket.IO realtime 서버(`services/realtime.py`)가 FastAPI app 을 ASGI wrap 한다. 전용 health 엔드포인트는 없다 (Docker HEALTHCHECK 는 `GET /docs` 사용).
