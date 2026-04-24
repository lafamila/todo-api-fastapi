from fastapi import APIRouter, Depends, HTTPException

try:
    from ..auth_utils import (
        _truncate_password,
        create_access_token,
        get_current_user,
        pwd_context,
        verify_password,
    )
    from ..connectors import get_db_connection
    from ..models.base import (
        ChangePasswordRequest,
        LoginRequest,
        RegisterRequest,
    )
    from ..utils import generate_id
except ImportError:  # pragma: no cover
    from auth_utils import (
        _truncate_password,
        create_access_token,
        get_current_user,
        pwd_context,
        verify_password,
    )
    from connectors import get_db_connection
    from models.base import ChangePasswordRequest, LoginRequest, RegisterRequest
    from utils import generate_id


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(data: LoginRequest):
    """로그인"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, password_hash, display_name, is_admin, is_super_admin, is_active FROM users WHERE username = %s",
                (data.username,),
            )
            user = cursor.fetchone()
            if not user or not verify_password(data.password, user["password_hash"]):
                raise HTTPException(
                    status_code=401, detail="Incorrect username or password"
                )
            if not user["is_active"]:
                raise HTTPException(status_code=401, detail="Account is inactive")

            token = create_access_token(
                {
                    "sub": user["id"],
                    "display_name": user["display_name"],
                    "username": user["username"],
                }
            )
            return {
                "accessToken": token,
                "tokenType": "bearer",
                "user": {
                    "id": user["id"],
                    "username": user["username"],
                    "displayName": user["display_name"],
                    "isAdmin": bool(user["is_admin"]),
                    "isSuperAdmin": bool(user.get("is_super_admin")),
                },
            }


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """현재 로그인한 사용자 조회"""
    return {
        "id": user["id"],
        "username": user["username"],
        "displayName": user["display_name"],
        "isAdmin": bool(user["is_admin"]),
        "isSuperAdmin": bool(user.get("is_super_admin")),
    }


@router.post("/register")
async def register(data: RegisterRequest):
    """회원가입"""
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
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

            token = create_access_token(
                {
                    "sub": user_id,
                    "display_name": data.displayName,
                    "username": data.username,
                }
            )
            return {
                "accessToken": token,
                "tokenType": "bearer",
                "user": {
                    "id": user_id,
                    "username": data.username,
                    "displayName": data.displayName,
                    "isAdmin": False,
                    "isSuperAdmin": False,
                },
            }


@router.post("/change-password")
async def change_password(
    data: ChangePasswordRequest, user: dict = Depends(get_current_user)
):
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
                raise HTTPException(
                    status_code=400, detail="기존 비밀번호가 일치하지 않습니다."
                )

            hashed = pwd_context.hash(_truncate_password(data.newPassword))
            cursor.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (hashed, user["id"]),
            )
            return {"message": "비밀번호가 변경되었습니다."}
