import os
import re
from time import time
from urllib.request import urlopen

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt


AUTH_ISSUER_URL = os.getenv("AUTH_ISSUER_URL", "http://localhost:3032")
AUTH_JWKS_URL = os.getenv("AUTH_JWKS_URL", f"{AUTH_ISSUER_URL}/oauth/jwks")
AUTH_AUDIENCE = os.getenv("AUTH_AUDIENCE", "service:todo")
SERVICE_CLAIM = "https://lafamila.xyz/claims/service"
JWKS_CACHE_SECONDS = int(os.getenv("AUTH_JWKS_CACHE_SECONDS", "300"))
SERVICE_PERMISSIONS = {"owner", "admin", "user", "visitor"}
ADMIN_PERMISSIONS = {"owner", "admin"}

_jwks_cache: dict | None = None
_jwks_cache_expires_at = 0.0

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/oauth/token", auto_error=False)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = _decode_auth_api_token(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    service_claim = payload.get(SERVICE_CLAIM) or {}
    if service_claim.get("key") != "todo":
        raise HTTPException(status_code=403, detail="Invalid service permission")

    permission = service_claim.get("permission", "visitor")
    if permission not in SERVICE_PERMISSIONS:
        raise HTTPException(status_code=403, detail="Invalid service permission")
    if permission == "visitor":
        raise HTTPException(status_code=403, detail="Todo access approval required")

    account_id = payload.get("sub")
    if not account_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    username = payload.get("preferred_username") or payload.get("email") or account_id
    display_name = payload.get("name") or payload.get("preferred_username") or account_id

    return {
        "id": account_id,
        "account_id": account_id,
        "username": username,
        "display_name": display_name,
        "email": payload.get("email"),
        "permission": permission,
        "slug": _slugify(username),
        "is_admin": permission in ADMIN_PERMISSIONS,
        "is_super_admin": permission == "owner",
        "is_active": True,
    }


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("permission") not in ADMIN_PERMISSIONS:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_super_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("permission") != "owner":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return user


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "user"


def _decode_auth_api_token(token: str) -> dict:
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    key = _find_jwk(kid)
    if key is None:
        _clear_jwks_cache()
        key = _find_jwk(kid)
    if key is None:
        raise JWTError("Signing key not found")
    return jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        issuer=AUTH_ISSUER_URL,
        audience=AUTH_AUDIENCE,
        options={"verify_at_hash": False},
    )


def _find_jwk(kid: str | None) -> dict | None:
    jwks = _get_jwks()
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def _get_jwks() -> dict:
    global _jwks_cache, _jwks_cache_expires_at
    now = time()
    if _jwks_cache and _jwks_cache_expires_at > now:
        return _jwks_cache
    with urlopen(AUTH_JWKS_URL, timeout=5) as response:
        raw = response.read().decode("utf-8")
    import json

    _jwks_cache = json.loads(raw)
    _jwks_cache_expires_at = now + JWKS_CACHE_SECONDS
    return _jwks_cache


def _clear_jwks_cache() -> None:
    global _jwks_cache, _jwks_cache_expires_at
    _jwks_cache = None
    _jwks_cache_expires_at = 0.0
