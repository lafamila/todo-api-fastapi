# Fresh Todo DB + Auth Migration Plan

## Goal

`teddynote` 데이터베이스를 `todo` 로 이름만 바꿔 복사하는 작업이 아니라, 기존 데이터는 버리고 `todo` 서비스를 **새 DB + auth-api-nest 중앙 인증** 기준으로 다시 시작한다.

기존 `teddynote` 데이터는 보존할 필요가 없다. 이유는 `../auth-api-nest` 가 새로 생기면서 기존 `/todo` 의 로컬 계정, 로컬 JWT, `users` 테이블, admin flag 를 모두 중앙 계정과 서비스 권한으로 교체할 예정이기 때문이다.

이 문서는 기존 `DB Rename Plan — teddynote -> todo` 의 수동 데이터 복사 계획을 대체한다.

## Decision

- `teddynote` -> `todo` `mysqldump` 복사는 하지 않는다.
- `todo` DB는 빈 스키마에서 새로 생성한다.
- `todo-api-fastapi` 는 최종적으로 auth-api access token 을 검증하는 resource server 가 된다.
- `ted-yee-beer-house-api-nest` 는 단순 proxy 에서 인증 경계/BFF 역할로 확장된다.
- `ted-yee-beer-house-web-next` 는 `/todo` 로컬 로그인과 `localStorage.auth_token` 흐름을 제거하고 OIDC/BFF session 흐름으로 전환한다.
- 기존 사용자 데이터와 기존 프로젝트/메모 데이터는 마이그레이션하지 않는다.

## Affected Repos

- `todo-api-fastapi`
  - MySQL `todo` 스키마 소유자.
  - 현재 로컬 HS256 JWT + `users` 테이블을 검증한다.
  - 새 구조에서는 auth-api JWKS/issuer/audience/service permission claim 을 검증해야 한다.
- `ted-yee-beer-house-api-nest`
  - 현재 `/api/todo/*` 를 FastAPI 로 프록시한다.
  - LiveKit token 발급과 socket gateway 에서 기존 HS256 JWT 를 직접 검증한다.
  - 새 구조에서는 auth-api 연동, service session, access token refresh, socket/livekit identity 처리를 맡아야 한다.
- `ted-yee-beer-house-web-next`
  - 현재 `/todo/login` 에서 username/password 를 받고 `localStorage.auth_token` 에 access token 을 저장한다.
  - 새 구조에서는 auth-api OIDC login redirect 또는 BFF session endpoint 기반으로 바꿔야 한다.
- `auth-api-nest`
  - 중앙 계정, OIDC client, service permission, JWKS, token 발급의 source of truth.
  - `todo` service/client/permission seed 또는 admin 설정이 필요하다.
- workspace root
  - `docker-compose.yml`, `docker-compose.dev.yml` 에 auth-api 및 auth DB wiring 이 필요하다.

## Current Findings

- root compose 는 이미 FastAPI `DB_NAME: todo` 를 사용한다. 따라서 DB 이름 자체는 새 이름으로 가는 경로가 열려 있다.
- 기존 계획서는 `teddynote` 데이터를 `todo` 로 복사하는 절차였지만, 이 요구사항과 맞지 않는다.
- FastAPI 인증은 아직 로컬 auth 다.
  - `src/auth_utils.py`: HS256 `SECRET_KEY` 로 JWT decode 후 `users` 테이블 조회.
  - `src/connectors/__init__.py`: `users`, `project_members` 를 로컬 계정 FK 기준으로 생성.
- BFF 도 기존 token 형식에 의존한다.
  - `src/todo/todo.service.ts`: LiveKit token 발급 시 기존 JWT 를 직접 verify.
  - `src/todo/memo.gateway.ts`: socket connection 에서 기존 JWT 를 직접 verify.
- Frontend 도 기존 token 저장 방식에 의존한다.
  - `src/contexts/AuthContext.tsx`: `localStorage.auth_token`.
  - `/todo/login`: 로컬 username/password form.
- auth-api 계획은 BFF/server-side session 중심을 권장한다.
  - access token 은 `aud: service:todo`.
  - service claim 은 `https://lafamila.xyz/claims/service`.
  - 서비스는 auth DB 를 직접 조회하지 않고 OIDC discovery/JWKS/issuer/audience/service permission claim 에만 의존한다.

