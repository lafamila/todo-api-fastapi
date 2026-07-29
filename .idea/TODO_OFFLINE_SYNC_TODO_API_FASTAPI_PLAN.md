---
status: PREPARED
summary: "오프라인 로컬 스택과 NAS 원격을 양방향 동기화하는 스키마·트리거·sync 엔드포인트·데몬·오프라인 세션·병합 API·CLI 를 구현한다."
---

# TODO OFFLINE SYNC — todo-api-fastapi execution plan

Canonical orchestration plan:

`../.idea/TODO_OFFLINE_SYNC_PLAN.md`

전체 그림(왜 이 구조인지, 충돌 타임라인, 부트스트랩 순서)은 root plan 을 본다. 이 문서는 이 레포가 실제로 무엇을 만드는지에 집중한다.

## Repo Responsibility

동기화의 거의 전부. 같은 코드베이스가 양쪽에 배포되고 **역할은 env 로 갈린다**:

| 역할 | 내용 | 켜지는 곳 |
|---|---|---|
| 서버 | `/api/sync/*` 수신, auth 에 credential 검증 질의 | prod (NAS) |
| 클라이언트 | 동기화 데몬 — push/pull/소켓 구독 | 노트북 (`SYNC_PEER_URL` 설정된 쪽) |

노트북은 sync 엔드포인트를 **서빙하지 않는다** — push 도 pull 도 노트북이 prod 를 호출하는 방향이다. 개발용 2번째 스택은 `SYNC_PEER_URL` 을 비워 자동으로 동기화에서 빠진다.

## Inputs / Dependencies

- **`auth-api-nest` 의 검증 엔드포인트 계약** — 경로/요청/응답/실패 이유. 그 레포 실행 계획이 확정본을 보고한다. 이 계약 없이 인증 부분을 구현하지 않는다.
- 사용자가 발급하는 **노트북용 credential** (`keyId`/`secret`) — 로컬 `.env` 에 보관. 통합 검증 시점까지만 있으면 된다.
- 확인된 현재 상태 (root plan 의 "현재 상태" 표 참조): PK 는 `uuid4`(`src/utils.py:12`), 타임스탬프는 naive `datetime.now()`, tombstone 은 `memos.deleted_at` 뿐, 메모 제목 409 가드(`routers/memos.py:88`), 버전 API 존재(`routers/memos.py:224,263`), Socket.IO 존재(`services/realtime.py`), 로컬 owner id 단일(`e1ecab2f-…`, 448행).

## Work Items

### 1. 스키마와 변경 기록 (`src/connectors/__init__.py`)

1. 동기화 대상 테이블(`projects`, `memos`, `memo_versions`, `project_members`)에 `updated_at_utc DATETIME(3) NOT NULL` 추가. 값은 **명시적 UTC** 로 기록한다 (naive `datetime.now()` 는 판정 근거로 쓰지 않는다).
2. tombstone 추가: `projects.deleted_at`, `project_members.deleted_at` (`memos.deleted_at` 은 이미 있다). `memo_versions` 는 append-only 라 불필요.
3. `memo_versions.note VARCHAR(255) NULL` 추가 — `충돌 · 로컬 (07-29 14:02)` 같은 표시용.
4. 신규 테이블:
   - `change_log (seq BIGINT AUTO_INCREMENT PK, table_name, row_id, op ENUM('insert','update','delete'), changed_at_utc DATETIME(3), INDEX(seq))`
   - `sync_state (peer VARCHAR(64) PK, last_pushed_seq BIGINT, last_pulled_seq BIGINT, last_ok_at DATETIME(3), last_error TEXT, paused TINYINT)`
   - `sync_issues (id VARCHAR(50) PK, kind, ref_table, ref_id, peer_ref_id, detail JSON, detected_at DATETIME(3), resolved_at DATETIME(3) NULL, INDEX(kind, resolved_at), INDEX(ref_table, ref_id))` — `kind`: `conflict | duplicate_project | duplicate_memo | identity | schema`
