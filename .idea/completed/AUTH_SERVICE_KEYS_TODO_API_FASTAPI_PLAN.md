---
status: COMPLETED
completed_at: 2026-05-24
completion_reason: "Switched todo account search from auth admin key to service credential internal API and verified Python compile."
summary: "todo account search의 `x-admin-key` 의존을 제거하고 auth service credential 기반 internal API 호출로 전환한다."
---

# AUTH SERVICE KEYS PLAN — todo-api-fastapi execution plan

Canonical orchestration plan:

`../../.idea/AUTH_SERVICE_KEYS_PLAN.md`

## Repo Responsibility

`todo-api-fastapi` 는 auth admin key 를 사용하지 않고, todo 서비스에 발급된 service credential 로만 auth internal API 를 호출해야 한다.

현재 대상 흐름:

- `TodoSessionService.search_accounts()`
- 기존 endpoint: `GET /api/admin/accounts/service-search?serviceKey=todo&q=...`
- 기존 header: `x-admin-key`

변경 후:

- endpoint: `GET /api/internal/service-accounts/search?serviceKey=todo&q=...`
- headers:
  - `x-auth-service-key-id`
  - `x-auth-service-secret`

## Inputs / Dependencies

- Root canonical plan: `../../.idea/AUTH_SERVICE_KEYS_PLAN.md`
- auth-api repo plan: `../../auth-api-nest/.idea/AUTH_SERVICE_KEYS_AUTH_API_NEST_PLAN.md`
- Current config: `src/config.py`
- Current auth integration: `src/services/session_auth.py`
- Current response parser expects fields:
  - `id`
  - `loginId`
  - `name`
  - `email`
  - `isSuperAdmin`
  - `permissionKey`

## Work Items

1. Update config env names.
   - Add:
     - `AUTH_SERVICE_KEY_ID`
     - `AUTH_SERVICE_SECRET`
   - Remove runtime dependence on:
     - `AUTH_ADMIN_API_KEY`
     - `ADMIN_API_KEY`
   - If temporary backward compatibility is kept, make it explicit and warn in code/docs. Acceptance requires service credential path.

2. Update account search call.
   - Change URL to:
     - `{AUTH_API_BASE_URL}/api/internal/service-accounts/search?serviceKey=todo&q=...`
   - Change headers to:
     - `x-auth-service-key-id: AUTH_SERVICE_KEY_ID`
     - `x-auth-service-secret: AUTH_SERVICE_SECRET`
   - Keep response parsing compatible with existing auth response shape.

3. Handle missing credentials clearly.
   - If either `AUTH_SERVICE_KEY_ID` or `AUTH_SERVICE_SECRET` is empty, account search should fail with a clear `503`/configuration error.
   - Login/OIDC flows should not require service credential unless they call account search.

4. Update docs/env examples if present.
   - If this repo has or gains `.env.example`, document:
     - `AUTH_SERVICE_KEY_ID`
     - `AUTH_SERVICE_SECRET`
   - Make clear that these are server-only secrets and must never be exposed to `todo-web-next`.

5. Coordinate with workspace root compose.
   - root compose should pass `AUTH_SERVICE_KEY_ID` and `AUTH_SERVICE_SECRET` into fastapi.
   - root compose should stop passing `AUTH_ADMIN_API_KEY` to fastapi.

6. Run verification.
   - `python3 -m py_compile src/config.py src/services/session_auth.py`
   - Run existing tests if available.
   - If auth-api is running with a generated todo credential, manually verify account search endpoint through todo invite/search flow.

## Acceptance Criteria

- `todo-api-fastapi` no longer sends `x-admin-key` to auth-api for account search.
- `todo-api-fastapi` reads `AUTH_SERVICE_KEY_ID` and `AUTH_SERVICE_SECRET`.
- `todo-api-fastapi` calls `/api/internal/service-accounts/search`.
- Missing service credential produces a clear configuration error for account search.
- Existing account search response mapping remains compatible with todo UI.
- No frontend env or browser code contains service credential.
- Python compile/test verification passes or exact failures are reported.

## Report Back To Orchestrator

- Report if auth-api endpoint path/header names differ from this plan after implementation.
- Report if account search response shape changes.
- Report if root compose still injects `AUTH_ADMIN_API_KEY` after repo work.
- Report any manual step needed to create the first todo credential in auth admin.

## Decision Escalation

사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않는다. 작업을 중단하고 현재 orchestrator 에게 전달해 결정받은 뒤 진행한다. orchestrator 에 보고할 수 없으면 workspace root `.idea/` 에 handoff 문서를 남긴다.