## Recommended Migration Sequence

### 1. Plan Update Only

이 문서 갱신이 1단계다. 이 단계에서는 코드, DB, compose 실행을 변경하지 않는다.

결과물:

- 기존 데이터 복사 계획을 폐기하고 fresh DB 계획으로 대체한다.
- BFF와 Frontend 도 수정 대상임을 명시한다.
- 후속 단계와 검증 기준을 정의한다.

### 2. auth-api-nest Runtime Readiness

목표: `auth-api-nest` 를 실제 `todo` 연동에 쓸 수 있는 상태로 만든다.

작업:

- root compose 에 auth-api 서비스와 auth DB(PostgreSQL) 추가.
- signing key persistence 확인 및 보강.
- refresh token persistence 확인 및 보강.
- `todo` service 등록.
- `todo-web` 또는 BFF client 등록.
- `owner`, `admin`, `user`, `visitor` service permission enum 정의.
- 프로젝트 role 은 `owner`, `editor`, `viewer` 로 분리.
- seed admin 계정에 `todo` 권한 부여.

검토 통과 기준:

- auth-api 가 compose 로 기동된다.
- OIDC discovery endpoint 와 JWKS endpoint 가 응답한다.
- admin/seed 계정으로 `todo` access token 을 발급할 수 있다.
- access token 에 `aud=service:todo` 와 service permission claim 이 포함된다.

### 3. BFF Auth Boundary

목표: `ted-yee-beer-house-api-nest` 가 브라우저와 auth-api 사이의 인증 경계가 된다.

작업:

- OIDC authorization code + PKCE callback 처리.
- refresh token 을 브라우저 localStorage 에 두지 않고 서버 측 session 또는 HttpOnly cookie 기반으로 관리.
- BFF session 에서 FastAPI 호출용 access token 을 갱신/첨부.
- `/api/todo/auth/me` 또는 별도 session endpoint 를 중앙 계정 기준으로 제공.
- 기존 `/api/todo/auth/login`, `/register`, `/change-password`, `/users/reset-password` proxy 제거 또는 auth-api admin/user API 로 대체.
- LiveKit token 발급 시 기존 HS256 verify 를 제거하고 BFF session/auth-api token identity 를 사용.
- socket gateway 인증도 기존 HS256 verify 에서 새 session/access-token 검증 흐름으로 교체.

검토 통과 기준:

- BFF 가 auth-api 로 로그인 callback 을 처리한다.
- 브라우저가 refresh token 을 직접 보관하지 않는다.
- BFF 가 FastAPI 로 `Authorization: Bearer <auth-api access token>` 을 전달한다.
- LiveKit token 발급과 socket connection 이 중앙 계정 id/display name 기준으로 동작한다.

### 4. Frontend Auth Flow

목표: `ted-yee-beer-house-web-next` 의 `/todo` 가 로컬 로그인/token storage 를 사용하지 않는다.

작업:

- `/todo/login` 을 auth-api/BFF login redirect 시작점으로 변경.
- `AuthContext` 에서 `localStorage.auth_token` 제거.
- `getMe`, logout, session restore 를 BFF session endpoint 기준으로 변경.
- 기존 register/change password UI/API 는 auth-api 흐름으로 이동 또는 제거.
- API client 는 직접 bearer token 을 조립하지 않고 BFF session 기반 호출을 사용한다.

검토 통과 기준:

- 새 브라우저 세션에서 `/todo` 접근 시 auth flow 로 이동한다.
- 로그인 후 `/todo` 로 돌아오며 현재 사용자 정보가 표시된다.
- 새로고침 후에도 BFF session 으로 사용자 상태가 복원된다.
- logout 후 `/todo` 보호 라우트 접근이 차단된다.

### 5. FastAPI Resource Server Conversion

목표: `todo-api-fastapi` 가 로컬 auth server 가 아니라 `todo` resource server 가 된다.

작업:

- `auth_utils.py` 에서 HS256 `SECRET_KEY` 검증과 `users` table lookup 제거.
- auth-api OIDC discovery/JWKS 기반 RS256 검증 추가.
- expected issuer, expected audience(`service:todo`), service claim 검증 추가.
- `require_admin`, `require_super_admin` 을 `todo` service permission 기준으로 재정의.
- `/api/auth/login`, `/api/auth/register`, `/api/auth/change-password`, `/api/users/*` 로컬 계정 API 제거 또는 deprecated 처리.
- route 들이 `user["id"]` 대신 central `account_id` 를 사용하도록 정리.

