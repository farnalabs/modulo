"""FastAPI auth dependencies for v1 user management."""

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError as JWTError

from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal, decode_principal
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

_bearer = HTTPBearer()


async def get_current_tenant_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    settings: Settings = Depends(get_settings),
) -> TenantPrincipal | None:
    """Like get_current_tenant_user but returns None instead of 401 when no credentials."""
    if credentials is None:
        return None
    try:
        from modulo.auth.jwt import decode_principal

        principal = decode_principal(credentials.credentials, settings.secret_key)
        if principal.organisation_id is None or principal.org_role is None:
            return None
        return TenantPrincipal(
            username=principal.username,
            organisation_id=principal.organisation_id,
            account_id=principal.account_id,
            org_role=principal.org_role,
            is_system_admin=principal.is_system_admin,
        )
    except Exception:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedPrincipal:
    """Decode the Bearer JWT and return its validated identity and tenant claims."""
    try:
        principal = decode_principal(credentials.credentials, settings.secret_key)
    except JWTError as exc:
        _log.warning("auth.jwt_decode_failed", extra={"token_prefix": credentials.credentials[:10] + "...", "error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return principal


async def get_current_tenant_user(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> TenantPrincipal:
    """Require the tenant claims used by organisation-scoped API routes.

    Also verifies the account and organisation still exist in the database.
    Catches stale JWTs from deleted accounts/orgs — returns 401 with a clear
    message instead of letting them surface as confusing 409 FK violations.
    """
    if current_user.organisation_id is None or current_user.org_role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organisation membership required",
        )

    await _verify_identity(current_user)

    return TenantPrincipal(
        username=current_user.username,
        organisation_id=current_user.organisation_id,
        account_id=current_user.account_id,
        org_role=current_user.org_role,
        is_system_admin=current_user.is_system_admin,
    )


async def _verify_identity(principal: AuthenticatedPrincipal) -> None:
    """Verify the JWT's account and organisation still exist in the database.

    Uses lazy imports to avoid a circular dependency:
    ``auth.dependencies → api.dependencies → auth.dependencies``.

    Silently fails on DB errors (connection issues, unit tests without a DB)
    so that a transient DB blip never blocks the request — the caller will
    still get a proper error from the actual DB operation.
    """
    try:
        from sqlalchemy import text as _text

        from modulo.api.dependencies import (
            get_or_create_engine,
            get_or_create_session_factory,
        )
        from modulo.settings import get_settings as _get_settings

        engine = get_or_create_engine(_get_settings())
        factory = get_or_create_session_factory(engine)
        async with factory() as session, session.begin():
            result = await session.execute(
                _text("SELECT 1 FROM accounts WHERE id = :aid"),
                {"aid": principal.account_id},
            )
            if result.scalar_one_or_none() is None:
                _log.warning(
                    "auth.account_not_found",
                    extra={
                        "account_id": str(principal.account_id),
                        "username": principal.username,
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Account not found. Please log in again.",
                )

            result = await session.execute(
                _text("SELECT 1 FROM organisations WHERE id = :oid"),
                {"oid": principal.organisation_id},
            )
            if result.scalar_one_or_none() is None:
                _log.warning(
                    "auth.org_not_found",
                    extra={
                        "org_id": str(principal.organisation_id),
                        "username": principal.username,
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Organisation not found. Please log in again.",
                )
    except HTTPException:
        raise
    except Exception:
        _log.warning("auth.identity_verify_failed", exc_info=True)


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