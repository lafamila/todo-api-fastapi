---
status: COMPLETED
summary: "브라우저 임시저장의 안전한 재시도를 위해 메모 응답에 opaque revision을 제공하고 PUT에 선택적 CAS를 추가한다."
completed_at: 2026-09-01
completion_reason: "opaque revision과 선택적 원자 CAS를 구현하고 전체 200 tests/46 subtests 및 compileall을 통과했다."
---

# Memo CAS for Draft Recovery — API

## 변경 범위

- DB 및 sync schema 컬럼은 추가하지 않는다.
- `memo.id`, `memo.content`, `memo.updated_at_utc`를 canonical JSON으로 직렬화한 뒤
  SHA-256으로 해시하여 opaque `revision`을 만든다.
- 모든 현재 메모 응답에 `revision`을 포함한다.
- `PUT /api/memos/{memo_id}`의 요청에 선택적 `baseRevision`을 추가한다.
- PUT은 메모 행을 `SELECT ... FOR UPDATE`로 잠근 뒤 권한·lease를 검증한다.
- `baseRevision`이 있고 현재 revision과 다르면 쓰기와 버전 생성을 하지 않고
  `memo_content_conflict` 409와 최신 메모 스냅샷을 반환한다.
- 기존 클라이언트처럼 `baseRevision`을 생략한 요청은 하위 호환을 위해 허용한다.

## 검토 통과 기준

- 같은 메모 행은 항상 같은 revision을 만들고, 본문 또는 `updated_at_utc`가 바뀌면
  revision도 바뀐다.
- 일치하는 CAS 저장은 기존 버전 생성과 본문 갱신을 수행하고 새 revision을 반환한다.
- 불일치하는 CAS 저장은 UPDATE와 `memo_versions` INSERT를 모두 수행하지 않는다.
- `memo_lease_required` 응답 계약은 변하지 않는다.
- 전체 unittest와 `python -m compileall src`가 통과한다.

## Cross-repo 계약

- `todo-web-next`는 GET/목록/생성/PUT 응답의 `revision`을 보관하고 저장 요청의
  `baseRevision`으로 보낸다.
- `memo_content_conflict`의 `detail.current`는 일반 Memo 응답과 같은 형태다.
