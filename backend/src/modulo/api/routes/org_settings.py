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

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.cost_settings import COST_CONTROLS_KEY, DEFAULT_CURRENCY, SUPPORTED_CURRENCIES
from modulo.db.crud.organisation import get_organisation
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/org", tags=["org"])


class OrgSettingsResponse(BaseModel):
    currency: str = "USD"


class OrgGuardrailsKillSwitchResponse(BaseModel):
    enabled: bool
    enabled_at: str | None


@router.get("/settings", response_model=OrgSettingsResponse)
@handle_db_errors("org.get_settings")
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
        _log.debug("org.get_settings HTTPException (org_id=%s) detail=%s", current_user.organisation_id, exc.detail)
        raise exc
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in get_org_settings")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    currency = DEFAULT_CURRENCY
    if org is not None and isinstance(org.settings_json, dict):
        cc = org.settings_json.get(COST_CONTROLS_KEY)
        if isinstance(cc, dict):
            value = cc.get("currency")
            if isinstance(value, str) and value in SUPPORTED_CURRENCIES:
                currency = value
    return OrgSettingsResponse(currency=currency)


@router.get("/settings/guardrails/kill-switch", response_model=OrgGuardrailsKillSwitchResponse)
@handle_db_errors("org.get_guardrails_kill_switch")
async def get_org_guardrails_kill_switch(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgGuardrailsKillSwitchResponse:
    """Read the org's guardrails kill-switch state (any org member).

    Unlike the admin-only write endpoint (``admin_orgs``), this read is
    available to every authenticated member of the caller's own organisation
    so non-admin members can see whether the kill-switch is ON. It is
    read-only — toggling remains admin-only.
    """
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
    except ProgrammingError:
        _log.exception("org.get_guardrails_kill_switch ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("org.get_guardrails_kill_switch SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A database error occurred. Please try again.",
        ) from None
    except HTTPException as exc:
        _log.debug(
            "org.get_guardrails_kill_switch HTTPException (org_id=%s) detail=%s",
            current_user.organisation_id,
            exc.detail,
        )
        raise exc
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in get_org_guardrails_kill_switch")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found.")
    return OrgGuardrailsKillSwitchResponse(
        enabled=bool(org.guardrails_kill_switch),
        enabled_at=org.guardrails_kill_switch_at.isoformat() if org.guardrails_kill_switch_at else None,
    )