5. **트리거**로 `change_log` 를 채운다 (테이블당 insert/update/delete 3개). 앱의 쓰기 지점을 모두 고치는 것보다 누락이 없고, 마이그레이션 스크립트나 수동 SQL 까지 잡힌다.
   - 트리거 본문은 `IF @sync_applying IS NULL THEN … END IF` 로 감싼다. 동기화 적용 커넥션은 `SET @sync_applying = 1` 을 실행해 자기 쓰기를 로그에서 제외한다.
   - DDL 은 `init_db()` 에 포함해 양쪽 재기동만으로 반영되게 한다. 트리거는 `CREATE TRIGGER IF NOT EXISTS` 가 MySQL 버전에 따라 없을 수 있으므로 존재 확인 후 생성하는 멱등 경로를 쓴다.
6. **백필**: 기존 naive `updated_at` 을 `Asia/Seoul` 로 해석해 `updated_at_utc` 로 변환하는 일회성 스크립트. 부트스트랩 적재 전에 로컬에서 끝낸다.

### 2. 쓰기 경로 정리

7. 하드 삭제 → **소프트 삭제** 전환: `DELETE /api/memos/{id}`, `POST /api/memos/bulk-delete`, 프로젝트 삭제 경로. 조회 쿼리에 `deleted_at IS NULL` 조건이 빠진 곳이 없는지 전수 확인한다.
8. 모든 쓰기에서 `updated_at_utc` 를 함께 기록한다. 기존 `updated_at` 컬럼은 표시용으로 유지한다(제거하면 프론트 계약이 깨진다).

### 3. sync 서버 (`src/routers/sync.py`, `src/services/sync_*.py`)

9. 엔드포인트:
   ```
   GET  /api/sync/handshake                      → {schemaVersion, accountId, permission, serverTimeUtc, ownerIds[], maxSeq}
   GET  /api/sync/changes?since=<seq>&limit=500  → {changes[], nextSeq, serverTimeUtc}
   POST /api/sync/push {clientId, changes[]}     → {applied, conflicts[], duplicates[], nextSeq}
   GET  /api/sync/status                         → {lastOkAt, pending, paused, issues}
   GET  /api/sync/issues                         → sync_issues 목록 (미해결 우선)
   ```
10. **일반 CRUD 엔드포인트를 재사용하지 않는다.** 타임스탬프가 다시 써지고, 제목 중복 409 가드가 걸리고, 대량 처리가 안 된다. sync 는 제목 중복 가드를 **우회**하고 중복을 `sync_issues` 로 기록한다 (막으면 오프라인 생성분이 409 로 동기화를 영구 정지시킨다).
11. **필드 화이트리스트**: `src/sync_schema.py` 에 `SYNC_TABLES = {'memos': [...컬럼...], ...}` 와 `SCHEMA_VERSION` 을 둔다. 모르는 컬럼은 절대 주고받지 않는다. 컬럼을 동기화 대상에 추가하면 화이트리스트와 `SCHEMA_VERSION` 을 함께 올린다.
12. **스키마 handshake**: 같으면 정상, 로컬이 앞서면 기본 중단(`--allow-schema-drift` 로 공통 필드만), 원격이 앞서면 "코드 pull + `init_db`" 안내 후 중단.
13. **인증 어댑터** (`auth-api-nest` 계약 입력):
    - 요청의 `x-auth-service-key-id`/`x-auth-service-secret` 를 받아 auth 검증 엔드포인트에 질의(자기 credential 로 자신을 인증).
    - 결과를 **5분 캐시**한다 (키: keyId+secret 해시). 매 요청마다 auth 를 때리지 않는다.
    - 인증은 **교체 가능한 단계**로 구현하고 결과를 `(accountId, permission)` 으로 정규화한다. 하위 로직은 `accountId` 만 본다 — 후에 사용자 앱이 PKCE+refresh access token 으로 같은 엔드포인트를 쓸 때 검증기만 추가하면 되게.
    - credential 은 기계 신원이라 사용자를 증명하지 않는다. 계정은 handshake 가 해석한 서비스 소유자 계정으로 고정한다.
