"""Shared FastAPI dependencies and utilities.

NOTE: Module-level globals `_engine` and `_session_factory` are used here
to cache a single engine + session-factory across the process lifetime.
This is thread-safe for async (single event-loop) usage but creates a
singleton that persists across tests — override via `app.dependency_overrides`
if test isolation is needed.

The engine itself is NOT built here: ``get_or_create_engine`` delegates to
``modulo.db.session.get_shared_engine`` so the API, dispatch, the SAQ worker
and crons all share one per-process pool with identical Fly/HAProxy knobs
(``pool_pre_ping``, ``statement_cache_size=0``) instead of each maintaining a
divergent second pool.
"""

import logging
import uuid
from collections.abc import AsyncGenerator
from contextvars import Token
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.params import Depends as DependsParameter
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError as JWTError
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from modulo.api.models.problem import ProblemException, ProblemType
from modulo.api.team_scope import TeamScopeProvider, team_membership_exists
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal, decode_principal
from modulo.auth.permissions import (
    PermissionConfigurationError,
    PermissionDenied,
    assert_org_role,
    reset_authz_enforce,
    resolve_required,
    set_authz_enforce,
)
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY
from modulo.core.feature_flags import PlanContext
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.db.settings_resolver import resolve_authz_enforce
from modulo.settings import Settings, get_settings

_CODE_PERMISSION_DENIED = "permission.denied"
_MSG_DATABASE_TEMPORARILY_UNAVAILABLE = "Database temporarily unavailable."


logger = logging.getLogger(__name__)


def _tagged_dep(
    dep: object,
    *,
    permission: str,
    permission_kind: str,
    min_role: str | None = None,
) -> DependsParameter:
    """Attach introspection metadata to a ``Depends`` object.

    ``fastapi.params.Depends`` is a frozen dataclass, so the tags are set via
    ``object.__setattr__``. The introspection test reads ``_dep.permission``
    and ``_dep.permission_kind`` (and ``min_role`` for scoped-hybrid variants)
    off the dependency default.
    """
    object.__setattr__(dep, "permission", permission)
    object.__setattr__(dep, "permission_kind", permission_kind)
    if min_role is not None:
        object.__setattr__(dep, "min_role", min_role)
    return cast("DependsParameter", dep)


