from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional, List
import uuid
import random
import os

from connectors import get_db_connection, init_db

app = FastAPI(title="Todo API")

SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__truncate_error=False)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _truncate_password(password: str) -> bytes:
    """bcrypt 72바이트 제한 대응 — 바이트 단위로 truncate"""
    return password.encode("utf-8")[:72]


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(_truncate_password(plain_password), hashed_password)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, display_name, is_admin, is_active FROM users WHERE id = %s",
                (user_id,),
            )
            user = cursor.fetchone()
            if not user or not user["is_active"]:
                raise HTTPException(
                    status_code=401, detail="User not found or inactive"
                )
            return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


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


class BulkDeleteMemosRequest(BaseModel):
    memoIds: List[str]


class LoginRequest(BaseModel):
    username: str
    password: str


class InviteMemberRequest(BaseModel):
    userId: str
    role: str = "member"


class RegisterRequest(BaseModel):
    username: str
    password: str
    displayName: str


class UpdateAdminRequest(BaseModel):
    isAdmin: bool


class ChangePasswordRequest(BaseModel):
    oldPassword: str
    newPassword: str

class PublishArticleRequest(BaseModel):
    memoId: str


class Article(BaseModel):
    id: str
    memoId: str
    projectId: str
    title: str
    content: str
    publishedVersion: int
    createdAt: datetime
    publishedAt: datetime
    updatedAt: datetime


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


def check_project_membership(cursor, project_id: str, user: dict):
    if user["is_admin"]:
        cursor.execute(
            "SELECT id FROM projects WHERE id = %s AND owner_id = %s",
            (project_id, user["id"]),
        )
        if cursor.fetchone():
            return True

    cursor.execute(
        "SELECT id FROM project_members WHERE project_id = %s AND user_id = %s",
        (project_id, user["id"]),
    )
    if cursor.fetchone():
        return True

    return False


# ============ Auth API ============


@app.post("/api/auth/login")
async def login(data: LoginRequest):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, password_hash, display_name, is_admin, is_active FROM users WHERE username = %s",
                (data.username,),
            )
            user = cursor.fetchone()
            if not user or not verify_password(data.password, user["password_hash"]):
                raise HTTPException(
                    status_code=401, detail="Incorrect username or password"
                )
            if not user["is_active"]:
                raise HTTPException(status_code=401, detail="Account is inactive")

            token = create_access_token({"sub": user["id"]})
            return {
                "accessToken": token,
                "tokenType": "bearer",
                "user": {
                    "id": user["id"],
                    "username": user["username"],
                    "displayName": user["display_name"],
                    "isAdmin": bool(user["is_admin"]),
                },
            }


@app.get("/api/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "username": user["username"],
        "displayName": user["display_name"],
        "isAdmin": bool(user["is_admin"]),
    }


@app.post("/api/auth/register")
async def register(data: RegisterRequest):
    """회원가입"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            # 중복 username 체크
            cursor.execute("SELECT id FROM users WHERE username = %s", (data.username,))
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail="Username already exists")

            user_id = generate_id()
            hashed = pwd_context.hash(_truncate_password(data.password))
            cursor.execute(
                """
                INSERT INTO users (id, username, password_hash, display_name, is_admin, is_active)
                VALUES (%s, %s, %s, %s, FALSE, TRUE)
            """,
                (user_id, data.username, hashed, data.displayName),
            )

            token = create_access_token({"sub": user_id})
            return {
                "accessToken": token,
                "tokenType": "bearer",
                "user": {
                    "id": user_id,
                    "username": data.username,
                    "displayName": data.displayName,
                    "isAdmin": False,
                },
            }


# ============ Users API ============


@app.get("/api/users/search")
async def search_users(q: str, user: dict = Depends(require_admin)):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, username, display_name, is_admin
                FROM users
                WHERE (username LIKE %s OR display_name LIKE %s) AND is_active = TRUE
                ORDER BY display_name
                LIMIT 20
            """,
                (f"%{q}%", f"%{q}%"),
            )
            users = cursor.fetchall()
            return [
                {
                    "id": u["id"],
                    "username": u["username"],
                    "displayName": u["display_name"],
                    "isAdmin": bool(u["is_admin"]),
                }
                for u in users
            ]


