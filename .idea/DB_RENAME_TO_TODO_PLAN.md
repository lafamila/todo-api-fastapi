# DB Rename Plan — `teddynote` → `todo`

## Goal

워크스페이스에서 "TeddyNote" 브랜드/컨셉을 완전히 제거한 결정(2026-05-21)에 따라, MySQL 데이터베이스 이름도 `teddynote` 에서 서비스명과 일치하는 `todo` 로 옮긴다. 코드/설정 파일은 이미 모두 `todo` 로 변경됐고, **남아있는 작업은 운영중 데이터의 수동 이관 한 가지** 다.

## 영향받는 레포

- **todo-api-fastapi** (주 레포) — `DB_NAME` 환경변수를 읽어 mysql 에 접속. 이관 후 새 DB 로 자동 연결됨.
- (이미 코드/설정 변경 완료) `docker-compose.yml`, `docker-compose.dev.yml`, `.claude/settings.local.json`, `.omx/plans/*` 가 `todo` 를 가리키도록 갱신됨.

부수 영향 없음 — `ted-yee-beer-house-api-nest` 는 fastapi 로 proxy 할 뿐 DB 직접 접근하지 않음. `travel-api-fastapi` 는 별도 DB (`travelnote`) 사용.

## 현재 상태

- MySQL 컨테이너 `teddy-mysql` 의 볼륨 `mysql-data` 에 `teddynote` 스키마가 살아있음 (`projects`, `memos`, `memo_versions` 테이블, plaintext password 포함).
- todo-api-fastapi 의 `connectors/__init__.py` 가 `init_db()` 에서 `DB_NAME` 으로 DB 를 만들기 때문에, 컨테이너 재기동시 빈 `todo` DB 가 생성되지만 **기존 데이터는 `teddynote` 에 그대로 남는다** (소실되진 않음).
- docker-compose env 가 이미 `DB_NAME: todo` 로 바뀌어 있어, 이관 전에 fastapi 재기동하면 데이터 없는 빈 화면이 보임.

## 제안 변경 (수동 이관 시퀀스)

```bash
# 0. 사전: mysql 컨테이너가 살아있는지 확인
docker ps | grep teddy-mysql

# 1. 안전 스냅샷
docker exec teddy-mysql sh -c 'mysqldump -uroot -pP@ssw0rd teddynote' \
  > /tmp/teddynote-backup-$(date +%Y%m%d-%H%M%S).sql

# 2. 새 DB 생성
docker exec teddy-mysql sh -c \
  'mysql -uroot -pP@ssw0rd -e "CREATE DATABASE IF NOT EXISTS todo CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"'

# 3. 데이터 복사 (스냅샷 통해서 안전하게)
docker exec teddy-mysql sh -c 'mysqldump -uroot -pP@ssw0rd teddynote' \
  | docker exec -i teddy-mysql sh -c 'mysql -uroot -pP@ssw0rd todo'

# 4. 검증
docker exec teddy-mysql sh -c \
  'mysql -uroot -pP@ssw0rd -e "
    SHOW TABLES FROM todo;
    SELECT COUNT(*) AS projects FROM todo.projects;
    SELECT COUNT(*) AS memos FROM todo.memos;
    SELECT COUNT(*) AS versions FROM todo.memo_versions;
  "'

# 5. fastapi 재기동 (DB_NAME=todo 로 붙음)
docker compose up -d --force-recreate fastapi
curl -s http://localhost:8000/api/projects | head

# 6. 검증 OK 확인 후 (며칠 운영해보고) 옛 DB drop
# docker exec teddy-mysql sh -c \
#   'mysql -uroot -pP@ssw0rd -e "DROP DATABASE teddynote"'
```

## 검토 통과 기준 (대원칙 #3)

- `SHOW TABLES FROM todo` 가 `projects`, `memos`, `memo_versions` 세 테이블을 반환.
- 각 테이블 COUNT 가 `teddynote` 의 동일 COUNT 와 정확히 일치.
- `curl http://localhost:8000/api/projects` 가 기존과 동일한 프로젝트 목록 반환.
- 웹 UI (`/todo`) 로그인 후 메모 목록/내용이 손실 없이 보임.
- 메모 버전 히스토리(`/api/memos/{id}/versions`) 가 보존됨.

위 5가지가 모두 통과한 *후에만* Step 6 의 `DROP DATABASE teddynote` 실행. 일정 기간(예: 1주) 보존 후 정리 권장.

## 마이그레이션 고려사항

- **무중단 아님** — fastapi 재기동 시 짧은 다운타임 (수 초). 운영중 트래픽이 없으므로 무시 가능.
- **password 평문 저장 상태 그대로 이관** — auth-api-nest 통합 (oauth-blueprint Phase 3) 까지는 손대지 않는다. 이번 이관 범위 밖.
- **mysql-data 볼륨은 유지** — 새 DB 가 같은 볼륨 안에 만들어지므로 디스크 사용량 잠시 ~2배 (한 데이터셋이 두 DB 에). DROP 후 정상화.
- **NAS 운영 인스턴스** — 같은 절차 필요. NAS 에서 `pull-all.sh` 로 코드는 동기화되지만 DB 이관은 운영자가 직접. 배포 직후 데이터가 비어보이는 사고 방지를 위해 NAS 에서도 *배포 전* 동일 이관 선행.

## 위험 / 열려있는 질문

- **위험**: 검증 단계 건너뛰고 `DROP DATABASE teddynote` 를 일찍 실행하면 복구 불가 (스냅샷 파일은 남지만 운영 중단 발생). → Step 6 을 별도 시점에 수동으로 실행하는 것으로 완화.
- **열린 질문**: NAS 운영 인스턴스의 이관 시점을 언제로 잡을지? 로컬 검증 끝나고 같은 날 배포할지, 별도 정비 시간을 둘지.