14. **변경 피드 계정 스코프**: `changes` 는 노드 전체가 아니라 그 계정이 볼 수 있는 행(프로젝트 멤버십 경유)만 반환한다. 단일 사용자에서는 결과가 같지만 다중 사용자에서는 필수다.
15. **적용 규칙**:
    - 의존성 순서: `projects → memos → memo_versions → project_members`. FK 위반 행은 그 행만 다음 라운드로 미룬다 (오프라인에서 "프로젝트 생성 → 그 안에 메모 생성"이 흔하다).
    - 멱등: 들어온 `updated_at_utc` ≤ 기존값이면 건너뛰고 충돌로 기록.
    - 충돌: `updated_at_utc` 늦은 쪽 승, **동시각이면 원격 승**. 메모 내용이면 **패자 내용을 `memo_versions` 에 삽입**하고 `note` 를 채운다. `sync_issues(kind='conflict')` 기록.
    - 삭제 vs 편집: `deleted_at` 이 더 최신이면 삭제 승, 아니면 편집이 부활시킨다.
    - 중복 감지: 프로젝트 이름 / (project_id, title) 을 `trim` + 유니코드 **NFC** 후 완전일치로 비교(대소문자 구분 유지) → `sync_issues(kind='duplicate_*')`.
    - **시계 편차 가드**: `serverTimeUtc` 로 편차 측정, 임계값(5초) 초과면 동기화 중단 + 알림. 시계가 틀어진 LWW 는 조용히 최신 내용을 버린다.

### 4. sync 클라이언트 데몬

16. `src/services/sync_client.py` + `python -m src.sync_daemon` 진입점. 동작:
    - 로컬 저장 후 **1초 디바운스 push**
    - 원격 Socket.IO 알림 수신 시 **즉시 pull**
    - **폴링 60초** 안전망 (트리거는 API 밖 변경까지 잡지만 소켓 알림은 API 레이어에서 나가므로 그 구멍을 메운다)
    - 오프라인 **백오프 30초**, 네트워크 복구 감지 시 즉시 동기화
    - 커서는 `sync_state` 의 `last_pushed_seq`/`last_pulled_seq`
    - `paused` 면 아무것도 하지 않는다
17. pull 적용 후 **로컬 Socket.IO 로 재발행**해 열려 있는 탭이 새로고침 없이 갱신되게 한다.
18. **편집 중 버퍼 보호**: 프론트가 판단할 수 있도록, 재발행 이벤트에 "이 변경이 원격 pull 에서 왔다"는 표시와 대상 메모 id·새 `updated_at_utc` 를 담는다. (덮어쓰기 여부 판단은 `todo-web-next` 책임.)

### 5. 오프라인 세션 (`src/services/session_auth.py`)

19. `local_identity` 저장소(테이블 또는 단일 행): 원격 auth 로그인 성공 시 계정 id·email·표시명·서비스 권한·issuer·검증시각을 캐시한다.
20. auth 가 닿지 않으면 **같은 계정 id 로 무기한 로컬 세션**을 발급한다 (`offline: true` 표시). 로컬 API 는 반드시 `127.0.0.1` 바인딩을 유지한다.
21. **preflight handshake**: 동기화 전에 원격 계정 id·권한과 로컬 데이터의 owner id **전체 distinct 목록**을 대조한다. 하나라도 다르면 **부분 적용 없이 중단**하고 `sync_issues(kind='identity')` 기록.

### 6. 병합 API

22. `POST /api/projects/{loserId}/merge-into/{winnerId}` — 패자 메모의 `project_id` 재작성 → `project_members` 합치기(user_id 중복 제거) → 패자 tombstone.
23. `POST /api/memos/{loserId}/merge-into/{winnerId}` — 생존자 유지(오래된 `created_at` 권장) → 패자 내용을 생존자 버전으로 편입 → 패자 `memo_versions` 이관 시 `version` **재번호**(`(memo_id, version)` 은 비유니크 인덱스지만 `GET /versions/{version}` 이 모호해지므로) → 패자 tombstone.
24. 병합은 **원격에서 실행하고 로컬은 pull 로 받는다**. 오프라인에서는 병합 조작을 **잠근다**(409 + 안내). 양쪽에서 각자 병합하면 결과가 달라져 재충돌한다.