@app.patch("/api/users/{user_id}/admin")
async def update_user_admin(
    user_id: str, data: UpdateAdminRequest, admin: dict = Depends(require_admin)
):
    """사용자 관리자 권한 변경 (관리자만)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, display_name, is_admin FROM users WHERE id = %s AND is_active = TRUE",
                (user_id,),
            )
            user = cursor.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            cursor.execute(
                "UPDATE users SET is_admin = %s WHERE id = %s", (data.isAdmin, user_id)
            )
            return {
                "id": user["id"],
                "username": user["username"],
                "displayName": user["display_name"],
                "isAdmin": bool(data.isAdmin),
            }


@app.post("/api/users/{user_id}/reset-password")
async def reset_user_password(user_id: str, admin: dict = Depends(require_admin)):
    """특정 사용자의 비밀번호를 0000으로 초기화 (관리자만)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, display_name FROM users WHERE id = %s AND is_active = TRUE",
                (user_id,),
            )
            user = cursor.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            hashed = pwd_context.hash(_truncate_password("0000"))
            cursor.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (hashed, user_id),
            )
            return {
                "id": user["id"],
                "username": user["username"],
                "displayName": user["display_name"],
                "message": "비밀번호가 0000으로 초기화되었습니다.",
            }


@app.post("/api/auth/change-password")
async def change_password(data: ChangePasswordRequest, user: dict = Depends(get_current_user)):
    """로그인된 사용자의 비밀번호 변경 (기존 비밀번호 확인 필요)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, password_hash FROM users WHERE id = %s AND is_active = TRUE",
                (user["id"],),
            )
            db_user = cursor.fetchone()
            if not db_user:
                raise HTTPException(status_code=404, detail="User not found")

            if not verify_password(data.oldPassword, db_user["password_hash"]):
                raise HTTPException(status_code=400, detail="기존 비밀번호가 일치하지 않습니다.")

            hashed = pwd_context.hash(_truncate_password(data.newPassword))
            cursor.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (hashed, user["id"]),
            )
            return {"message": "비밀번호가 변경되었습니다."}

# ============ Project Members API ============


@app.post("/api/projects/{project_id}/members", status_code=201)
async def invite_member(
    project_id: str, data: InviteMemberRequest, user: dict = Depends(require_admin)
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, owner_id FROM projects WHERE id = %s", (project_id,)
            )
            project = cursor.fetchone()
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")
            if project["owner_id"] != user["id"]:
                raise HTTPException(
                    status_code=403, detail="Only project owner can invite members"
                )

            cursor.execute(
                "SELECT id FROM users WHERE id = %s AND is_active = TRUE",
                (data.userId,),
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="User not found")

            cursor.execute(
                "SELECT id FROM project_members WHERE project_id = %s AND user_id = %s",
                (project_id, data.userId),
            )
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail="User is already a member")

            member_id = generate_id()
            now = datetime.now()
            cursor.execute(
                "INSERT INTO project_members (id, project_id, user_id, role, invited_at) VALUES (%s, %s, %s, %s, %s)",
                (member_id, project_id, data.userId, data.role, now),
            )
            return {
                "id": member_id,
                "projectId": project_id,
                "userId": data.userId,
                "role": data.role,
                "invitedAt": now.isoformat(),
            }


@app.delete("/api/projects/{project_id}/members/{member_user_id}")
async def remove_member(
    project_id: str, member_user_id: str, user: dict = Depends(require_admin)
):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, owner_id FROM projects WHERE id = %s", (project_id,)
            )
            project = cursor.fetchone()
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")
            if project["owner_id"] != user["id"]:
                raise HTTPException(
                    status_code=403, detail="Only project owner can remove members"
                )

            cursor.execute(
                "DELETE FROM project_members WHERE project_id = %s AND user_id = %s",
                (project_id, member_user_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Member not found")
            return {"message": "Member removed successfully"}


@app.get("/api/projects/{project_id}/members")
async def get_project_members(project_id: str, user: dict = Depends(get_current_user)):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM projects WHERE id = %s", (project_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Project not found")

            if not user["is_admin"]:
                cursor.execute(
                    "SELECT id FROM project_members WHERE project_id = %s AND user_id = %s",
                    (project_id, user["id"]),
                )
                if not cursor.fetchone():
                    raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                """
                SELECT pm.id, pm.project_id, pm.user_id, pm.role, pm.invited_at,
                       u.username, u.display_name, u.is_admin
                FROM project_members pm
                JOIN users u ON pm.user_id = u.id
                WHERE pm.project_id = %s
                ORDER BY pm.invited_at
            """,
                (project_id,),
            )
            members = cursor.fetchall()
            return [
                {
                    "id": m["id"],
                    "projectId": m["project_id"],
                    "userId": m["user_id"],
                    "role": m["role"],
                    "invitedAt": m["invited_at"].isoformat(),
                    "username": m["username"],
                    "displayName": m["display_name"],
                    "isAdmin": bool(m["is_admin"]),
                }
                for m in members
            ]


# ============ Projects API ============


@app.get("/api/projects")
async def get_projects(user: dict = Depends(get_current_user)):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if user["is_admin"]:
                cursor.execute(
                    """
                    SELECT DISTINCT p.id, p.name, p.icon, p.is_secret, p.owner_id, p.created_at, p.updated_at
                    FROM projects p
                    LEFT JOIN project_members pm ON p.id = pm.project_id AND pm.user_id = %s
                    WHERE p.owner_id = %s OR pm.user_id = %s
                    ORDER BY p.created_at DESC
                """,
                    (user["id"], user["id"], user["id"]),
                )
            else:
                cursor.execute(
                    """
                    SELECT p.id, p.name, p.icon, p.is_secret, p.owner_id, p.created_at, p.updated_at
                    FROM projects p
                    JOIN project_members pm ON p.id = pm.project_id
                    WHERE pm.user_id = %s
                    ORDER BY p.created_at DESC
                """,
                    (user["id"],),
                )

            projects = cursor.fetchall()

            return [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "icon": p["icon"],
                    "isSecret": bool(p["is_secret"]),
                    "ownerId": p["owner_id"],
                    "createdAt": p["created_at"].isoformat(),
                    "updatedAt": p["updated_at"].isoformat(),
                }
                for p in projects
            ]


@app.post("/api/projects", status_code=201)
async def create_project(
    data: CreateProjectRequest, user: dict = Depends(require_admin)
):
    """프로젝트 생성"""
    project_id = generate_id()
    member_id = generate_id()
    now = datetime.now()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO projects (id, owner_id, name, icon, is_secret, password, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    project_id,
                    user["id"],
                    data.name,
                    data.icon,
                    data.isSecret,
                    data.password,
                    now,
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO project_members (id, project_id, user_id, role, invited_at)
                VALUES (%s, %s, %s, %s, %s)
            """,
                (member_id, project_id, user["id"], "owner", now),
            )

    return {
        "id": project_id,
        "name": data.name,
        "icon": data.icon,
        "isSecret": data.isSecret,
        "ownerId": user["id"],
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
    }


