---
status: PREPARED
summary: "todo-api-fastapi가 todo session/auth, Python Socket.IO, LiveKit token, REST API를 직접 소유하게 한다."
---

# TODO WEB SPLIT — todo-api-fastapi execution plan

Canonical orchestration plan:

`../.idea/TODO_WEB_SPLIT_PLAN.md`

## Repo Responsibility
`todo-api-fastapi` 는 독립 todo backend가 된다. 기존 REST API에 더해, `ted-yee-beer-house-api-nest` 가 맡던 todo session/auth, memo/screen-share realtime, LiveKit token 발급, account search 연동을 FastAPI 안으로 옮긴다.

## Inputs / Dependencies
- Root canonical plan: `/Users/lafamila/work/teddy/.idea/TODO_WEB_SPLIT_PLAN.md`
- Source BFF repo: `/Users/lafamila/work/teddy/ted-yee-beer-house-api-nest/src/todo`
- New frontend: `/Users/lafamila/work/teddy/todo-web-next`
- Auth provider: `auth-api-nest`
- Realtime decision: Python Socket.IO
- Service type: DEPLOY

## Work Items
1. Session/auth module을 추가한다.
   - `POST /api/session/login`
   - `POST /api/session/logout`
   - `GET /api/session/me`
   - `POST /api/session/service-application`
   - HttpOnly cookie 기반 session id 발급/삭제
   - 1차 구현 session store는 in-memory 허용
2. 기존 Nest `TodoSessionService` 흐름을 Python으로 이식한다.
   - auth-api `/login` 호출
   - `/oauth/authorize` code 획득
   - `/oauth/token` authorization_code / refresh_token 교환
   - `/oauth/revoke` 호출
   - access token decode 후 user 호환 shape 생성
3. REST route 인증 방식을 session cookie와 bearer token 양쪽을 처리할 수 있게 정리한다.
   - todo-web-next 브라우저 호출은 cookie session
   - 내부/테스트 호출은 bearer token 유지 가능
4. member invite용 account search를 처리한다.
   - 기존 BFF의 auth-api `service-search` 호출 역할을 FastAPI로 이동
   - `AUTH_SERVICE_KEYS_PLAN.md` 가 아직 미구현이면 현재 가능한 admin key/env 기반 최소 구현을 명확히 분리하고, service credential 전환은 follow-up으로 보고
5. Python Socket.IO 서버를 붙인다.
   - 기존 client가 쓰는 path와 호환되도록 설정
   - session cookie 검증으로 user 식별
   - memo room join/leave, lock/unlock, memoUpdated broadcast 이식
   - project screen-share join/leave/start/stop state broadcast 이식
6. LiveKit token endpoint를 추가/이식한다.
   - `POST /api/livekit/token`
   - room name은 `project:{projectId}` 규칙 유지
   - token identity/name은 session user 기준
7. CORS/cookie/env를 todo-web-next 기준으로 정리한다.
   - local origin `http://localhost:3034`
   - credentials 허용
   - prod domain은 env로 주입 가능하게 둔다.
8. Dockerfile/requirements를 업데이트한다.
   - Python Socket.IO dependency
   - LiveKit token 발급에 필요한 dependency
   - uvicorn/asgi app wrapping 방식 검증

## Acceptance Criteria
- `python -m compileall src` 가 통과한다.
- FastAPI app import가 통과한다.
- Docker build가 통과한다.
- session login/me/logout smoke가 가능하다.
- 기존 bearer token protected endpoint는 깨지지 않거나, 변경점이 명확히 문서화된다.
- Socket.IO client가 session cookie 기반으로 connect 가능하다.
- LiveKit token endpoint가 session user 기준 token을 반환한다.

## Report Back To Orchestrator
- auth-api 쪽에 필요한 OIDC client/redirect/service permission 설정 gap을 보고한다.
- `todo-web-next` 가 맞춰야 하는 socket path/env 이름을 보고한다.
- beer-house api에서 제거 가능한 todo 파일 목록 또는 남겨야 하는 임시 호환이 발견되면 보고한다.

## Decision Escalation
사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않는다. 작업을 중단하고 현재 orchestrator 에게 전달해 결정받은 뒤 진행한다. orchestrator 에 보고할 수 없으면 workspace root `.idea/` 에 handoff 문서를 남긴다.
