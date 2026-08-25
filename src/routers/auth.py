from fastapi import APIRouter, Depends, Query, Request, Response

try:
    from ..auth_utils import get_current_user, require_admin
    from ..config import feature_flags, TODO_OIDC_CALLBACK_ROUTE_PATH
    from ..models.auth import (
        LiveKitTokenRequest,
        ServiceApplicationRequest,
        SessionOidcStartRequest,
    )
    from ..services.livekit import issue_livekit_token
    from ..services.session_auth import get_session_service
except ImportError:  # pragma: no cover
    from auth_utils import get_current_user, require_admin
    from config import feature_flags, TODO_OIDC_CALLBACK_ROUTE_PATH
    from models.auth import (
        LiveKitTokenRequest,
        ServiceApplicationRequest,
        SessionOidcStartRequest,
    )
    from services.livekit import issue_livekit_token
    from services.session_auth import get_session_service


router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/session/oidc/start")
async def session_oidc_start(body: SessionOidcStartRequest | None = None):
    """todo-web 용 OIDC authorize URL 을 생성한다."""
    return await get_session_service().start_login(body.returnTo if body else None)


async def session_oidc_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
):
    """auth-api callback code 를 todo 세션으로 교환하고 todo-web 로 redirect 한다."""
    return await get_session_service().handle_oidc_callback(
        code=code,
        state=state,
        error=error,
        error_description=error_description,
    )


@router.post("/session/logout")
async def session_logout(request: Request, response: Response):
    """todo 세션을 종료하고 HttpOnly 쿠키를 제거한다."""
    await get_session_service().logout(request, response)
    return {"success": True}


@router.get("/session/me")
async def session_me(request: Request):
    """현재 todo 세션 사용자를 반환한다 (오프라인 세션이면 `offline: true`).

    `features` 는 위치 축에 따라 웹이 노출할 prod/local 전용 표면 목록이다.
    """
    me = await get_session_service().get_user(request)
    return {**me, "features": feature_flags()}


@router.post("/session/local")
async def session_local(request: Request, response: Response):
    """원격 auth 가 닿지 않을 때 캐시된 신원으로 무기한 로컬 세션을 발급한다.

    최초 1회는 반드시 원격 auth 로그인이 필요하고(그때 신원이 캐시된다), loopback 요청만
    허용한다. 노트북 오프라인 스택 전용 경로다 (`TODO_LOCAL_SESSION_ENABLED`).
    """
    return await get_session_service().start_local_session(request, response)


@router.get("/session/local/identity")
async def session_local_identity(request: Request):
    """신뢰된 로컬 클라이언트에 오프라인 로그인 가능 여부만 반환한다."""
    return await get_session_service().get_local_identity_info(request)


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


router.add_api_route(
    TODO_OIDC_CALLBACK_ROUTE_PATH,
    session_oidc_callback,
    methods=["GET"],
)
