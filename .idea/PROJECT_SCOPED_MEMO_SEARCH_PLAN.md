---
status: COMPLETED
summary: "프로젝트의 현재 메모 전체를 BM25 관련도순으로 검색하고 안전한 강조 구간과 문맥을 반환한다."
completed_at: 2026-09-14
completion_reason: "현재 메모 검색·권한·Unicode 강조·성능 보호를 구현하고 215 tests 및 compileall을 통과했다. DB 환경이 필요한 기존 59개 테스트는 skip됐다."
---

# Project-scoped ranked memo search — API

Root plan: `../../.idea/TODO_PROJECT_SEARCH_PLAN.md`

## 변경 범위

- `GET /api/projects/{project_id}/memos/search?q=...&limit=20` 추가
- 기존 세션 인증 및 프로젝트 읽기 권한을 그대로 적용하고 삭제 프로젝트를 404 처리
- `memos`의 삭제되지 않은 현재 행만 읽으며 `memo_versions`는 조회하지 않음
- 프로젝트 전체 현재 코퍼스를 평가한 뒤 BM25F 방식으로 제목에 가중치를 주고,
  검색어 커버리지·근접도·연속 구문 보정을 적용
- NFKC/casefold 정규화와 부분 문자열 term frequency로 한글 부분어 및 정규화 등가 검색 지원
- 제목/문맥을 `{text, match}` segment로 반환하며 문맥은 최대 220자로 제한
- 최대 16개 lexical term 및 프로세스당 동시 검색 2개로 CPU·코퍼스 메모리를 제한하고,
  포화 시 `429`와 `Retry-After: 1`을 반환
- DB 스키마, sync schema, 외부 의존성 변경 없음

## 검토 통과 기준

- 제목 가중치, 다중 검색어 커버리지와 근접도가 순위에 반영됨
- limit 전에 전체 코퍼스를 평가하며 응답 `total`은 전체 일치 문서 수임
- 한글 부분어와 NFC/NFD 등가 문자열이 검색·강조됨
- 문맥은 일치 밀도가 높은 구간을 선택하고 생략 부호를 별도 일반 segment로 반환함
- HTML처럼 보이는 원문도 문자열 segment로만 반환함
- 비멤버 403, 삭제 프로젝트 404, 삭제 메모 제외, 과거 버전 미조회가 검증됨
- q 1~200자, limit 1~50 검증 및 실제 ASGI route 호출 테스트가 통과함
- 전체 unittest, compileall, 10MB 코퍼스 성능 점검이 통과함
