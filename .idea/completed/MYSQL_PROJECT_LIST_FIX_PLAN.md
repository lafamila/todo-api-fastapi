---
status: COMPLETED
summary: "운영 MySQL 8의 프로젝트 목록 DISTINCT/ORDER BY 오류 3065 수정"
---

# 프로젝트 목록 MySQL 호환성 수정

- 증거: 로그인 후 GET /api/projects의 비-service-owner 분기가 SELECT DISTINCT와 parent.created_at 정렬을 결합하여 MySQL 오류3065로 실패한다.
- project_members는 UNIQUE(project_id,user_id), parent는 PK 조인이므로 목록 조인에서 프로젝트 행이 중복되지 않는다. 불필요한 DISTINCT만 제거한다.
- 기존 권한 필터/탐색 전용 부모/정렬/JSON 계약은 유지한다. 데이터 변경·SQL mode 완화·스키마 변경 없음.
- 검증: 임시 MySQL8(ONLY_FULL_GROUP_BY 활성)에서 기존 쿼리 실패 재현 → 같은 회귀 테스트 통과. 소유자/admin/editor/viewer/outsider별 flat/계층 목록·정렬·중복·숨김 검증. 관련 계층 회귀 및 native HTTP200 확인.
- API만 수정/커밋/push하고 운영 API 재배포 명령을 전달한다. 실제 NAS 배포는 사용자 수행. 기존 README 미커밋 변경 보존.

## 검증 (2026-11-05)

- 로컬에 있던 mysql:8.0 이미지로 임시 loopback/tmpfs DB 실행. VERSION()=8.0.45, 기본 SQL mode에 ONLY_FULL_GROUP_BY 포함 확인.
- 코드 변경 전 새 테스트가 실제 운영과 같은 오류3065를 재현함(비-service-owner의 flat/계층/빈 목록).
- 불필요한 DISTINCT 제거 후 목록+기존 계층 검증 13 passed, 8 subtests passed.
- MySQL8 전체 tests/ 실행: 258 passed, 61 subtests passed, skip 없음.
- 실제 프로젝트 라우터를 네이티브 uvicorn으로 실행: owner200/5개, editor200/3개, viewer200/2개, outsider200/0개. 권한·순서·탐색용 부모·중복 없음 확인.
- 운영/개발 DB 데이터 및 SQL mode 변경 없음. 임시 서버/테스트 DB 컨테이너 정리. 새 의존성/env/API 계약 변경 없음.