@app.post("/api/projects/{project_id}/verify")
async def verify_project_password(
    project_id: str, data: VerifyPasswordRequest, user: dict = Depends(get_current_user)
):
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
async def get_project_memos(project_id: str, user: dict = Depends(get_current_user)):
    """특정 프로젝트의 메모 목록 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM projects WHERE id = %s", (project_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Project not found")

            if not check_project_membership(cursor, project_id, user):
                raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                """
                SELECT id, project_id, created_by, title, content, created_at, updated_at
                FROM memos
                WHERE project_id = %s AND deleted_at IS NULL
                ORDER BY created_at DESC
            """,
                (project_id,),
            )
            memos = cursor.fetchall()

            return [
                {
                    "id": m["id"],
                    "projectId": m["project_id"],
                    "createdBy": m["created_by"],
                    "title": m["title"],
                    "content": m["content"],
                    "createdAt": m["created_at"].isoformat(),
                    "updatedAt": m["updated_at"].isoformat(),
                }
                for m in memos
            ]


# ============ Memos API ============


@app.post("/api/memos", status_code=201)
async def create_memo(data: CreateMemoRequest, user: dict = Depends(get_current_user)):
    """메모 생성"""
    memo_id = generate_id()
    now = datetime.now()

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM projects WHERE id = %s", (data.projectId,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Project not found")

            if not check_project_membership(cursor, data.projectId, user):
                raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                "SELECT id FROM memos WHERE project_id = %s AND title = %s AND deleted_at IS NULL LIMIT 1",
                (data.projectId, data.title),
            )
            existing_memo = cursor.fetchone()
            if existing_memo:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "동일한 제목의 메모가 이미 존재합니다.",
                        "existingMemoId": existing_memo["id"],
                    },
                )

            cursor.execute(
                """
                INSERT INTO memos (id, project_id, created_by, title, content, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
                (memo_id, data.projectId, user["id"], data.title, "", now, now),
            )

    return {
        "id": memo_id,
        "projectId": data.projectId,
        "createdBy": user["id"],
        "title": data.title,
        "content": "",
        "createdAt": now.isoformat(),
        "updatedAt": now.isoformat(),
    }


