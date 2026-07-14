"""FastAPI auth dependencies for v1 user management."""

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError as JWTError

from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal, decode_principal
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

_bearer = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedPrincipal:
    """Decode the Bearer JWT and return its validated identity and tenant claims."""
    try:
        principal = decode_principal(credentials.credentials, settings.secret_key)
    except JWTError:
        _log.warning("auth.jwt_decode_failed", extra={"token_prefix": credentials.credentials[:10] + "..."})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    return principal


async def get_current_tenant_user(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> TenantPrincipal:
    """Require the tenant claims used by organisation-scoped API routes."""
    if current_user.organisation_id is None or current_user.org_role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organisation membership required",
        )
    return TenantPrincipal(
        username=current_user.username,
        organisation_id=current_user.organisation_id,
        account_id=current_user.account_id,
        org_role=current_user.org_role,
        is_system_admin=current_user.is_system_admin,
    )


async def require_system_admin(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> AuthenticatedPrincipal:
    """Require the current user to have system admin privileges."""
    if not current_user.is_system_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System admin role required",
        )
    return current_user
