from fastapi import APIRouter, Depends, HTTPException

try:
    from ..auth_utils import _truncate_password, pwd_context, require_admin
    from ..connectors import get_db_connection
    from ..models.base import UpdateAdminRequest
except ImportError:  # pragma: no cover
    from auth_utils import _truncate_password, pwd_context, require_admin
    from connectors import get_db_connection
    from models.base import UpdateAdminRequest


router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/search")
async def search_users(q: str, user: dict = Depends(require_admin)):
    """사용자 검색"""
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


@router.patch("/{user_id}/admin")
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
                "UPDATE users SET is_admin = %s WHERE id = %s",
                (data.isAdmin, user_id),
            )
            return {
                "id": user["id"],
                "username": user["username"],
                "displayName": user["display_name"],
                "isAdmin": bool(data.isAdmin),
            }


@router.post("/{user_id}/reset-password")
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