@app.get("/api/memos/{memo_id}")
async def get_memo(memo_id: str, user: dict = Depends(get_current_user)):
    """메모 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, project_id, created_by, title, content, created_at, updated_at
                FROM memos
                WHERE id = %s AND deleted_at IS NULL
            """,
                (memo_id,),
            )
            memo = cursor.fetchone()

            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not check_project_membership(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            return {
                "id": memo["id"],
                "projectId": memo["project_id"],
                "createdBy": memo["created_by"],
                "title": memo["title"],
                "content": memo["content"],
                "createdAt": memo["created_at"].isoformat(),
                "updatedAt": memo["updated_at"].isoformat(),
            }


@app.put("/api/memos/{memo_id}")
async def update_memo(
    memo_id: str, data: UpdateMemoRequest, user: dict = Depends(get_current_user)
):
    """메모 업데이트 (버전 히스토리 저장)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM memos WHERE id = %s AND deleted_at IS NULL", (memo_id,)
            )
            memo = cursor.fetchone()

            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not check_project_membership(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            if memo["content"]:
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

            cursor.execute(
                """
                UPDATE memos
                SET content = %s, updated_at = %s
                WHERE id = %s
            """,
                (data.content, datetime.now(), memo_id),
            )

            cursor.execute(
                """
                SELECT id, project_id, created_by, title, content, created_at, updated_at
                FROM memos
                WHERE id = %s AND deleted_at IS NULL
            """,
                (memo_id,),
            )
            updated_memo = cursor.fetchone()

            return {
                "id": updated_memo["id"],
                "projectId": updated_memo["project_id"],
                "createdBy": updated_memo["created_by"],
                "title": updated_memo["title"],
                "content": updated_memo["content"],
                "createdAt": updated_memo["created_at"].isoformat(),
                "updatedAt": updated_memo["updated_at"].isoformat(),
            }


@app.get("/api/memos/{memo_id}/versions")
async def get_memo_versions(memo_id: str, user: dict = Depends(get_current_user)):
    """메모의 버전 히스토리 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, project_id FROM memos WHERE id = %s AND deleted_at IS NULL",
                (memo_id,),
            )
            memo = cursor.fetchone()
            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not check_project_membership(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

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
async def get_memo_version(
    memo_id: str, version: int, user: dict = Depends(get_current_user)
):
    """특정 버전의 메모 내용 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, project_id FROM memos WHERE id = %s AND deleted_at IS NULL",
                (memo_id,),
            )
            memo = cursor.fetchone()
            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not check_project_membership(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

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


@app.delete("/api/memos/{memo_id}")
async def delete_memo(memo_id: str, user: dict = Depends(get_current_user)):
    """메모 소프트 삭제"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.id, m.project_id, p.owner_id
                FROM memos m
                JOIN projects p ON m.project_id = p.id
                WHERE m.id = %s AND m.deleted_at IS NULL
            """,
                (memo_id,),
            )
            memo = cursor.fetchone()
            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not user["is_admin"] or memo["owner_id"] != user["id"]:
                raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                "UPDATE memos SET deleted_at = %s WHERE id = %s",
                (datetime.now(), memo_id),
            )
            return {"message": "Memo soft-deleted successfully"}


@app.post("/api/memos/bulk-delete")
async def bulk_delete_memos(
    data: BulkDeleteMemosRequest, user: dict = Depends(require_admin)
):
    """메모 일괄 소프트 삭제"""
    if not data.memoIds:
        raise HTTPException(status_code=400, detail="memoIds cannot be empty")

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            now = datetime.now()
            deleted_count = 0
            for memo_id in data.memoIds:
                cursor.execute(
                    "UPDATE memos SET deleted_at = %s WHERE id = %s AND deleted_at IS NULL",
                    (now, memo_id),
                )
                deleted_count += cursor.rowcount

            return {
                "message": f"{deleted_count}개의 메모가 삭제되었습니다.",
                "deletedCount": deleted_count,
            }


# ============ Articles API ============


@app.post("/api/articles", status_code=201)
async def publish_article(
    data: PublishArticleRequest, user: dict = Depends(require_admin)
):
    """메모를 게시글로 발행 (현재 내용 스냅샷)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.id, m.project_id, m.title, m.content, m.created_at, p.owner_id,
                       COALESCE(MAX(mv.version), 0) + 1 as current_version
                FROM memos m
                JOIN projects p ON m.project_id = p.id
                LEFT JOIN memo_versions mv ON m.id = mv.memo_id
                WHERE m.id = %s AND m.deleted_at IS NULL
                GROUP BY m.id
            """,
                (data.memoId,),
            )
            memo = cursor.fetchone()

            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if memo["owner_id"] != user["id"]:
                raise HTTPException(
                    status_code=403, detail="Only project owner can publish article"
                )

            if not memo["content"]:
                raise HTTPException(status_code=400, detail="Cannot publish empty memo")

            cursor.execute("SELECT id FROM articles WHERE memo_id = %s", (data.memoId,))
            existing = cursor.fetchone()

            now = datetime.now()

            if existing:
                cursor.execute(
                    """
                    UPDATE articles
                    SET title = %s, content = %s, published_version = %s,
                        published_at = %s, updated_at = %s
                    WHERE memo_id = %s
                """,
                    (
                        memo["title"],
                        memo["content"],
                        memo["current_version"],
                        now,
                        now,
                        data.memoId,
                    ),
                )
                article_id = existing["id"]
            else:
                article_id = generate_id()
                cursor.execute(
                    """
                    INSERT INTO articles (id, memo_id, project_id, title, content, published_version, created_at, published_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        article_id,
                        data.memoId,
                        memo["project_id"],
                        memo["title"],
                        memo["content"],
                        memo["current_version"],
                        memo["created_at"],
                        now,
                        now,
                    ),
                )

            cursor.execute(
                """
                SELECT a.id, a.memo_id, a.project_id, a.title, a.content,
                       a.published_version, a.created_at, a.published_at, a.updated_at,
                       p.name as project_name, p.icon as project_icon, p.is_secret
                FROM articles a
                JOIN projects p ON a.project_id = p.id
                WHERE a.id = %s
            """,
                (article_id,),
            )
            article = cursor.fetchone()

            return {
                "id": article["id"],
                "memoId": article["memo_id"],
                "projectId": article["project_id"],
                "title": article["title"],
                "content": article["content"],
                "publishedVersion": article["published_version"],
                "createdAt": article["created_at"].isoformat(),
                "publishedAt": article["published_at"].isoformat(),
                "updatedAt": article["updated_at"].isoformat(),
                "projectName": article["project_name"],
                "projectIcon": article["project_icon"],
                "isSecret": bool(article["is_secret"]),
            }


