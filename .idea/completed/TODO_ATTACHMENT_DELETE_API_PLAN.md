---
status: COMPLETED
completed_at: 2026-11-05
summary: "업로더 본인만 첨부 batch tombstone 삭제·sync 우회 차단"
---

Canonical: `../../../.idea/completed/TODO_ATTACHMENT_DELETE_SELECTION_PLAN.md`

## Repo Responsibility / Work Items

기존 memo auth/locked read helper와 attachment UTC·sync tombstone 재사용. 원자적 batch delete endpoint,
exact created_by 검증·관리자 우회 금지·read membership 필수. storage IO/physical deletion 없음.
raw sync 타인 delete/undelete 우회도 차단하되 기존 merge/reparent 보존. 모델/테스트/docs 갱신.

## Acceptance Criteria / Report Back

root MySQL8/API/security matrix, fixture fake storage 미호출, compile/diff/test 통과.
새 환경값/의존성/DDL 불필요. commit/push/서버/컨테이너 lifecycle은 root만 수행.

완료: 실제 MySQL8+LocalStack 전체327 passed/127 subtests(skip0), compile/diff 및 security review APPROVE.
삭제12 tests/동시성·멱등·혼합 rollback·uploader/owner/admin/viewer/private/read·raw sync 경계 검증.
실제HTTP/nativeWeb 본인삭제·타인403·다운로드404 확인. storage 호출/실제 R2 삭제 없음.
