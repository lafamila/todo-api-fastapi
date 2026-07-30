---
status: IN_PROGRESS
summary: "TODO_MODE(dev/local/prod) 프리셋을 config 에 도입 — env 다이어트, 명시값>프리셋 우선순위, 기동 시 preflight 검증."
---

# TODO MODE SIMPLIFICATION — todo-api-fastapi execution plan

Canonical orchestration plan:

`../.idea/TODO_MODE_SIMPLIFICATION_PLAN.md`

전체 그림(3모드 정의·todoctl·compose 개편)은 root plan 을 본다. 이 문서는 config 계층 작업만 다룬다.

## Repo Responsibility

`TODO_MODE` 하나로 모드별 프리셋을 파생하는 config 재구성. **동작 변경 없음** — 지금 각 모드가 실제로 쓰는 값들이 프리셋의 정답지다 (compose 오버라이드·NAS `.env` 의 현재 값을 그대로 코드로 옮긴다).

## Inputs / Dependencies

- 프리셋 정답지 = 현재 실배포 값들:
  - dev: `.scripts/todo/compose.yml` 의 `todo-api-dev` environment 블록
  - local: 같은 파일 `todo-api` 블록 + `.env.sync-client`
  - prod: NAS `.env` (root plan 의 env 계약 표에 정리됨 — 내부 auth `http://auth-api-nest:3032`, DB `teddy-mysql:3306` 포함)
- 이 단계는 **루트 compose/todoctl 변경보다 먼저** 완료되어야 하고, 완료 후에도 기존 env 방식으로 완전 동작해야 한다 (하위호환이 루트 단계의 전제).

## Work Items

1. **`src/config.py` 재구성**
   - `TODO_MODE = os.getenv("TODO_MODE", "").strip().lower()` — 값: `dev|local|prod|""(레거시)`.
   - 모드별 프리셋 dict 정의: AUTH_ISSUER_URL/AUTH_PUBLIC_BASE_URL/AUTH_API_BASE_URL/AUTH_JWKS_URL, TODO_ALLOWED_ORIGINS/TODO_OIDC_REDIRECT_URI/TODO_WEB_BASE_URL/쿠키 설정, DB_HOST/DB_PORT/DB_NAME, SYNC_ENABLED/SYNC_PEER_URL/SYNC_CLIENT_ID/폴링·디바운스·백오프 등 SYNC 수치 전부, TODO_LOCAL_SESSION_ENABLED, TODO_SESSION_DB_PERSISTENCE, LIVEKIT_URL.
   - **우선순위: 명시 env > 모드 프리셋 > 기존 하드코딩 기본값.** `TODO_MODE` 미설정(레거시)이면 현재와 100% 동일하게 동작해야 한다.
   - 헬퍼 예: `def _mode_default(name, presets): env = os.getenv(name); return env if env not in (None, "") else presets.get(TODO_MODE, 기존기본)` — 단, 빈 문자열을 의미 있게 쓰는 키(`SYNC_PEER_URL` 서버 역할)는 명시 규칙을 문서화하고 프리셋으로 흡수한다 (모드가 역할을 정하므로 빈값 트릭이 필요 없어진다).
2. **preflight 검증** (`src/config.py` 또는 `src/preflight.py`, 앱 기동 시 호출)
   - 모드별 필수 비밀: dev→(DB_PASSWORD, dev용 OIDC secret) / local→(+ SYNC_KEY_ID/SECRET, SYNC_ACCOUNT_ID, AUTH_SERVICE_*) / prod→(+ SYNC_ALLOWED_KEY_IDS, SYNC_ACCOUNT_ID). 누락 시 키 이름을 나열하며 즉시 실패.
   - 형식 검증: URL 키는 `urlsplit` 으로 scheme/host 확인, 모든 값에 대해 **내부 공백+`#` 혼입 감지** (docker env-file 인라인 주석 사고 — 2026-07-29 prod 장애 재발 방지), `TODO_MODE` 오타 거부.
   - 실패 메시지는 "어느 키가, 왜, 어떻게 고치는지" 1줄씩.
3. **`.env.example` 재작성** — 3모드 각각의 최소 형태(모드 선언 + 비밀만)를 예시로. `.env.local`/`.env.dev` 파일 관례와 `.env.sync-client` 폐기 예고를 명시. `.gitignore` 에 `.env.local`/`.env.dev` 추가.
4. **테스트** (`tests/test_mode_presets.py`)
   - 모드 매트릭스: 세 모드 각각에서 파생값이 root plan 의 env 계약 표와 일치.
   - 우선순위: 명시 env 가 프리셋을 이긴다 / `TODO_MODE` 미설정 시 레거시 동작 불변.
   - preflight: 필수 누락·URL 형식 오류·`#` 혼입·모드 오타 각각 명확히 거부.
   - sync 역할 파생: dev→disabled, local→client, prod→server (`sync_role()`·`feature_flags()` 연동 확인).
5. **문서** — repo `CLAUDE.md` 에 TODO_MODE 계약(프리셋 표·우선순위·preflight·비밀 파일 관례) 기록, 기존 env 나열 부분 정리.

## Acceptance Criteria

- `TODO_MODE` 미설정 + 기존 env 조합으로 전체 테스트 suite green (레거시 무변경 증명).
- `TODO_MODE=local` + 비밀 6~8개만으로 앱이 기동하고 `sync_role()==client`, 프리셋 값들이 현재 compose 오버라이드와 동일.
- `TODO_MODE=prod` 프리셋이 NAS 현재 값과 동일 (내부 auth URL·DB 호스트 포함).
- preflight 실패 케이스가 기동을 막고 원인 키를 정확히 지목한다.
- 신규 테스트 + 기존 전체 테스트 green. `python -m compileall src` 통과.

## Report Back To Orchestrator

- 확정된 프리셋 표 (루트 compose 개편·todoctl 이 이 표를 소비한다).
- 빈 문자열 의미 키들의 최종 규칙 (`SYNC_PEER_URL` 등).
- preflight 가 deploy 스크립트의 기존 env 검증과 겹치는 부분 (스크립트 축소 제안).
- `.env.sync-client` → `.env.local` 이관 시 사용자가 옮겨야 할 키 목록.

## Decision Escalation

사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않는다. 작업을 중단하고 현재 orchestrator 에게 전달해 결정받은 뒤 진행한다. orchestrator 에 보고할 수 없으면 workspace root `../.idea/` 에 handoff 문서를 남긴다.

특히: 프리셋 값이 현재 실배포 값과 다르게 정해야 할 상황이 발견되면(정답지 불일치) 임의로 정하지 말고 보고한다.
