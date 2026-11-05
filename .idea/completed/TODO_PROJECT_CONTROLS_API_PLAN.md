---
status: COMPLETED
summary: "프로젝트 rename과 이름 확인형 원자적 다중 메모 이동 API"
---

# API 실행 계획

Canonical: `../../../.idea/completed/TODO_PROJECT_CONTROLS_REFINEMENT_PLAN.md`

## Repo Responsibility

프로젝트 rename, 두 이름 확인형 bulk move, sync 구조 권한 일관성, MySQL8 검증.

## Work Items

1. root의 PATCH rename/POST bulk-move 계약 구현. 정확한 두 프로젝트명/양쪽 소유권/leaf/live/전체 동일source/제목충돌 검증.
2. 전 행 검증 후 단일 트랜잭션, 메모/버전ID·본문·편집시각 보존, 게시글/소켓 권한 동반 갱신.
3. cross-root 이동은 두 root 실제 소유자만 sync에서도 허용, 기존 위조/editor 이동 차단 유지.
4. MySQL8 격리 테스트, 관련 회귀/문서 반영. 기존 README 보존, no schema/env/dependency change.

## Acceptance Criteria

실제 MySQL8에서 정상/원자성/확인 이름/권한/target충돌/삭제/동기화 테스트, 기존 전체회귀. root native browser 통합 후 검토.

## Report Back To Orchestrator

변경 파일/검증 명령/API 계약 차이/남은 위험. root 다른 파일 수정 및 자체 commit/push 금지.

## 완료 증거 (2026-11-05)

- MySQL8 전체 API 267 passed/73 subtests, root 독립 핵심 회귀29 passed/22 subtests. compileall/diff-check 통과, security/atomicity APPROVE.
- 실제 native HTTP/browser에서 PATCH200, 두 이름 불일치 버튼 차단, 일반 메모2개 bulk200/ID·본문hash·버전 보존 및 dirty 후속PUT200 확인.
- 기존 README 변경 보존. schema/env/dependency 변경 없음. cross-root 이동을 사용하려면 서버/로컬 API 코드 모두 갱신해야 한다.

## Implementation Evidence (2026-11-05)

- `PATCH /api/projects/{id}`: owner/live/ancestor, trim 후 공백·255자, 같은 hierarchy scope 중복을
  검증하고 display/sync timestamps를 함께 갱신한다. 관계·멤버십은 변경하지 않는다.
- `POST /api/memos/bulk-move`: 양쪽 실제 root owner, exact source/target 이름, live leaf,
  unique regular same-source memo 집합, selected/target title 충돌을 모두 검증한 뒤 단일 transaction으로
  memo/article 참조를 이동한다. ID/history/content/updated_at은 보존한다.
- 단일/다중 이동 모두 root→leaf→memo lock order와 locking collision read를 사용해 동시 같은-title
  이동에서 정확히 하나만 성공한다. 실패/충돌/확인 불일치는 memo/article 모두 rollback된다.
- sync v3 cross-root memo 이동은 양쪽 actual live root `owner_id == account_id`일 때만 허용한다.
  editor/타인 root와 backlog manual move는 거부하고 first-child group backlog migration은 유지한다.
- 새 schema/env/dependency 없음.
- 격리 MySQL 8 (`todo_sync_controls_test`, `ONLY_FULL_GROUP_BY`) focused: 27 passed, 14 subtests.
- 동일 MySQL 8 전체 API suite: 267 passed, 73 subtests.
- compileall/diff-check 및 최종 security/atomicity review 통과.
