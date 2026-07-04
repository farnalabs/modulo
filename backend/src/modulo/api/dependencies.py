"""Shared FastAPI dependencies and utilities.

NOTE: Module-level globals `_engine` and `_session_factory` are used here
to cache a single engine + session-factory across the process lifetime.
This is thread-safe for async (single event-loop) usage but creates a
singleton that persists across tests — override via `app.dependency_overrides`
if test isolation is needed.
"""

from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Depends, HTTPException, status
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from modulo.api.models.problem import ProblemException, ProblemType
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import PlanContext
from modulo.settings import Settings, get_settings


def require_feature(feature_name: str):
    """FastAPI dependency factory — blocks access if the named feature is not enabled on the current plan.

    Returns 402 Payment Required when the feature is unavailable.
    Use as a default value in route parameters or in ``dependencies=[...]``:

    .. code-block:: python

       _: None = require_feature("sso")           # route parameter
       dependencies=[require_feature("team_rbac")]  # decorator
    """

    async def _check(ctx: PlanContext = Depends(get_plan_context)) -> None:
        if not ctx.feature_enabled(feature_name):
            raise ProblemException(
                ProblemType.FEATURE_REQUIRED,
                detail=f"{feature_name} is not available on your plan",
                instance=feature_name,
            )

    return Depends(_check)


def pg_connection_string(database_url: str) -> str:
    """Strip SQLAlchemy+asyncpg prefix to get a psycopg-compatible URL."""
    return (
        database_url.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg://", "postgresql://")
        .split("?")[0]
    )


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_or_create_engine(settings: Settings) -> AsyncEngine:
    """Return the process-global engine, creating it if necessary.

    This is the non-Depends version — use it outside FastAPI route handlers
    (e.g. in the MCP sub-app or background tasks) to share the same connection
    pool used by the main API.
    """
    global _engine
    if _engine is None:
        kw: dict[str, Any] = {
            "url": settings.database_url,
            "pool_pre_ping": True,
            "connect_args": {"timeout": 10},
        }
        db_type = settings.modulo_db.lower()
        if db_type != "sqlite":
            kw["pool_size"] = 10
            kw["max_overflow"] = 5
            kw["pool_timeout"] = 10
        _engine = create_async_engine(**kw)
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
            )


async def get_plan_context(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> PlanContext:
    """FastAPI dependency — resolve plan context per-org.

    Resolution order:
    1. Org-level license key (from ``org.settings_json["license_key"]``)
    2. System-level license (in-memory store or env var)
    3. Organisation.plan_id (per-org, from DB)
    4. SystemConfig.default_plan (deployment-wide, from DB)
    5. CommunityTier (default fallback)
    """
    from modulo.core.feature_flags import resolve_plan_context
    from modulo.db.crud.organisation import get_organisation

    org = None
    if current_user.organisation_id is not None:
        async with session.begin():
            org = await get_organisation(session, current_user.organisation_id)
    return await resolve_plan_context(get_settings(), session, org=org)
