---
status: COMPLETED
completed_at: 2026-06-16
completion_reason: "Implemented infra-only root deployment model and repo deployment documentation."
summary: "todo-api-fastapi 를 독립 배포 backend 로 문서화하고 root compose 의 fastapi 앱 전제를 제거한다."
---

# WORKSPACE DEPLOY INFRA SPLIT — todo-api-fastapi execution plan

Canonical orchestration plan:

`../../.idea/WORKSPACE_DEPLOY_INFRA_SPLIT_PLAN.md`

## Repo Responsibility
`todo-api-fastapi` 는 todo backend, auth session callback, Socket.IO, LiveKit token API 를 소유한다. root compose 에 `fastapi` 앱 서비스로 배포된다는 전제를 제거하고, 자체 Dockerfile/env/local run/deploy build 문서를 기준으로 한다.

## Inputs / Dependencies
- MySQL/MariaDB 는 root infra compose 또는 운영 DB 를 사용한다.
- LiveKit 은 root infra compose 또는 운영 LiveKit 을 사용한다.
- auth 는 독립 배포된 `auth-api-nest` 를 사용한다.
- legacy todo 로컬 서비스/DB 는 건드리지 않는다.

## Work Items
1. `CLAUDE.md` 의 "Docker 빌드 가능 — DEPLOY. 루트 docker-compose fastapi 서비스" 표현을 독립 배포 표현으로 수정한다.
2. local run command 에 현재 repo 권장 실행 방식(`venv/bin/python3.13 -m src` 또는 실제 유지할 명령)을 명시한다.
3. `.env.example` 이 DB/auth/OIDC/LiveKit/service credential env shape 를 반영하는지 확인한다.
4. Dockerfile 이 root compose 의 internal service DNS 만 가정하지 않는지 확인한다.
5. README 에 root infra 사용 시 DB/LiveKit/auth URL 을 어떻게 넣는지 문서화한다.

## Acceptance Criteria
- root compose 의 `fastapi` 앱 서비스명을 기준으로 하는 문서가 제거된다.
- local command 와 Docker build command 가 구분된다.
- `.env.example` 기준으로 local/dev 설정을 재현할 수 있다.
- todo API 가 필요로 하는 root infra 목록이 명확하다.

## Report Back To Orchestrator
- todo-web-next 와 맞춰야 하는 public API URL/cookie/CORS 변경.
- auth service onboarding spec/env 변경 필요.
- DB port/name 변경 필요.

## Decision Escalation
사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않는다. 작업을 중단하고 현재 orchestrator 에게 전달해 결정받은 뒤 진행한다. orchestrator 에 보고할 수 없으면 workspace root `.idea/` 에 handoff 문서를 남긴다.