검토 통과 기준:

- auth-api access token 으로 FastAPI protected endpoint 호출이 성공한다.
- audience/issuer/permission 이 틀린 token 은 거부된다.
- 로컬 `users` 테이블 없이 프로젝트/메모 권한 판단이 가능하다.

### 6. Fresh Todo DB Schema

목표: 기존 로컬 계정 FK 를 제거한 fresh `todo` schema 를 만든다.

작업:

- `users` 테이블 생성 제거.
- `project_members.user_id` 를 auth-api account id 문자열로 저장하는 컬럼으로 재정의한다. 이름은 `account_id` 를 권장한다.
- `projects.owner_id`, `memos.created_by` 도 central account id 의미로 정리한다. 가능하면 `owner_account_id`, `created_by_account_id` 로 rename 한다.
- `project_members` 는 auth DB FK 를 걸지 않는다. 서비스 DB 는 auth DB 를 직접 참조하지 않는다.
- 프로젝트 password/is_secret 의미를 재검토한다. 중앙 auth 전환 후 평문 project password 는 제거 대상이다.
- seed 또는 admin bootstrap 으로 첫 프로젝트를 만들 수 있는 경로를 마련한다.

검토 통과 기준:

- 빈 MySQL volume 에서 FastAPI startup 만으로 `todo` schema 가 생성된다.
- `users` 테이블 없이 프로젝트 생성/목록/메모 생성/수정/삭제가 동작한다.
- 기존 `teddynote` schema 와 count 비교를 하지 않는다.

### 7. End-to-End Verification

목표: Next -> BFF -> FastAPI -> MySQL 전체 경로가 중앙 인증 기준으로 동작한다.

검토 통과 기준:

- compose 환경에서 auth-api, BFF, FastAPI, Next, MySQL 이 함께 기동된다.
- 브라우저에서 `/todo` 접근 -> auth login -> `/todo` 복귀가 성공한다.
- 프로젝트 생성, 멤버 초대, 메모 생성/수정/버전 조회, 일괄 삭제가 권한별로 동작한다.
- service `owner/admin/user/visitor` 와 project `owner/editor/viewer` 권한 차이가 의도대로 적용된다.
- LiveKit 화면 공유 token 발급이 성공한다.
- socket memo lock/update 흐름이 중앙 계정 identity 로 동작한다.
- 더 이상 `/todo` 기능이 FastAPI local auth endpoint 에 의존하지 않는다.

## Deprecated Previous Manual Copy Sequence

이전 계획의 다음 흐름은 폐기한다.

```bash
mysqldump teddynote | mysql todo
```

이유:

- 기존 데이터는 보존 대상이 아니다.
- 기존 데이터에는 로컬 users/admin/password 전제가 섞여 있다.
- auth-api-nest 전환 후 기존 계정 id 와 권한 모델을 그대로 쓸 수 없다.
- 복사 마이그레이션은 새 중앙 인증 구조의 결합도를 높이고 불필요한 호환 코드를 만들 가능성이 크다.

## Remaining Risks / Open Questions

- auth-api 의 signing key 와 refresh token persistence 가 운영 수준인지 확인해야 한다.
- BFF server-side session 저장소를 무엇으로 둘지 정해야 한다. 초기에는 memory 로 가능하지만 운영은 Redis/DB 등 persistent store 가 필요할 수 있다.
- `todo` service permission enum 은 `owner/admin/user/visitor` 로 확정했다. 프로젝트 role 은 `owner/editor/viewer` 로 분리한다.
- project member 검색은 auth-api account search 를 BFF 가 호출하는 방식이 자연스럽지만, admin API 권한 모델과 rate limit 이 필요하다.
- project password 기능은 중앙 auth 이후 제거하는 것이 맞아 보이나, UI/기존 UX 기대가 있으면 별도 대체 UX가 필요하다.
- socket 인증을 BFF session cookie 로 할지, 짧은 수명의 socket token 으로 할지 결정해야 한다.
