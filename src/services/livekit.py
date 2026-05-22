from fastapi import HTTPException

try:
    from ..config import LIVEKIT_API_KEY, LIVEKIT_API_SECRET
except ImportError:  # pragma: no cover
    from config import LIVEKIT_API_KEY, LIVEKIT_API_SECRET


def issue_livekit_token(room_name: str, user: dict) -> dict:
    if not room_name.startswith("project:"):
        raise HTTPException(status_code=400, detail="Invalid room name")
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(status_code=500, detail="LiveKit is not configured")

    try:
        from livekit import api
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(
            status_code=500,
            detail="livekit-api dependency is not installed",
        ) from exc

    token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    token = token.with_identity(user["id"])
    token = token.with_name(user.get("display_name") or user.get("username") or user["id"])
    token = token.with_grants(
        api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        )
    )
    return {"token": token.to_jwt()}
