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
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.db.models.organisation import Organisation
from modulo.settings import Settings, get_settings

_scim_bearer = HTTPBearer()


class ScimPrincipal:
    """SCIM-authenticated identity carrying the target org ID."""

    def __init__(self, organisation_id: uuid.UUID) -> None:
        self.organisation_id = organisation_id


async def get_scim_principal(
    credentials: HTTPAuthorizationCredentials = Depends(_scim_bearer),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> ScimPrincipal:
    """Validate Bearer token against MODULO_SCIM_TOKEN and resolve the target org."""
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

    result = await session.execute(
        select(Organisation).order_by(Organisation.created_at).limit(1)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No organisation exists — cannot resolve SCIM target org",
        )
    return ScimPrincipal(organisation_id=org.id)
