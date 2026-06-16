---
status: COMPLETED
summary: "todo 의 ID/PW session login 을 OIDC start/callback 기반 session 발급으로 교체한다."
completed_at: 2026-06-16
completion_reason: "todo OIDC session flow 구현 및 로컬 검증 완료"
---

# AUTH_HOSTED_LOGIN_PLAN — todo-api-fastapi execution plan

Canonical orchestration plan:

`../../.idea/AUTH_HOSTED_LOGIN_PLAN.md`

## Repo Responsibility
todo-web-next 가 auth-hosted login 을 사용할 수 있도록 service backend/BFF 역할을 맡는다. 중앙 계정 credential 은 받지 않고, OIDC code 를 token 으로 교환한 뒤 todo 자체 session cookie 를 발급한다.

## Inputs / Dependencies
- auth-api-nest 에 등록된 todo OIDC client.
- `.env.example` 기준 `AUTH_API_BASE_URL`, `TODO_OIDC_CLIENT_ID`, `TODO_OIDC_CLIENT_SECRET`, `TODO_OIDC_REDIRECT_URI`.
- 기존 todo session cookie 설정: `TODO_SESSION_COOKIE_NAME`, `TODO_SESSION_*`.
- access denied 는 auth 공통 화면이 아니라 todo 가 받은 role/permission claim 또는 callback/token error 를 기준으로 todo 서비스가 처리한다.

## Work Items
1. `SessionLoginRequest` 와 `/api/session/login` credential 기반 contract 를 제거한다.
2. `/api/session/oidc/start` 를 추가한다.
   - PKCE verifier, challenge, state, login transaction 을 서버 메모리 또는 기존 local session store 패턴에 저장한다.
   - auth-api-nest `/oauth/authorize` URL 을 반환한다.
3. `/api/todo/session/callback` 또는 현재 env 의 `TODO_OIDC_REDIRECT_URI` 와 일치하는 callback endpoint 를 구현한다.
   - code/state/error 를 처리한다.
   - code 를 `/oauth/token` 으로 교환한다.
   - 기존 `TodoSession` 을 생성하고 HttpOnly todo session cookie 를 발급한다.
   - 성공 후 todo-web-next 로 redirect 한다.
   - access denied/error 는 todo-web-next 가 자체 처리할 수 있도록 명확한 error query 또는 response 로 전달한다.
4. 필요하면 `/api/session/oidc/complete` 를 추가하되, web callback redirect 만으로 충분하면 만들지 않는다.
5. `_create_auth_api_session()` 과 auth `/login` 호출을 제거한다.
6. 기존 token refresh, `/api/session/me`, `/api/session/logout`, service application, account search 는 유지한다.
7. `.env.example` 에 필요한 redirect/front URL 값이 빠져 있으면 먼저 추가하고 `.env` 변경 필요사항을 보고한다.
8. tests 를 추가/수정한다.

## Acceptance Criteria
- todo-api-fastapi 는 중앙 계정 ID/PW 를 입력받는 API 를 제공하지 않는다.
- login-start 응답으로 auth authorize URL 이 생성된다.
- callback 이 token exchange 후 기존 todo session cookie 를 발급한다.
- `/api/session/me` 가 callback 이후 user 를 반환한다.
- legacy `_create_auth_api_session` 코드와 `/session/login` 의존이 사라진다.

## Report Back To Orchestrator
- 최종 callback path 와 `.env.example` 에 추가/변경된 값.
- todo-web-next 가 호출해야 하는 endpoint 및 redirect 처리 방식.
- auth-api-nest 쪽 OIDC redirect URI 등록 변경 필요 여부.

## Decision Escalation
사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않는다. 작업을 중단하고 현재 orchestrator 에게 전달해 결정받은 뒤 진행한다. orchestrator 에 보고할 수 없으면 workspace root `.idea/` 에 handoff 문서를 남긴다.
