---
status: COMPLETED
completed_at: 2026-11-05
summary: "raw attachment 업로드 superadmin guard"
---

Canonical: `../../../.idea/completed/TODO_SUPERADMIN_UPLOAD_PLAN.md`

## Repo Responsibility / Work Items

upload_memo_attachment 시작점에서 canonical superadmin 검증, 기존 memo ACL 유지.
권한 matrix·읽기 유지 regression 및 기존 upload fixture 갱신, CLAUDE/docs 갱신.

## Acceptance Criteria / Report Back

API tests/compile 통과, 비-superadmin의 body/DB/S3 side effect 없음, no new env/schema/dependency.

완료: 실제 MySQL8/LocalStack 전체315 passed+118 subtests, compile/diff 검사와 보안 리뷰 통과.
비-superadmin은 raw HTTP로도403, 기존 멤버의 목록/다운로드 및 superadmin memo ACL 유지.
R2 버킷 생성은 자동이 아님·무료 allowance·환경값/토큰 scope 공식 근거도 배포 문서에 명시했다.