### 7. 온라인 락 위임 · 실시간

25. 락/언락/보유자조회 3개를 온라인일 때 원격에 위임한다. 오프라인이거나 `SYNC_PEER_URL` 이 없으면(개발 스택) 로컬 락을 쓴다. 이걸로 **온라인 동시 편집 충돌이 구조적으로 사라진다**.
26. `services/realtime.py` 에 전역 룸 `sync:<accountId>` 추가. 동기화 대상 테이블에 변경이 생기면 `{maxSeq}` 를 그 룸에 알린다. 원격이 이 룸을 서빙하고 로컬 데몬이 클라이언트로 구독한다.

### 8. CLI

27. `todo-sync` (예: `python -m src.sync_cli`):
    - `doctor` — 신원·스키마·커서·대기 건수·이슈 리포트
    - `link-identity [--dry-name] [--map old=new]` — 로컬 distinct owner id 가 **1개면 자동 해석**해 원격 계정 id 로 재작성(단일 트랜잭션, 448행). 로컬 신원(`project_members` 의 username/display_name/email)과 원격 handshake 신원을 **둘 다 보여주고 확인**을 받는다. **email 일치를 게이트로 쓰지 않는다**(로컬은 `lafamila325@gmail.com`, 원격 계정 email 은 다를 수 있다). 재작성 시 `project_members` 의 username/display_name/email 도 원격 값으로 갱신한다. distinct id 가 2개 이상이거나 NULL 이 섞이면 자동 해석을 포기하고 `--map` 을 요구한다.
    - `pause` / `resume`
    - `bootstrap` — 아래 9번
28. `SYNC_ENABLED` 플래그로 전체를 끌 수 있게 한다.

### 9. 부트스트랩 (원격 전량 덮어쓰기)

29. `scripts/migrate_legacy_todo.py` 의 `--mode mirror --replace --confirm-replace <db>` 경로를 재사용·확장한다 (신규 컬럼 `updated_at_utc`·tombstone 반영). 원격은 폐기 대상이므로 양방향 정합이 아니라 **일방 적재**다.
30. wipe 범위: `projects`·`memos`·`memo_versions`·`project_members` (+ `articles` 는 `ON DELETE CASCADE` 로 자동) **+ `daily_task_types`·`daily_task_completions` 명시적 삭제**(원격에서도 쓰지 않기로 확정).
31. 순서: ① 원격 auth 1회 로그인(신원 캐시) → ② `link-identity` → ③ 적재. 순서가 바뀌면 원격에 없는 계정 id 로 소유된 데이터가 굳는다.
32. 적재 직후 **커서 기준선**: 로컬 `last_pushed_seq` = 로컬 `max(change_log.seq)`, `last_pulled_seq` = handshake 의 원격 `max(seq)`. 적재 커넥션은 `@sync_applying = 1` 로 둔다.
33. 실행 전 양쪽 덤프를 `.backups/db/` 에 남긴다.

### 10. 2스택 실행 · env · 문서

34. 로컬 **실사용**(`teddynote`, api `:20022`)과 **개발**(`teddynote_dev`, api `:20023`) 스택이 동시에 뜰 수 있게 `.env`/`.env.example` 을 정리한다. compose 파일은 워크스페이스 루트(`.scripts/todo/compose.yml`) 소유이므로 **필요한 변경을 orchestrator 에 보고**한다.
35. `.env.example` 에 신규 키를 먼저 반영한다 (원칙 7): `SYNC_ENABLED`, `SYNC_PEER_URL`, `SYNC_CLIENT_ID`, `SYNC_KEY_ID`, `SYNC_SECRET`, `SYNC_POLL_SECONDS`, `SYNC_PUSH_DEBOUNCE_MS`, `SYNC_CLOCK_SKEW_LIMIT_SECONDS`, `SYNC_VERIFY_CACHE_SECONDS`, `AUTH_VERIFY_URL`(계약에 따라).
36. repo `CLAUDE.md` 갱신: 새 스키마·트리거·엔드포인트·CLI·2스택 실행법, 그리고 **원칙 8 위반 해소**(naive → UTC) 기록. sync 인증이 auth 발급 service credential 이라는 점과 그 이유도 적는다.

