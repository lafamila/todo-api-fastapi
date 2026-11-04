---
status: COMPLETED
summary: "Todo 계층·초대 snapshot·메모 이동·백로그·soft delete API/sync 구현"
---

# Todo hierarchy/backlog — API execution plan

Canonical: `../../../.idea/completed/TODO_SUBPROJECT_BACKLOG_PLAN.md`

## Repo Responsibility

projects/memos/members 스키마·트랜잭션·권한·백로그 singleton, 모든 접근 경로의 삭제 차단, sync schema/복제, 테스트. 다른 repo/root 파일 수정 금지.

## Inputs / Dependencies

root 계약 확정: 첫 자식은 기존 메모와 멤버 권한 복사, 상위는 아코디언 전용 그룹(본문/백로그 없음). root의 API/Web 공통 계약을 따른다. 모든 결정 확정, 실행 가능.

## Work Items

1. 부모 관계와 백로그 메모 구분의 additive idempotent DDL, 기존 행 보존, sync whitelist/version/의존성 갱신.
2. 부모 owner만 자식 생성. 최초 생성+기존 메모 전체 이동 원자성. 입력 검증/동시성/잠금 순서.
3. 자식별 멤버십. 루트 초대에 현재 자식 전체/선택 snapshot 입력, 무권한 자식 ID 거부, 미래 자식 비상속.
4. 루트 owner의 형제 간 메모 이동 API, 기존 ID·버전·생성/본문 유지, 동시 편집 CAS/lease 처리, 게시글 경로 정합성.
5. 프로젝트별 backlog 조회/초기화, 기존 memo CRUD/version/lease 사용, 목록/일반 생성/이동/병합과 구분.
6. 이름 정확 일치/생성자만 삭제, soft delete 및 부모 삭제된 자식 차단. REST 검색/버전/게시/Socket.IO 직접 접근 포함.
7. sync 부모 순서·늦은 자식·이동·동시 singleton/병합 충돌 보존. scratch DB에서 검증하고 schema migration/배포순서 문서화.

## Acceptance Criteria

- root 완료 증거 매트릭스 2/3/4/5/7의 서버 증거 전부, 기존 API 회귀, 2노드 실 DB 동기화.
- tests/scratch_db.py 가드 강제, 실사용/개발 DB 변경 금지. 네이티브 실행 테스트 후 자신이 만든 프로세스만 정리.
- root plan의 결정에 맞는 authz (서비스 owner 우회와 생성자 전용 삭제를 혼동하지 않음).

## Report Back To Orchestrator

변경 파일, API 계약, 테스트 결과, sync version/rolling 배포 제약, 남은 위험. 코드/API 문서 동기화. README의 기존 사용자 변경 보존. 커밋/push는 root 조율.

## Decision Escalation

root 결정 gate 통과. 새로운 권한·데이터 노출 정책이 필요하면 orchestrator에 보고.

## 완료 검증 (2026-11-04)

- 최종 root 독립 실행: `venv/bin/python -m pytest tests/ -q` → 256 passed, 53 subtests passed.
- 첫 자식/백로그 동시 생성, 이후 멤버 비상속, creator-only 정확 이름 soft delete, 버전/본문/시간 보존, CRUD/공개 조회/realtime 차단 검증.
- sync v3 구조 위조·권한 회수·삭제 부활·부모 의존성·백로그 수렴·push/pull 구독 회수 테스트 및 최종 security APPROVE.
- 실제 native API/Socket.IO+scratch DB 브라우저에서 첫 이동, 멤버별 초대, 이동, viewer 실시간 삭제 반영 확인.
- 루트 배포 안내 `.docs/TODO_SUBPROJECT_BACKLOG_RELEASE.md`에 schema v3 양쪽 API 갱신 순서와 기존 위험 기록. 실제 NAS/로컬 Docker 재빌드 없음.
- README 및 동시 social-only 계정 검색 변경(session_auth/tests/해당 CLAUDE 문단)은 기능 커밋에서 제외하고 보존.

## Implementation Evidence (2026-11-04)

- additive v3 schema: projects.parent_id/is_group, memos.kind; 기존 DB는 init_db 멱등 DDL.
- 첫 자식 transaction: parent row lock, 모든 memo(tombstone/backlog 포함)·article 재부모화,
  ID/content/version/updated_at 보존, live member role snapshot. 동시 first-child test 통과.
- 이후 자식 owner-only, root 초대 all/selected snapshot, child-only navigation metadata 분리.
- sibling move/root creator auth, target title collision, public article relocation, post-commit realtime sweep.
- deterministic leaf backlog 자동 생성 + legacy get-or-create; concurrent initialization test 통과.
- exact creator/name recursive soft delete; rows retained, public/direct/realtime access fail-closed.
- sync v3 structural authorization, v2 drift fail-closed, hierarchy scope confidentiality,
  irreversible tombstones, duplicate backlog convergence, pull/push access sweep.
- `venv/bin/python -m compileall -q src tests` 통과.
- `venv/bin/python -m pytest tests/ -q` → 255 passed, 51 subtests passed.
- 실제 사용자/개발 DB는 사용하지 않았고 `teddynote_sync_*` guard DB만 사용했다.
- 새 환경변수 없음. v2↔v3 sync는 hierarchy 권한 필드 유실 방지를 위해 의도적으로 막히므로
  운영 API와 prod-local API를 모두 v3로 순차 갱신해야 한다(중간에는 sync가 fail-closed).
