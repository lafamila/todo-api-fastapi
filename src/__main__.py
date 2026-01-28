from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import uuid
import random

from connectors import get_db_connection, init_db

app = FastAPI(title="Todo API")

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic 모델
class CreateProjectRequest(BaseModel):
    name: str
    icon: str
    isSecret: bool
    password: Optional[str] = None


class CreateMemoRequest(BaseModel):
    projectId: str
    title: str


class UpdateMemoRequest(BaseModel):
    content: str


class VerifyPasswordRequest(BaseModel):
    password: str


class Project(BaseModel):
    id: str
    name: str
    icon: str
    isSecret: bool
    createdAt: datetime
    updatedAt: datetime


class Memo(BaseModel):
    id: str
    projectId: str
    title: str
    content: str
    createdAt: datetime
    updatedAt: datetime


def generate_id():
    """UUID 기반 ID 생성"""
    return str(uuid.uuid4())


# ============ Projects API ============


@app.get("/api/projects")
async def get_projects():
    """모든 프로젝트 조회 (password 제외)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, icon, is_secret, created_at, updated_at
                FROM projects
                ORDER BY created_at DESC
            """
            )
            projects = cursor.fetchall()

            return [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "icon": p["icon"],
                    "isSecret": bool(p["is_secret"]),
                    "createdAt": p["created_at"].isoformat(),
                    "updatedAt": p["updated_at"].isoformat(),
                }
                for p in projects
            ]


@app.post("/api/projects", status_code=201)
async def create_project(data: CreateProjectRequest):
    """프로젝트 생성"""
    project_id = generate_id()
    now = datetime.now()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO projects (id, name, icon, is_secret, password, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    project_id,
                    data.name,
                    data.icon,
                    data.isSecret,
                    data.password,
                    now,
                    now,
                ),
            )

    return {
        "id": project_id,
        "name": data.name,
        "icon": data.icon,
        "isSecret": data.isSecret,
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
    }


