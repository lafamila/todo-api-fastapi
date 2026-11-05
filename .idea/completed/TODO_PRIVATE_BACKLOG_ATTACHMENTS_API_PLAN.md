---
status: COMPLETED
completed_at: 2026-11-05
summary: "개인 백로그 권한·동기화 v4·비공개 R2 첨부 API"
---

# API 실행

Canonical: `../../../.idea/completed/TODO_PRIVATE_BACKLOG_ATTACHMENTS_PLAN.md`.

## Repo Responsibility

Core API와 attachment API 파일 소유권은 root plan 기준. 서로 코드/테스트를 되돌리지 않는다.

## Work Items

- backlog(project,user) 범위/legacy 보존, direct REST/버전/소켓/issue/lock/sync/merge 권한 전부 개인화. viewer 자신의 메모쓰기 허용.
- memo_attachments additive DDL/helper 통합 및v4 whitelist/owner scope/canonical objectkey/변조 방지. v3 peer fail closed.
- raw bounded upload/private storage/인증 bytes/선택 ZIP·안전한 filename·retry 및 권한 race 처리. 환경키/운영 준비 문서.

## Acceptance Criteria

root 완료 기준의 MySQL8·S3 HTTP·권한/legacy/sync/실패/파일hash/ZIP 테스트 및 리뷰. 실제prod DB/R2 객체에 임의 테스트 금지.

## Report Back

파일, API 계약/DDL/sync 버전/새env, 테스트/검증범위·실제R2 미검증과 운영 준비사항. commit/push는 root.

## Implementation Evidence (2026-11-05)

- Core memo auth: `get_authorized_memo`, `can_read_memo`, `can_write_memo`,
  `require_memo_access`, `require_locked_memo_access`. Private backlog는 exact created_by+live
  membership, viewer own-write, project/service owner bypass 없음. 일반 memo 역할은 기존 유지.
- Backlog identity: deterministic `(project,user)`, GET/POST current account scope, legacy NULL creator는
  project owner로 id/content/history/updated_at 보존 backfill, first-child 이동도 created_by 보존.
- Attachment: additive memo_attachments + immutable origin_memo_id, stdlib SigV4 private S3 proxy,
  bounded spooling/size/count/ZIP, magic 재검증 inline, safe NFC/Cc/Cf filename, idempotent retry,
  active quota locking+membership current-read recheck. merge/convergence는 memo_id만 reparent하고
  origin/key/bytes를 보존한다.
- Sync v4: v3 이하 drift fail-closed; private memo/version/attachment/feed/issues/locks exact account scope;
  canonical key/immutable metadata/status 검증; metadata only sync; global syncApplied payload sanitized.
- MySQL8 core privacy/locking: 9 passed, 4 subtests. Sync private media: 13 passed, 20 subtests.
- Local S3/SigV4/API/storage focused verification: 73 passed + 별도 pure 8 passed, optional 1 skipped.
- 최종 root 검증: 실제 MySQL8+LocalStack 전체 API suite **309 passed, 101 subtests**, skip 없음.
- `compileall`, `git diff --check`, bounded security review 통과. 실제 R2/prod DB 접근 없음.
- 신규 환경키는 `.env.example`/`docs/memo-attachments.md`; 양쪽 sync node 동일 private bucket/prefix 필요.
  실제 R2 자격증명과 NAS gateway 64 MiB 적용은 운영 준비사항이다.
