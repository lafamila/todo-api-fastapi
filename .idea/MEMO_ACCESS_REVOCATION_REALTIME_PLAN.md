---
status: COMPLETED
summary: "이미 구독한 메모의 읽기 권한이 회수되면 재가입 요청에서 룸과 편집 락을 즉시 정리한다."
completed_at: 2026-09-22
completion_reason: "권한 회수 재가입에서 룸·락·lease를 정리하고 후속 content 미수신, 이중 room 정리 실패 시 연결 차단, 닫힌 소켓 denial 경합, viewer read 유지 회귀 테스트를 추가했으며 전체 222 tests와 compileall을 통과했다."
---

# Memo realtime access revocation

## 변경 범위

- `joinMemo` 읽기 권한 재검증 실패 시 기존 `memo:<id>` 룸 구독을 해제한다.
- 기존 `_leave_memo` 경로로 소유 락 해제를 시도하고, stale 로컬 락·lease 상태를 제거한다.
- 쓰기 권한만 없는 viewer의 읽기 룸 구독은 유지한다.
- DB 스키마와 외부 Socket.IO 이벤트 계약은 변경하지 않는다.

## 검토 통과 기준

- 권한 있는 가입 뒤 읽기 권한을 회수하고 재가입하면 룸, `rooms`, `socket_memos`,
  `socket_locks`, lease token이 제거된다.
- 권한 회수 뒤 해당 메모 룸에 발행된 내용은 그 소켓에 전달되지 않는다.
- 정리 중 오류가 나도 `memoAccessDenied` 알림은 전달된다.
- 쓰기 락 거부는 읽기 룸 구독을 제거하지 않는다.
- 관련 단위 테스트와 전체 API 테스트, `compileall`이 통과한다.