def require_permission(permission: str) -> Any:
    """FastAPI dependency factory — require the current tenant's org role.

    Resolves the minimum role for ``permission`` at factory-creation time so a
    typo'd permission key fails fast at import. The dependency wraps
    ``get_current_tenant_user`` and compares the principal's org role against
    the hierarchy, raising 403 on denial.

    .. code-block:: python

       principal: TenantPrincipal = Depends(require_permission("pipeline.graph.update"))
    """
    required = resolve_required(permission)

    async def _check(
        principal: TenantPrincipal = Depends(get_current_tenant_user),
        session: AsyncSession = Depends(get_db_session),
    ) -> TenantPrincipal:
        token: Token[bool | None] | None = None
        try:
            if principal.organisation_id is not None:
                try:
                    async with session.begin():
                        enforce = await resolve_authz_enforce(session, principal.organisation_id)
                except SQLAlchemyError:
                    # Kill-switch read failure defaults to ENFORCE (fail-closed,
                    # ADR 017 DECISION 3): a DB blip must not fail-open the
                    # org-role gate.
                    logger.exception("permission.kill_switch_read_failed")
                    enforce = True
                token = set_authz_enforce(enforce)
            try:
                assert_org_role(principal.org_role, required, permission)
            except PermissionDenied as exc:
                logger.warning(
                    _CODE_PERMISSION_DENIED,
                    extra={
                        "permission": permission,
                        "required": required,
                        "actual": principal.org_role,
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission '{permission}' requires '{required}' role",
                ) from exc
            return principal
        finally:
            if token is not None:
                reset_authz_enforce(token)

    return _tagged_dep(Depends(_check), permission=permission, permission_kind="tenant")


def require_in_dev_operator(principal: TenantPrincipal, permission: str) -> None:
    """Enforce the operator gate for an In-Dev reveal on a list endpoint.

    Called by list handlers that accept ``?include_in_dev=true``; the override
    must only be honoured for operator+ principals (ADR 010 disclosure control
    — In-Dev items are deliberately hidden from the UI and all list endpoints).
    The base ``connector.list`` / ``model_backend.list`` / ``library.search``
    permissions remain viewer-level so ordinary listing is unchanged; only the
    In-Dev reveal is gated (``*.list.in_dev`` resolves to ``operator``).

    Fail-closed and NEVER lifted by the org authz kill switch (ADR 017 DECISION
    3 scope pin): the In-Dev disclosure control is deliberately
    ``kill_switch_eligible=False`` so an org that disables authz enforcement
    still cannot expose pre-release items to viewers/runners.
    """
    required = resolve_required(permission)
    try:
        assert_org_role(principal.org_role, required, permission, kill_switch_eligible=False)
    except PermissionDenied as exc:
        logger.warning(
            _CODE_PERMISSION_DENIED,
            extra={
                "permission": permission,
                "required": required,
                "actual": principal.org_role,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission '{permission}' requires '{required}' role",
        ) from exc


def require_permission_any_credential(permission: str) -> Any:
    """FastAPI dependency factory — require the org role for JWT OR API-key callers.

    Resolves the principal via `get_current_tenant_user_or_api_key` so both
    user JWTs and `mk_` org API keys are accepted (the documented CI/CD
    credential path, PRD \u00a75.2). API-key roles are clamped to the key owner's
    LIVE org role via `_clamp_role` (a demoted operator's key degrades). The
    org-role floor and kill-switch ContextVar behave identically to
    `require_permission`.

    .. code-block:: python

       principal: TenantPrincipal = Depends(require_permission_any_credential("run.trigger"))
    """
    required = resolve_required(permission)

    async def _check(
        principal: TenantPrincipal = Depends(get_current_tenant_user_or_api_key),
        session: AsyncSession = Depends(get_db_session),
    ) -> TenantPrincipal:
        token: Token[bool | None] | None = None
        try:
            if principal.organisation_id is not None:
                try:
                    async with session.begin():
                        enforce = await resolve_authz_enforce(session, principal.organisation_id)
                except SQLAlchemyError:
                    # Kill-switch read failure defaults to ENFORCE (fail-closed,
                    # ADR 017 DECISION 3): a DB blip must not fail-open the
                    # org-role gate for JWT or API-key callers.
                    logger.exception("permission.kill_switch_read_failed")
                    enforce = True
                token = set_authz_enforce(enforce)
            try:
                assert_org_role(principal.org_role, required, permission)
            except PermissionDenied as exc:
                logger.warning(
                    _CODE_PERMISSION_DENIED,
                    extra={
                        "permission": permission,
                        "required": required,
                        "actual": principal.org_role,
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission '{permission}' requires '{required}' role",
                ) from exc
            return principal
        finally:
            if token is not None:
                reset_authz_enforce(token)

    return _tagged_dep(Depends(_check), permission=permission, permission_kind="tenant_or_api_key")


def require_system_permission(permission: str) -> DependsParameter:
    """FastAPI dependency factory — strict ``is_system_admin`` only.

    No org-role fall-through (license-gate bypass). Resolves the permission
    key at factory-creation time for import-time fail-fast, but the actual
    gate is purely the principal's ``is_system_admin`` flag.

    .. code-block:: python

       current_user: AuthenticatedPrincipal = Depends(require_system_permission("org.email.manage"))
    """
    resolve_required(permission)

    async def _check(
        current_user: AuthenticatedPrincipal = Depends(get_current_user),
    ) -> AuthenticatedPrincipal:
        if not current_user.is_system_admin:
            logger.warning(
                _CODE_PERMISSION_DENIED,
                extra={
                    "permission": permission,
                    "required": "system_admin",
                    "actual": "org_role_only",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' requires system admin role",
            )
        return current_user

    return _tagged_dep(Depends(_check), permission=permission, permission_kind="system")


def require_system_or_org_admin(permission: str) -> Any:
    """FastAPI dependency factory — the one true hybrid.

    Passes if the principal is a system admin OR holds org role ``admin``
    in the current tenant. Used for destructive org-level operations where
    both an org admin and a system admin must be allowed.

    .. code-block:: python

       principal: TenantPrincipal = Depends(require_system_or_org_admin("org.delete"))
    """
    resolve_required(permission)

    async def _check(
        principal: TenantPrincipal = Depends(get_current_tenant_user),
    ) -> TenantPrincipal:
        if principal.is_system_admin:
            return principal
        try:
            # Destructive operations are NEVER lifted by the kill switch
            # (ADR 017 DECISION 3 scope pin): org deletion stays gated on the
            # org-admin role even when authz.enforce is off.
            assert_org_role(principal.org_role, "admin", permission, kill_switch_eligible=False)
        except PermissionDenied as exc:
            logger.warning(
                _CODE_PERMISSION_DENIED,
                extra={
                    "permission": permission,
                    "required": "admin",
                    "actual": principal.org_role,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' requires org admin role",
            ) from exc
        return principal

    return _tagged_dep(Depends(_check), permission=permission, permission_kind="system_or_org")


async def _resolve_live_org_role(
    session: AsyncSession,
    account_id: uuid.UUID,
    org_id: uuid.UUID,
) -> str | None:
    """Return the caller's live org role in the target org, or `None`.

    Delegates to the single membership lookup in `auth.dependencies`
    (ADR 017 live-role re-read); keeps the name for existing callers.
    """
    from modulo.auth.dependencies import resolve_role_from_membership

    return await resolve_role_from_membership(
        session,
        str(account_id),
        str(org_id),
    )


def require_target_org_role(
    permission: str,
    min_role: str,
    kill_switch_eligible: bool = True,
) -> DependsParameter:
    """FastAPI dependency factory — scoped-hybrid reads and mutations.

    Passes if the principal is a system admin OR holds a live membership in
    the *target* org (read from the route path param ``org_id``) at ``min_role``
    or higher. A multi-org member operating with current-org B gains access to
    org A at the minimum role; a non-member is denied.

    Reads variant: ``org.email.view``/``org.license.view`` at ``operator``.
    Mutations variant: ``org.email.manage`` at ``admin``.

    ``min_role`` must equal the permission's registry-resolved role
    (``PERMISSIONS`` is the single source of truth); a mismatch is a
    configuration error and fails fast at factory-creation time.

    ``kill_switch_eligible`` (default True) mirrors
    ``require_system_or_org_admin``'s hardcoded ``kill_switch_eligible=False``:
    pass False for destructive gates (e.g. the org trigger-pause toggle) so the
    org authz kill-switch can never lift them.

    .. code-block:: python

       _: AuthenticatedPrincipal = Depends(require_target_org_role("org.email.view", "operator"))
    """
    resolved = resolve_required(permission)
    if min_role not in ORG_ROLE_HIERARCHY:
        raise PermissionConfigurationError(f"min_role '{min_role}' is not a valid org role")
    if min_role != resolved:
        raise PermissionConfigurationError(
            f"min_role '{min_role}' for '{permission}' does not match the registry-resolved role '{resolved}'",
        )

    async def _check(
        request: Request,
        current_user: AuthenticatedPrincipal = Depends(get_current_user),
        session: AsyncSession = Depends(get_db_session),
    ) -> AuthenticatedPrincipal:
        org_id_raw = request.path_params.get("org_id")
        if org_id_raw is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing org_id path parameter",
            )
        if current_user.is_system_admin:
            return current_user
        try:
            org_id = uuid.UUID(str(org_id_raw))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid org_id path parameter",
            ) from None
        token: Token[bool | None] | None = None
        try:
            try:
                async with session.begin():
                    role = await _resolve_live_org_role(session, current_user.account_id, org_id)
                    enforce = await resolve_authz_enforce(session, org_id)
                token = set_authz_enforce(enforce)
            except SQLAlchemyError:
                logger.exception("permission.live_role_read_failed")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
                ) from None
            try:
                assert_org_role(role, min_role, permission, kill_switch_eligible=kill_switch_eligible)
            except PermissionDenied as exc:
                logger.warning(
                    _CODE_PERMISSION_DENIED,
                    extra={
                        "permission": permission,
                        "required": min_role,
                        "actual": role,
                        "target_org_id": str(org_id),
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission '{permission}' requires '{min_role}' role in target organisation",
                ) from exc
            return current_user
        finally:
            if token is not None:
                reset_authz_enforce(token)

    return _tagged_dep(
        Depends(_check),
        permission=permission,
        permission_kind="scoped_hybrid",
        min_role=min_role,
    )


def require_team_membership_or_admin(resource_team_id_provider: TeamScopeProvider) -> Any:
    """Team-scoped gate matching RLS visibility: membership (any role) OR org-admin.

    Mirrors the DB team-visibility RLS policy (migration 0002:375-383,
    0003:911-919) exactly:

    .. code-block:: text

       visibility='org' OR visibility IS NULL
       OR owner_team_id IS NULL
       OR membership (any team role)
       OR org_role='admin'

    ``admin`` bypasses before any DB work (RLS parity — the org-admin sees every
    team-scoped row). Rows with ``visibility='org'`` or ``owner_team_id IS NULL``
    are NOT team-gated (org-role floor only). The org-role floor itself is
    enforced by ``require_permission`` on the same endpoint — this dependency
    adds ONLY the membership-or-admin gate. Team *role* remains dead code until
    Phase 3; any membership row qualifies.

    ``resource_team_id_provider`` resolves the target row's ``owner_team_id``
    and ``visibility`` from the request (path params + a DB lookup); see
    ``modulo.api.team_scope``. ``runs`` is intentionally not team-gated — it has
    no ``visibility`` column (org-role floor only, ADR 017 iteration-7 special).

    .. code-block:: python

       _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope)
    """

    async def _check(
        request: Request,
        principal: TenantPrincipal = Depends(get_current_tenant_user),
        session: AsyncSession = Depends(get_db_session),
    ) -> TenantPrincipal:
        if principal.org_role == "admin":
            return principal
        try:
            # ONE transaction: RLS context (set_config ... is_local) is scoped
            # to the transaction and reverts on COMMIT, so the provider read
            # and the membership check MUST share a single session.begin() or
            # the second query runs with an empty app.organisation_id and
            # team_memberships RLS filters every row (hard 403 on Postgres).
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                await set_rls_user_context(session, principal.account_id, principal.org_role)
                row = await resource_team_id_provider(request, session)
                if row is not None and row.visibility not in ("org", None) and row.owner_team_id is not None:
                    is_member = await team_membership_exists(
                        session,
                        account_id=principal.account_id,
                        team_id=row.owner_team_id,
                    )
                else:
                    is_member = True
        except SQLAlchemyError:
            logger.exception("permission.team_scope_read_failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
            ) from None
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
        if row.visibility == "org" or row.visibility is None or row.owner_team_id is None:
            return principal
        if not is_member:
            logger.warning(
                _CODE_PERMISSION_DENIED,
                extra={
                    "permission": "team.membership_or_admin",
                    "required": "team_membership",
                    "actual": principal.org_role,
                    "owner_team_id": str(row.owner_team_id),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not a member of the team that owns this resource",
            )
        return principal

    return _tagged_dep(
        Depends(_check),
        permission="team.membership_or_admin",
        permission_kind="team_scope",
    )


def require_feature(feature_name: str) -> DependsParameter:
    """FastAPI dependency factory — blocks access if the named feature is not enabled on the current plan.

    Returns 402 Payment Required when the feature is unavailable.
    Use as a default value in route parameters or in ``dependencies=[...]``:

    .. code-block:: python

       _: object = require_feature("sso")           # route parameter
       dependencies=[require_feature("team_rbac")]  # decorator
    """

    async def _check(ctx: PlanContext = Depends(get_plan_context)) -> None:
        if not ctx.feature_enabled(feature_name):
            raise ProblemException(
                ProblemType.FEATURE_REQUIRED,
                detail=f"{feature_name} is not available on your plan",
                instance=feature_name,
            )

    return cast("DependsParameter", Depends(_check))


def pg_connection_string(database_url: str) -> str:
    """Strip SQLAlchemy prefix to get a psycopg-compatible URL.

    Preserves any existing sslmode parameter from the DATABASE_URL
    (e.g. sslmode=require for Fly.io managed Postgres).
    """
    url = database_url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )
    if "sslmode" in url:
        return url
    if "?" in url:
        return url + "&sslmode=disable"
    return url + "?sslmode=disable"


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_or_create_engine(_settings: Settings) -> AsyncEngine:
    """Return the process-global engine, creating it if necessary.

    This is the non-Depends version — use it outside FastAPI route handlers
    (e.g. in the MCP sub-app or background tasks) to share the same connection
    pool used by the main API.

    Delegates to :func:`modulo.db.session.get_shared_engine` — the one engine
    factory per process — so dispatch, the SAQ worker, crons and the API share
    a single pool with identical Fly/HAProxy knobs (``pool_pre_ping``,
    ``statement_cache_size=0``) instead of maintaining divergent pools. The
    API's pool sizing (20/10) is passed as the creation-time override; the
    first caller in the process fixes the pool size for everyone. ``settings``
    is retained for signature compatibility — the shared factory reads the
    process settings via ``get_settings()``.
    """
    global _engine
    if _engine is None:
        from modulo.db.session import get_shared_engine

        _engine = get_shared_engine(pool_size=20, max_overflow=10)
    return _engine


def get_or_create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return the process-global session factory, creating it if necessary."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    return _session_factory


def _get_engine(settings: Settings = Depends(get_settings)) -> AsyncEngine:
    return get_or_create_engine(settings)


def _get_session_factory(
    engine: AsyncEngine = Depends(_get_engine),
) -> async_sessionmaker[AsyncSession]:
    return get_or_create_session_factory(engine)


async def get_db_session(
    factory: async_sessionmaker[AsyncSession] = Depends(_get_session_factory),
) -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession.

    Transaction management is left to the caller.  ``ProgrammingError``
    (missing DB table) is caught centrally and converted to a 501 so that
    unhandled migration gaps don't leak raw 500s to the client.
    """
    async with factory() as session:
        try:
            yield session
        except ProgrammingError:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Feature is not available. Run database migrations to enable it.",
            ) from None


async def get_plan_context(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> PlanContext:
    """FastAPI dependency — resolve plan context per-org.

    Resolution order:
    1. Org-level license key (from ``org.settings_json["license_key"]``)
    2. System-level license (in-memory store or env var)
    3. Organisation.plan_id (per-org, from DB)
    4. SystemConfig.default_plan (deployment-wide, from DB)
    5. CommunityTier (default fallback)
    """
    from modulo.core.feature_flags import CommunityTier, resolve_plan_context
    from modulo.db.crud.organisation import get_organisation

    org = None
    if current_user.organisation_id is not None:
        try:
            async with session.begin():
                org = await get_organisation(session, current_user.organisation_id)
        except ProgrammingError:
            logger.exception("api.dependencies")

            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="This feature is not available. Run database migrations to enable it.",
            ) from None

        except (TypeError, AttributeError):
            logger.warning("Session does not support async begin() — returning CommunityTier")

            return CommunityTier()

    try:
        async with session.begin():
            return await resolve_plan_context(settings, session, org=org)

    except (TypeError, AttributeError):
        logger.warning("Session does not support resolve_plan_context — returning CommunityTier")

        return CommunityTier()


async def get_current_tenant_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> TenantPrincipal | None:
    """Optional tenant principal (``None`` when unauthenticated).

    Fail-closed for break-glass accounts (plan v17, deliverable (B)): ANY
    break-glass account — live or denied — resolving through this optional path
    is treated as deny and returns ``None``, so webhook routes never grant a
    break-glass account org context or a permission bypass. The account is
    loaded by PK (``session.get``); if it cannot be read the path folds to
    ``None`` (a DB blip must not fail-open a break-glass principal).

    The login-route create_family mint for break-glass logins is DELIBERATELY
    excluded — it IS the recovery path. Consumers import this function directly
    from ``modulo.api.dependencies`` (the previous PEP 562 lazy re-export in
    ``auth.dependencies`` was removed; ``webhooks.py`` imports it here).
    """
    if credentials is None:
        return None
    try:
        principal = decode_principal(credentials.credentials, settings.secret_key)
        if principal.organisation_id is None or principal.org_role is None:
            return None
    except JWTError:
        return None

    try:
        from modulo.db.crud.break_glass_deny import is_break_glass_denied, is_break_glass_live
        from modulo.db.models.account import Account

        now = datetime.now(UTC)
        async with session.begin():
            account = await session.get(Account, principal.account_id)
    except SQLAlchemyError:
        logger.exception("auth.break_glass_optional_read_failed")
        return None

    if account is not None and account.is_break_glass is True:
        is_break_glass_account = is_break_glass_denied(
            is_break_glass=account.is_break_glass,
            break_glass_expires_at=account.break_glass_expires_at,
            break_glass_deactivated_at=account.break_glass_deactivated_at,
            active=account.active,
            now=now,
        ) or is_break_glass_live(
            is_break_glass=account.is_break_glass,
            break_glass_expires_at=account.break_glass_expires_at,
            break_glass_deactivated_at=account.break_glass_deactivated_at,
            active=account.active,
            now=now,
        )
        if is_break_glass_account:
            logger.warning(
                "auth.break_glass_optional_denied",
                extra={"account_id": str(principal.account_id), "username": principal.username},
            )
            return None

    return TenantPrincipal(
        username=principal.username,
        organisation_id=principal.organisation_id,
        account_id=principal.account_id,
        org_role=principal.org_role,
        is_system_admin=principal.is_system_admin,
    )


async def deny_break_glass_mint(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AuthenticatedPrincipal:
    """Raise 403 when the current principal's account is a break-glass account.

    Break-glass accounts can NEVER mint secrets or credentials — live OR denied
    (plan v17, API-key + long-lived deny). Enforced via a shared DI/dependency
    marker on the enumerated secret-bearing create/update/delete routes with a
    uniform 403. The login-route create_family mint for break-glass logins is
    DELIBERATELY EXCLUDED — it IS the recovery path.

    The account is loaded by primary key (``session.get``) and the deny rule is
    the union of the shared ``is_break_glass_denied`` / ``is_break_glass_live``
    decisions from ``db.crud.break_glass_deny`` (single-sourced — never
    duplicated here). The ``is True`` identity check guards against ORM test
    doubles whose auto-created attributes are truthy mocks, not booleans. A DB
    read failure folds to 503 (fail-closed: a blip must not fail-open a
    break-glass mint).
    """
    try:
        from modulo.db.crud.break_glass_deny import is_break_glass_denied, is_break_glass_live
        from modulo.db.models.account import Account

        now = datetime.now(UTC)
        async with session.begin():
            account = await session.get(Account, current_user.account_id)
    except SQLAlchemyError:
        logger.exception("permission.break_glass_mint_read_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    if account is None:
        return current_user
    if account.is_break_glass is True:
        is_break_glass_account = is_break_glass_denied(
            is_break_glass=account.is_break_glass,
            break_glass_expires_at=account.break_glass_expires_at,
            break_glass_deactivated_at=account.break_glass_deactivated_at,
            active=account.active,
            now=now,
        ) or is_break_glass_live(
            is_break_glass=account.is_break_glass,
            break_glass_expires_at=account.break_glass_expires_at,
            break_glass_deactivated_at=account.break_glass_deactivated_at,
            active=account.active,
            now=now,
        )
        if is_break_glass_account:
            logger.warning(
                "permission.break_glass_mint_denied",
                extra={"account_id": str(current_user.account_id), "username": current_user.username},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Break-glass accounts cannot create or modify secrets/credentials",
            )
    return current_user
