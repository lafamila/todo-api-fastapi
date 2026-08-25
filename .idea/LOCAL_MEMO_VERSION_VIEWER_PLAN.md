---
status: COMPLETED
summary: "메모 버전 조회 UI를 위치 축의 로컬 모드에서만 노출하도록 session feature flag를 추가한다."
completed_at: 2026-08-25
completion_reason: "위치축 feature flag와 metadata-only 버전 목록 계약을 구현하고 전체 테스트를 통과했다."
---

# Local Memo Version Viewer — API

## 변경

- 기존 `memo_versions` 조회 API는 그대로 사용한다.
- `/api/session/me.features.memoVersionHistory`를 추가한다.
- `dev-local`·`prod-local`에서는 `true`, `dev-prod`·`prod-prod`에서는 `false`다.

## 검증

- `tests/test_feature_flags.py`
- `tests/test_mode_presets.py`
- 전체 unit test