@app.post("/api/projects/{project_id}/verify")
async def verify_project_password(project_id: str, data: VerifyPasswordRequest):
    """프로젝트 비밀번호 검증"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT password FROM projects WHERE id = %s", (project_id,))
            project = cursor.fetchone()

            if not project:
                raise HTTPException(status_code=404, detail="Project not found")

            verified = project["password"] == data.password
            return {"verified": verified}


@app.get("/api/projects/{project_id}/memos")
async def get_project_memos(project_id: str):
    """특정 프로젝트의 메모 목록 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # 프로젝트 존재 확인
            cursor.execute("SELECT id FROM projects WHERE id = %s", (project_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Project not found")

            # 메모 조회
            cursor.execute(
                """
                SELECT id, project_id, title, content, created_at, updated_at
                FROM memos
                WHERE project_id = %s
                ORDER BY created_at DESC
            """,
                (project_id,),
            )
            memos = cursor.fetchall()

            return [
                {
                    "id": m["id"],
                    "projectId": m["project_id"],
                    "title": m["title"],
                    "content": m["content"],
                    "createdAt": m["created_at"].isoformat(),
                    "updatedAt": m["updated_at"].isoformat(),
                }
                for m in memos
            ]


# ============ Memos API ============


@app.post("/api/memos", status_code=201)
async def create_memo(data: CreateMemoRequest):
    """메모 생성"""
    memo_id = generate_id()
    now = datetime.now()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # 프로젝트 존재 확인
            cursor.execute("SELECT id FROM projects WHERE id = %s", (data.projectId,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Project not found")

            # 메모 생성
            cursor.execute(
                """
                INSERT INTO memos (id, project_id, title, content, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """,
                (memo_id, data.projectId, data.title, "", now, now),
            )

    return {
        "id": memo_id,
        "projectId": data.projectId,
        "title": data.title,
        "content": "",
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
    }


@app.get("/api/memos/{memo_id}")
async def get_memo(memo_id: str):
    """메모 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, project_id, title, content, created_at, updated_at
                FROM memos
                WHERE id = %s
            """,
                (memo_id,),
            )
            memo = cursor.fetchone()

            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            return {
                "id": memo["id"],
                "projectId": memo["project_id"],
                "title": memo["title"],
                "content": memo["content"],
                "createdAt": memo["created_at"].isoformat(),
                "updatedAt": memo["updated_at"].isoformat(),
            }


@app.put("/api/memos/{memo_id}")
async def update_memo(memo_id: str, data: UpdateMemoRequest):
    """메모 업데이트 (버전 히스토리 저장)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # 메모 존재 확인 및 현재 내용 조회
            cursor.execute("SELECT * FROM memos WHERE id = %s", (memo_id,))
            memo = cursor.fetchone()

            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            # 현재 내용이 비어있지 않으면 버전 히스토리에 저장
            if memo["content"]:
                # 현재 최대 버전 번호 조회
                cursor.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) as max_version
                    FROM memo_versions
                    WHERE memo_id = %s
                """,
                    (memo_id,),
                )
                result = cursor.fetchone()
                next_version = result["max_version"] + 1

                # 버전 히스토리 저장
                version_id = generate_id()
                cursor.execute(
                    """
                    INSERT INTO memo_versions (id, memo_id, content, version, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                """,
                    (
                        version_id,
                        memo_id,
                        memo["content"],
                        next_version,
                        datetime.now(),
                    ),
                )

            # 메모 업데이트
            cursor.execute(
                """
                UPDATE memos
                SET content = %s, updated_at = %s
                WHERE id = %s
            """,
                (data.content, datetime.now(), memo_id),
            )

            # 업데이트된 메모 조회
            cursor.execute(
                """
                SELECT id, project_id, title, content, created_at, updated_at
                FROM memos
                WHERE id = %s
            """,
                (memo_id,),
            )
            updated_memo = cursor.fetchone()

            return {
                "id": updated_memo["id"],
                "projectId": updated_memo["project_id"],
                "title": updated_memo["title"],
                "content": updated_memo["content"],
                "createdAt": updated_memo["created_at"].isoformat(),
                "updatedAt": updated_memo["updated_at"].isoformat(),
            }


@app.get("/api/memos/{memo_id}/versions")
async def get_memo_versions(memo_id: str):
    """메모의 버전 히스토리 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # 메모 존재 확인
            cursor.execute("SELECT id FROM memos WHERE id = %s", (memo_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Memo not found")

            # 버전 히스토리 조회 (최신순)
            cursor.execute(
                """
                SELECT id, memo_id, content, version, created_at
                FROM memo_versions
                WHERE memo_id = %s
                ORDER BY version DESC
            """,
                (memo_id,),
            )
            versions = cursor.fetchall()

            return [
                {
                    "id": v["id"],
                    "memoId": v["memo_id"],
                    "content": v["content"],
                    "version": v["version"],
                    "createdAt": v["created_at"].isoformat(),
                }
                for v in versions
            ]


@app.get("/api/memos/{memo_id}/versions/{version}")
async def get_memo_version(memo_id: str, version: int):
    """특정 버전의 메모 내용 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # 메모 존재 확인
            cursor.execute("SELECT id FROM memos WHERE id = %s", (memo_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Memo not found")

            # 특정 버전 조회
            cursor.execute(
                """
                SELECT id, memo_id, content, version, created_at
                FROM memo_versions
                WHERE memo_id = %s AND version = %s
            """,
                (memo_id, version),
            )
            version_data = cursor.fetchone()

            if not version_data:
                raise HTTPException(status_code=404, detail="Version not found")

            return {
                "id": version_data["id"],
                "memoId": version_data["memo_id"],
                "content": version_data["content"],
                "version": version_data["version"],
                "createdAt": version_data["created_at"].isoformat(),
            }


@app.on_event("startup")
async def startup_event():
    """서버 시작 시 데이터베이스 초기화"""
    init_db()
    print("Todo API Server started successfully")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