@app.get("/api/articles")
async def get_articles(projectId: Optional[str] = None):
    """게시글 목록 조회 (프로젝트별 필터 가능)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if projectId:
                cursor.execute(
                    """
                    SELECT a.id, a.memo_id, a.project_id, a.title,
                           a.published_version, a.created_at, a.published_at, a.updated_at,
                           p.name as project_name, p.icon as project_icon, p.is_secret
                    FROM articles a
                    JOIN projects p ON a.project_id = p.id
                    WHERE a.project_id = %s
                    ORDER BY a.published_at DESC
                """,
                    (projectId,),
                )
            else:
                cursor.execute(
                    """
                    SELECT a.id, a.memo_id, a.project_id, a.title,
                           a.published_version, a.created_at, a.published_at, a.updated_at,
                           p.name as project_name, p.icon as project_icon, p.is_secret
                    FROM articles a
                    JOIN projects p ON a.project_id = p.id
                    ORDER BY a.published_at DESC
                """
                )
            articles = cursor.fetchall()

            return [
                {
                    "id": a["id"],
                    "memoId": a["memo_id"],
                    "projectId": a["project_id"],
                    "title": a["title"],
                    "publishedVersion": a["published_version"],
                    "createdAt": a["created_at"].isoformat(),
                    "publishedAt": a["published_at"].isoformat(),
                    "updatedAt": a["updated_at"].isoformat(),
                    "projectName": a["project_name"],
                    "projectIcon": a["project_icon"],
                    "isSecret": bool(a["is_secret"]),
                }
                for a in articles
            ]


@app.get("/api/articles/{article_id}")
async def get_article(article_id: str):
    """게시글 상세 조회 (비밀 프로젝트 콘텐츠 제외)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.id, a.memo_id, a.project_id, a.title, a.content,
                       a.published_version, a.created_at, a.published_at, a.updated_at,
                       p.name as project_name, p.icon as project_icon, p.is_secret
                FROM articles a
                JOIN projects p ON a.project_id = p.id
                WHERE a.id = %s
            """,
                (article_id,),
            )
            article = cursor.fetchone()

            if not article:
                raise HTTPException(status_code=404, detail="Article not found")

            return {
                "id": article["id"],
                "memoId": article["memo_id"],
                "projectId": article["project_id"],
                "title": article["title"],
                "content": article["content"],
                "publishedVersion": article["published_version"],
                "createdAt": article["created_at"].isoformat(),
                "publishedAt": article["published_at"].isoformat(),
                "updatedAt": article["updated_at"].isoformat(),
                "projectName": article["project_name"],
                "projectIcon": article["project_icon"],
                "isSecret": bool(article["is_secret"]),
            }


@app.delete("/api/articles/{article_id}")
async def delete_article(article_id: str, user: dict = Depends(require_admin)):
    """게시글 삭제 (게시 취소)"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.id, p.owner_id
                FROM articles a
                JOIN projects p ON a.project_id = p.id
                WHERE a.id = %s
            """,
                (article_id,),
            )
            article = cursor.fetchone()
            if not article:
                raise HTTPException(status_code=404, detail="Article not found")

            if article["owner_id"] != user["id"]:
                raise HTTPException(
                    status_code=403, detail="Only project owner can delete article"
                )

            cursor.execute("DELETE FROM articles WHERE id = %s", (article_id,))
            return {"message": "Article unpublished successfully"}


@app.get("/api/memos/{memo_id}/article")
async def get_memo_article(memo_id: str, user: dict = Depends(get_current_user)):
    """특정 메모의 게시 상태 조회"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, project_id FROM memos WHERE id = %s AND deleted_at IS NULL",
                (memo_id,),
            )
            memo = cursor.fetchone()
            if not memo:
                raise HTTPException(status_code=404, detail="Memo not found")

            if not check_project_membership(cursor, memo["project_id"], user):
                raise HTTPException(status_code=403, detail="Access denied")

            cursor.execute(
                """
                SELECT a.id, a.memo_id, a.project_id, a.title, a.content,
                       a.published_version, a.created_at, a.published_at, a.updated_at
                FROM articles a
                WHERE a.memo_id = %s
            """,
                (memo_id,),
            )
            article = cursor.fetchone()

            if not article:
                return None

            return {
                "id": article["id"],
                "memoId": article["memo_id"],
                "projectId": article["project_id"],
                "title": article["title"],
                "content": article["content"],
                "publishedVersion": article["published_version"],
                "createdAt": article["created_at"].isoformat(),
                "publishedAt": article["published_at"].isoformat(),
                "updatedAt": article["updated_at"].isoformat(),
            }


@app.on_event("startup")
async def startup_event():
    """서버 시작 시 데이터베이스 초기화"""
    init_db()
    print("Todo API Server started successfully")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)