## Acceptance Criteria

- `init_db()` 재실행이 멱등하고, 트리거가 재생성 없이 통과한다.
- `uvicorn src.__main__:app --port 8000` 기동 후 다음 스모크가 통과:
  - `GET /api/sync/handshake` 가 `schemaVersion`·`accountId`·`maxSeq` 를 반환
  - 로컬에서 메모 1건 저장 → `change_log` 에 1행 → push → 원격 반영 → 원격에서 수정 → pull → 로컬 반영
  - 동기화 적용 커넥션의 쓰기가 `change_log` 에 **남지 않음**(핑퐁 없음)
- 시나리오 검증 4종:
  1. **충돌**: 양쪽에서 같은 메모 수정 → 늦은 쪽이 현재값, 패자가 `memo_versions` 에 `note` 와 함께 남고 `sync_issues(conflict)` 1건
  2. **오프라인 생성**: 오프라인에서 프로젝트+그 안에 메모 생성 → 온라인 전환 후 의존성 순서로 둘 다 반영
  3. **중복**: 양쪽에서 같은 이름 프로젝트/같은 제목 메모 생성 → 차단되지 않고 `sync_issues(duplicate_*)` 기록 → merge-into 로 정리되며 내용이 버전으로 합쳐짐
  4. **신원 불일치**: owner id 를 일부러 어긋나게 만든 뒤 동기화 시도 → 부분 적용 없이 중단 + `sync_issues(identity)`
- 스키마 handshake 3케이스(같음 / 로컬 앞섬 / 원격 앞섬)가 의도대로 동작.
- `todo-sync doctor` 와 `link-identity --dry-run` 이 실측값(로컬 owner id 1개, 448행)을 정확히 보고.
- 부트스트랩 `--dry-run` 이 wipe 대상 건수와 적재 건수를 리포트.
- 소프트 삭제 전환 후 삭제된 메모/프로젝트가 목록·검색·버전 조회에 나타나지 않음.
- 온라인일 때 락이 원격에 위임되어, 로컬과 원격 웹에서 같은 메모를 동시에 열면 한쪽이 잠김.
- Docker 빌드는 구현 완료 후 사용자가 명시적으로 요청할 때만 수행한다 (원칙 17). 실행 테스트는 로컬 native 명령으로 한다.

## Report Back To Orchestrator

- `.scripts/todo/compose.yml` 에 필요한 변경 (개발 스택 추가, `DB_NAME`/포트/`SYNC_*` env) — 루트 파일은 orchestrator 소유.
- 루트 `CLAUDE.md` 포트 대장·COMMANDS 에 추가할 내용(`30333`/`20023`, 2스택 실행 명령).
- `todo-web-next` 가 구현에 필요한 **API 계약**: `sync_issues` 응답 스키마, 상태 응답, 버전 조회/최종본 저장, merge-into, 실시간 이벤트 payload(원격 pull 출처 표시 포함).
- `auth-api-nest` 검증 엔드포인트 사용 중 발견한 계약 문제.
- 새로 추가된 `.env` 키 목록 (원칙 7 — 사용자에게 보고 필요).
- 남은 위험: 트리거 `@sync_applying` 의존, 블록 단위 LWW 로 인한 부분 편집 유실 가능성, 무기한 로컬 세션의 보안 트레이드오프.

## Decision Escalation

사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않는다. 작업을 중단하고 현재 orchestrator 에게 전달해 결정받은 뒤 진행한다. orchestrator 에 보고할 수 없으면 workspace root `../.idea/` 에 handoff 문서를 남긴다.

특히 다음은 임의로 정하지 않는다:

- 부트스트랩 실행(원격 데이터 삭제) — 사용자 확인 아래 orchestrator 가 실행한다
- 동기화 대상 테이블 추가/제외 변경
- 충돌 정책 변경(LWW → 자동 3-way 등)
- 로컬 세션 만료 정책 변경
- 기존 API 응답 계약을 깨는 변경
