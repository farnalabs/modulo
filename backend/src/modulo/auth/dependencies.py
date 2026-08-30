"""FastAPI auth dependencies for v1 user management."""

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError as JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal, decode_principal
from modulo.auth.permissions import _clamp_role
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
        raise InvalidToken from exc

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
        raise OrganisationMembershipRequired

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

    API keys are resolved by first looking up the key's organisation through a
    SECURITY DEFINER function (owned by the migration role, so it can read
    RLS-protected ``org_api_keys`` rows that the runtime app role cannot), then
    re-validating the key inside that org's RLS context — mirroring the MCP
    middleware, since ``org_api_keys`` has RLS enabled. For JWT credentials the
    behaviour is identical to :func:`get_current_tenant_user`.
    """
    if credentials is None:
        raise InvalidToken

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
            # valid key. On Postgres the runtime app role is RLS-subject (a
            # non-owner DML-granted role), so the org is resolved through a
            # SECURITY DEFINER function owned by the migration role rather than
            # SET row_security TO OFF (which only bypasses RLS for owners and
            # raises for a regular role). On generic backends there is no RLS
            # and the tenant filter only injects when an org context is set, so
            # a plain prefix scan works. Then re-validate inside the org
            # context before trusting the key.
            prefix = token[len(_MK_PREFIX) :][:_PREFIX_LEN]
            async with factory() as session, session.begin():
                dialect = await _ensure_active_transaction(session)
                if dialect == "postgresql":
                    org_id = (
                        await session.execute(
                            text("SELECT public.lookup_api_key_org(:prefix)"),
                            {"prefix": prefix},
                        )
                    ).scalar_one_or_none()
                else:
                    key_record = (
                        await session.execute(
                            select(OrgApiKey).where(
                                OrgApiKey.lookup_prefix == prefix,
                                OrgApiKey.revoked_at.is_(None),
                            )
                        )
                    ).scalar_one_or_none()
                    org_id = key_record.organisation_id if key_record is not None else None
            if org_id is None:
                raise ApiKeyInvalidError

            async with factory() as session, session.begin():
                await set_rls_org(session, org_id)
                key = await validate_api_key(session, token, org_id=org_id)

                # ADR 017 live-role re-read: clamp the minted role to the
                # account's current membership role, matching the MCP auth
                # path (api/mcp_server.py:770-790). A demoted or removed
                # member's key must not retain elevated privileges.
                live_role = await resolve_role_from_membership(
                    session,
                    str(key.account_id),
                    str(key.organisation_id),
                )
        except ApiKeyInvalidError:
            raise InvalidToken from None
        except SQLAlchemyError:
            _log.warning("auth.api_key_verify_failed", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database temporarily unavailable.",
            ) from None
        # Clamp: if the account has no active membership, reject.
        # If the live role is lower than the minted role, use the live role.
        if live_role is None:
            _log.warning(
                "auth.api_key_membership_not_found",
                extra={"account_id": str(key.account_id), "org_id": str(key.organisation_id)},
            )
            raise OrganisationMembershipNotFound
        clamped_role = _clamp_role(key.role, live_role)
        if not clamped_role:
            _log.warning(
                "auth.api_key_clamp_failed",
                extra={
                    "account_id": str(key.account_id),
                    "org_id": str(key.organisation_id),
                    "minted_role": key.role,
                    "live_role": live_role,
                },
            )
            raise OrganisationMembershipNotFound
        return TenantPrincipal(
            username=key.name,
            organisation_id=key.organisation_id,
            account_id=key.account_id,
            org_role=clamped_role,
            is_system_admin=False,
        )

    try:
        principal = decode_principal(token, settings.secret_key)
    except JWTError as exc:
        _log.warning(
            "auth.jwt_decode_failed",
            extra={"token_prefix": token[:10] + "...", "error": str(exc)},
        )
        raise InvalidToken from exc

    return await get_current_tenant_user(principal)


async def resolve_role_from_membership(session: AsyncSession, account_id: str, organisation_id: str) -> str | None:
    """Return the LIVE org role for the account in the org, or None if no active membership.

    Filters ``deactivated_at IS NULL`` — a soft-deactivated membership must not
    resolve a role (ADR 017). The canonical implementation lives in the db
    layer (``db.crud.org_membership``) so the service-layer HITL backstop can
    reuse it without reaching through the api layer; this re-export keeps the
    ``auth.dependencies`` surface stable for existing callers.
    """
    from modulo.db.crud.org_membership import resolve_role_from_membership as _resolve

    return await _resolve(session, account_id, organisation_id)


async def _verify_identity(principal: AuthenticatedPrincipal) -> str | None:
    """Verify the JWT's account and organisation still exist, returning the LIVE org role.

    Uses lazy imports to avoid a circular dependency:
    ``auth.dependencies → api.dependencies → auth.dependencies``.

    ADR 017 live-role re-read: after the existence checks, the account's live
    org role is read from ``org_memberships`` (deactivated rows excluded).

        Failure modes:
    - missing/deactivated membership -> raise 401 (removed users lose access immediately)
    - SQLAlchemyError during the read -> raise 503 (fail-closed; a DB blip must
      not restore a removed user's stale role - ADR 017 review decision)
    - any other exception -> propagate (500)
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
                raise AccountNotFound

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
                raise OrganisationNotFound

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
        raise OrganisationMembershipNotFound
    return live_role


async def require_system_admin(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> AuthenticatedPrincipal:
    """Require the current user to have system admin privileges."""
    if not current_user.is_system_admin:
        raise SystemAdminRequired
    return current_user
