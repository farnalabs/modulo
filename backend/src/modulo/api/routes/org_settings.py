"""Non-admin org-level settings surface for display concerns.

Every org member (not just ``cost.manage`` admins) needs to read the org's
display currency so cost surfaces render the right symbol. This router exposes
a single tiny read endpoint gated only by tenant auth
(``get_current_tenant_user``) — no feature flag, no plan gate, no permission.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.organisation import get_organisation
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/org", tags=["org"])

# Kept in sync with modulo.api.routes.costs._COST_CONTROLS_KEY / _DEFAULT_CURRENCY.
_COST_CONTROLS_KEY = "cost_controls"
_DEFAULT_CURRENCY = "USD"


class OrgSettingsResponse(BaseModel):
    currency: str = "USD"


@handle_db_errors("org.get_settings")
@router.get("/settings", response_model=OrgSettingsResponse)
async def get_org_settings(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgSettingsResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
    except ProgrammingError:
        _log.exception("org.get_settings ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("org.get_settings SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A database error occurred. Please try again.",
        ) from None
    except HTTPException as exc:
        raise exc
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in get_org_settings")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    currency = _DEFAULT_CURRENCY
    if org is not None and isinstance(org.settings_json, dict):
        cc = org.settings_json.get(_COST_CONTROLS_KEY)
        if isinstance(cc, dict):
            value = cc.get("currency")
            if isinstance(value, str) and value:
                currency = value
    return OrgSettingsResponse(currency=currency)
