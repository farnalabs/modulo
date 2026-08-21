"""SCIM 2.0 auth — Bearer token from MODULO_SCIM_TOKEN env var.

Separate from the JWT-based auth used by the main API. The SCIM token is a
shared secret configured via environment variable. SCIM operates on the default
organisation (first org in the DB, or the org specified by MODULO_SCIM_DEFAULT_ORG_ID).
"""

import hmac
import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.models.problem import ProblemException, ProblemType
from modulo.core.feature_flags import CommunityTier, PlanContext, resolve_plan_context
from modulo.db.crud.organisation import get_organisation
from modulo.db.models.organisation import Organisation
from modulo.settings import Settings, get_settings

_scim_bearer = HTTPBearer(auto_error=False)


class ScimPrincipal:
    """SCIM-authenticated identity carrying the target org ID."""

    def __init__(self, organisation_id: uuid.UUID) -> None:
        self.organisation_id = organisation_id


async def get_scim_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_scim_bearer),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> ScimPrincipal:
    """Validate Bearer token against MODULO_SCIM_TOKEN and resolve the target org."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing SCIM token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not settings.modulo_scim_token:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SCIM is not configured — set MODULO_SCIM_TOKEN",
        )

    if not hmac.compare_digest(credentials.credentials, settings.modulo_scim_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid SCIM token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if settings.modulo_scim_default_org_id:
        try:
            org_id = uuid.UUID(settings.modulo_scim_default_org_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="MODULO_SCIM_DEFAULT_ORG_ID is not a valid UUID",
            ) from None
        return ScimPrincipal(organisation_id=org_id)

    result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No organisation exists — cannot resolve SCIM target org",
        )
    return ScimPrincipal(organisation_id=org.id)


async def get_scim_plan_context(
    principal: ScimPrincipal = Depends(get_scim_principal),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> PlanContext:
    """Resolve feature access for a SCIM principal without requiring a user JWT."""
    try:
        async with session.begin():
            org = await get_organisation(session, principal.organisation_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This feature is not available. Run database migrations to enable it.",
        ) from None
    except (TypeError, AttributeError):
        return CommunityTier()

    try:
        return await resolve_plan_context(settings, session, org=org)
    except (TypeError, AttributeError):
        return CommunityTier()


async def require_scim_feature(ctx: PlanContext = Depends(get_scim_plan_context)) -> None:
    """Require the SCIM feature using the SCIM principal's organisation plan."""
    if not ctx.feature_enabled("scim"):
        raise ProblemException(
            ProblemType.FEATURE_REQUIRED,
            detail="scim is not available on your plan",
            instance="scim",
        )
