from fastapi import APIRouter, Depends, Query, Request, Response

try:
    from ..auth_utils import get_current_user, require_admin
    from ..models.auth import (
        LiveKitTokenRequest,
        ServiceApplicationRequest,
        SessionLoginRequest,
    )
    from ..services.livekit import issue_livekit_token
    from ..services.session_auth import get_session_service
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user, require_admin
    from models.auth import LiveKitTokenRequest, ServiceApplicationRequest, SessionLoginRequest
    from services.livekit import issue_livekit_token
    from services.session_auth import get_session_service


router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/session/login")
async def session_login(data: SessionLoginRequest, response: Response):
    """중앙 auth-api를 사용해 todo 세션을 생성한다."""
    return await get_session_service().login(data.loginId, data.password, response)


@router.post("/session/logout")
async def session_logout(request: Request, response: Response):
    """todo 세션을 종료하고 HttpOnly 쿠키를 제거한다."""
    await get_session_service().logout(request, response)
    return {"success": True}


@router.get("/session/me")
async def session_me(request: Request):
    """현재 todo 세션 사용자를 반환한다."""
    return await get_session_service().get_user(request)


@router.post("/session/service-application")
async def create_service_application(
    request: Request, body: ServiceApplicationRequest
):
    """todo 서비스 접근 신청을 auth-api에 생성한다."""
    return await get_session_service().create_service_application(request, body.message)


@router.get("/users/search")
async def search_users(
    q: str = Query("", min_length=1),
    user: dict = Depends(require_admin),  # noqa: B008
):
    """멤버 초대용 auth-api 계정 검색."""
    _ = user
    return await get_session_service().search_accounts(q)


@router.post("/livekit/token")
async def get_livekit_token(
    body: LiveKitTokenRequest,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """프로젝트 room 참여용 LiveKit 토큰을 발급한다."""
    return issue_livekit_token(body.roomName, user)
