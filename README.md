# Todo Backend API

Todo 백엔드 API 서버입니다. FastAPI와 MariaDB를 사용하여 구현되었습니다.

## 기술 스택

- **프레임워크**: FastAPI
- **데이터베이스**: MariaDB
- **언어**: Python 3.8+

## 디렉터리 구조

```
todo-api-fastapi/
├── requirements.txt          # 필요 라이브러리
├── .env                      # 환경변수 설정
└── src/
    ├── __main__.py          # FastAPI 메인 코드 (API 엔드포인트)
    └── connectors/
        └── __init__.py      # MariaDB connection 관련 코드
```

## 설치 및 실행

### 1. 필요 라이브러리 설치

```bash
pip install -r requirements.txt
```

### 2. MariaDB 설정

MariaDB가 설치되어 있어야 합니다. `.env` 파일에서 데이터베이스 연결 정보를 설정하세요:

```env
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your_password
DB_NAME=todo
```

### 3. 서버 실행

```bash
python -m src
```

또는

```bash
cd src
python __main__.py
```

서버는 `http://localhost:8000`에서 실행됩니다.

## API 엔드포인트

### Projects API

- `GET /api/projects` - 모든 프로젝트 조회
- `POST /api/projects` - 프로젝트 생성
- `POST /api/projects/{id}/verify` - 프로젝트 비밀번호 검증
- `GET /api/projects/{id}/memos` - 특정 프로젝트의 메모 목록 조회

### Memos API

- `POST /api/memos` - 메모 생성
- `GET /api/memos/{id}` - 메모 조회
- `PUT /api/memos/{id}` - 메모 업데이트

## 데이터베이스 스키마

### projects 테이블

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | VARCHAR(50) | 프로젝트 ID (PK) |
| name | VARCHAR(255) | 프로젝트 이름 |
| icon | VARCHAR(10) | 프로젝트 아이콘 |
| is_secret | BOOLEAN | 비밀 프로젝트 여부 |
| password | VARCHAR(255) | 프로젝트 비밀번호 |
| created_at | DATETIME | 생성 시간 |
| updated_at | DATETIME | 수정 시간 |

### memos 테이블

| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | VARCHAR(50) | 메모 ID (PK) |
| project_id | VARCHAR(50) | 프로젝트 ID (FK) |
| title | VARCHAR(255) | 메모 제목 |
| content | LONGTEXT | 메모 내용 |
| created_at | DATETIME | 생성 시간 |
| updated_at | DATETIME | 수정 시간 |

## API 문서

서버 실행 후 다음 URL에서 Swagger UI를 통해 API 문서를 확인할 수 있습니다:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## CORS 설정

프론트엔드 개발 서버(`http://localhost:3031`)와의 통신을 위해 CORS가 설정되어 있습니다.
다른 origin에서 접근이 필요한 경우 `src/__main__.py`의 CORS 설정을 수정하세요.

## 자동 초기화

서버 시작 시 자동으로:
- 데이터베이스가 존재하지 않으면 생성
- 필요한 테이블이 존재하지 않으면 생성

따라서 처음 실행 시 별도의 마이그레이션 작업이 필요하지 않습니다.
