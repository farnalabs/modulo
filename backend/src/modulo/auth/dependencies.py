"""FastAPI auth dependencies for v1 user management."""

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError as JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal, decode_principal
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

_bearer = HTTPBearer()
_bearer_optional = HTTPBearer(auto_error=False)


class InvalidToken(HTTPException):
    def __init__(self, detail: str = "Invalid or expired token") -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


class OrganisationMembershipRequired(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organisation membership required",
        )


class AccountNotFound(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account not found. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class OrganisationNotFound(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organisation not found. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


class OrganisationMembershipNotFound(HTTPException):
    """401 for a principal with no active membership (removed/deactivated).
    ADR 017: a user removed from the org loses access immediately - the JWT
    claim alone is not sufficient.
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organisation membership required",
        )


class SystemAdminRequired(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System admin role required",
        )


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
    except JWTError:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedPrincipal:
    """Decode the Bearer JWT and return its validated identity and tenant claims."""
    try:
        principal = decode_principal(credentials.credentials, settings.secret_key)
    except JWTError as exc:
        _log.warning(
            "auth.jwt_decode_failed",
            extra={"token_prefix": credentials.credentials[:10] + "...", "error": str(exc)},
        )
        raise InvalidToken() from exc

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
        raise OrganisationMembershipRequired()

    live_role = await _verify_identity(current_user)

    return TenantPrincipal(
        username=current_user.username,
        organisation_id=current_user.organisation_id,
        account_id=current_user.account_id,
        # _verify_identity returns the LIVE role; when it returns None the
        # caller's identity was verified but no live role could be read
        # (e.g. the test harness patches it) - fall back to the claim role.
        # In production the DB read either returns the live role or raises
        # (401 missing membership / 503 on SQLAlchemyError), so the claim
        # fallback is only reachable when the read is explicitly stubbed.
        org_role=live_role if live_role is not None else current_user.org_role,
        is_system_admin=current_user.is_system_admin,
    )


async def get_current_tenant_user_or_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
    settings: Settings = Depends(get_settings),
) -> TenantPrincipal:
    """Tenant principal from either a user JWT or an org API key (``mk_``).

    API keys are the documented credential for CI/CD and external agents
    (PRD §9.3 / §5.2): ``runner`` keys can trigger runs and call read
    endpoints; ``operator`` keys are reserved for future HITL-approval
    wiring. This dependency is only wired into ``trigger_run`` and
    ``get_run_status`` — the HITL-approval routes (``observe_run_node``,
    ``recover_run_node``) and other write endpoints still require a user
    JWT, so API keys cannot approve gates or modify pipelines.

    Note that team-scoped keys (``team_id`` set) behave like org-wide keys
    here: ``TenantPrincipal`` carries no team info and these routes do not
    call ``set_rls_user_context``, so team restriction is not enforced for
    run trigger/read — consistent with the existing user-JWT behaviour.

    API keys are resolved with an RLS-disabled prefix lookup (the key's org is
    unknown until the record is read) and re-validated inside the key's org
    context — mirroring the MCP middleware, since ``org_api_keys`` has RLS
    enabled. For JWT credentials the behaviour is identical to
    :func:`get_current_tenant_user`.
    """
    if credentials is None:
        raise InvalidToken()

    token = credentials.credentials
    if token.startswith("mk_"):
        from sqlalchemy import select, text
        from sqlalchemy.exc import SQLAlchemyError

        from modulo.api.dependencies import (
            get_or_create_engine,
            get_or_create_session_factory,
        )
        from modulo.auth.api_key import (
            _MK_PREFIX,
            _PREFIX_LEN,
            ApiKeyInvalidError,
            validate_api_key,
        )
        from modulo.db.models.api_key import OrgApiKey
        from modulo.db.rls import _ensure_active_transaction, set_rls_org

        engine = get_or_create_engine(settings)
        factory = get_or_create_session_factory(engine)
        try:
            # org_api_keys has RLS enabled (migration 0005, _STRICT_RLS) and the
            # key's org is unknown until the record is read — a plain lookup in
            # an empty org context would be filtered out by RLS and reject every
            # valid key. Resolve the record with RLS disabled, then re-validate
            # inside the key's org context before trusting it.
            prefix = token[len(_MK_PREFIX) :][:_PREFIX_LEN]
            async with factory() as session, session.begin():
                dialect = await _ensure_active_transaction(session)
                if dialect == "postgresql":
                    await session.execute(text("SET LOCAL row_security TO OFF"))
                result = await session.execute(
                    select(OrgApiKey).where(
                        OrgApiKey.lookup_prefix == prefix,
                        OrgApiKey.revoked_at.is_(None),
                    )
                )
                key_record = result.scalar_one_or_none()
            if key_record is None:
                raise ApiKeyInvalidError()
            async with factory() as session, session.begin():
                await set_rls_org(session, key_record.organisation_id)
                key = await validate_api_key(session, token, org_id=key_record.organisation_id)
        except ApiKeyInvalidError:
            raise InvalidToken() from None
        except SQLAlchemyError:
            _log.warning("auth.api_key_verify_failed", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database temporarily unavailable.",
            ) from None
        # Role is guaranteed to be "runner"/"operator" by the
        # ck_org_api_keys_role DB constraint; no runtime check needed.
        return TenantPrincipal(
            username=key.name,
            organisation_id=key.organisation_id,
            account_id=key.account_id,
            org_role=key.role,
            is_system_admin=False,
        )

    try:
        principal = decode_principal(token, settings.secret_key)
    except JWTError as exc:
        _log.warning(
            "auth.jwt_decode_failed",
            extra={"token_prefix": token[:10] + "...", "error": str(exc)},
        )
        raise InvalidToken() from exc

    return await get_current_tenant_user(principal)


async def resolve_role_from_membership(session: AsyncSession, account_id: str, organisation_id: str) -> str | None:
    """Return the LIVE org role for the account in the org, or None if no active membership.

    Filters ``deactivated_at IS NULL`` — a soft-deactivated membership must not
    resolve a role (ADR 017). Lazy-imports the ORM model to avoid the auth →
    api circular import; the caller already holds a session from a live factory.
    """
    from sqlalchemy import select

    from modulo.db.models.org_membership import OrgMembership

    result = await session.execute(
        select(OrgMembership.role).where(
            OrgMembership.account_id == account_id,
            OrgMembership.organisation_id == organisation_id,
            OrgMembership.deactivated_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _verify_identity(principal: AuthenticatedPrincipal) -> str | None:
    """Verify the JWT's account and organisation still exist, returning the LIVE org role.

    Uses lazy imports to avoid a circular dependency:
    ``auth.dependencies → api.dependencies → auth.dependencies``.

    ADR 017 live-role re-read: after the existence checks, the account's live
    org role is read from ``org_memberships`` (deactivated rows excluded).

        Failure modes:
    - missing/deactivated membership  raise 401 (removed users lose access immediately)
    - SQLAlchemyError during the read  raise 503 (fail-closed; a DB blip must
      not restore a removed user's stale role - ADR 017 review decision)
    - any other exception  propagate (500)
    """
    try:
        from sqlalchemy import text as _text
        from sqlalchemy.exc import SQLAlchemyError

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
                raise AccountNotFound()

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
                raise OrganisationNotFound()

            live_role = await resolve_role_from_membership(
                session,
                str(principal.account_id),
                str(principal.organisation_id),
            )
    except HTTPException:
        raise
    except SQLAlchemyError:
        _log.warning("permission.live_role_read_failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Role verification temporarily unavailable. Please try again.",
        ) from None

    if live_role is None:
        _log.warning(
            "auth.membership_not_found",
            extra={
                "account_id": str(principal.account_id),
                "org_id": str(principal.organisation_id),
                "username": principal.username,
            },
        )
        raise OrganisationMembershipNotFound()
    return live_role


async def require_system_admin(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> AuthenticatedPrincipal:
    """Require the current user to have system admin privileges."""
    if not current_user.is_system_admin:
        raise SystemAdminRequired()
    return current_user
