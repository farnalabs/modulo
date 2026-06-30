"""FastAPI auth dependencies for v1 user management."""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

from modulo.auth.jwt import AuthenticatedPrincipal, decode_principal
from modulo.settings import Settings, get_settings

_bearer = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedPrincipal:
    """Decode the Bearer JWT and return its validated identity and tenant claims."""
    try:
        principal = decode_principal(credentials.credentials, settings.secret_key)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    return principal


async def require_system_admin(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> AuthenticatedPrincipal:
    if not current_user.is_system_admin:
        raise HTTPException(status_code=403, detail="System admin role required")
    return current_user
