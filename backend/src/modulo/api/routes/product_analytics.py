"""Product analytics consent & settings routes (FAR-354).

Provides:
- POST /api/v1/org/product-analytics/consent  — accept/decline/dismiss
- GET  /api/v1/org/product-analytics          — current consent state + instance switch
- PUT  /api/v1/org/product-analytics          — admin level toggle
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.product_analytics.consent import (
    apply_consent_action,
    get_product_analytics_block,
    is_egress_allowed,
    is_instance_analytics_enabled,
    is_prompt_eligible,
    merge_product_analytics_block,
    set_level,
)
from modulo.core.product_analytics.constants import (
    DEFAULT_LEVEL,
    VALID_LEVELS,
)
from modulo.db.crud.organisation import get_organisation
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/org/product-analytics", tags=["org", "product-analytics"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ConsentRequest(BaseModel):
    action: Literal["accept", "decline", "dismiss"]


class ConsentResponse(BaseModel):
    level: str = DEFAULT_LEVEL
    prompted: str | None = None
    prompted_at: str | None = None
    level_changed_at: str | None = None
    instance_enabled: bool = False
    egress_allowed: bool = False
    prompt_eligible: bool = False


class LevelUpdateRequest(BaseModel):
    level: str = Field(..., description="Analytics level: 'off' or 'all'")

    def validate_level(self) -> str:
        if self.level not in VALID_LEVELS:
            raise ValueError(f"Invalid level: {self.level!r}. Must be one of {VALID_LEVELS}")
        return self.level


class LevelUpdateResponse(BaseModel):
    level: str
    level_changed_at: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_consent_response(
    consent: dict[str, object],
    instance_enabled: bool,
) -> ConsentResponse:
    egress = is_egress_allowed(instance_enabled, str(consent.get("level", DEFAULT_LEVEL)))
    return ConsentResponse(
        level=str(consent.get("level", DEFAULT_LEVEL)),
        prompted=consent.get("prompted"),  # type: ignore[arg-type]
        prompted_at=consent.get("prompted_at"),  # type: ignore[arg-type]
        level_changed_at=consent.get("level_changed_at"),  # type: ignore[arg-type]
        instance_enabled=instance_enabled,
        egress_allowed=egress,
        prompt_eligible=is_prompt_eligible(consent),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/consent", response_model=ConsentResponse)
@handle_db_errors("product_analytics.consent")
async def post_consent(
    body: ConsentRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> ConsentResponse:
    """Accept, decline, or dismiss the product analytics consent prompt."""
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Organisation not found.",
                )

            consent = get_product_analytics_block(org.settings_json)

            # Prompt eligibility gate: only eligible orgs can consent
            if not is_prompt_eligible(consent):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Consent prompt is not currently eligible. Dismissed prompts re-appear after 7 days.",
                )

            updated_consent = apply_consent_action(consent, body.action)
            new_settings = merge_product_analytics_block(org.settings_json, updated_consent)
            org.settings_json = new_settings

            instance_enabled = await is_instance_analytics_enabled(session)

            return _build_consent_response(updated_consent, instance_enabled)
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("product_analytics.consent unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.get("", response_model=ConsentResponse)
@handle_db_errors("product_analytics.get")
async def get_product_analytics(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> ConsentResponse:
    """Read the current product analytics consent state and instance switch."""
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Organisation not found.",
                )

            consent = get_product_analytics_block(org.settings_json)
            instance_enabled = await is_instance_analytics_enabled(session)

            return _build_consent_response(consent, instance_enabled)
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("product_analytics.get unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.put("", response_model=LevelUpdateResponse)
@handle_db_errors("product_analytics.update_level")
async def update_product_analytics_level(
    body: LevelUpdateRequest,
    current_user: TenantPrincipal = Depends(require_permission("org.settings.update")),
    session: AsyncSession = Depends(get_db_session),
) -> LevelUpdateResponse:
    """Update the analytics level (admin toggle). Turning off will purge staging buffer (FAR-355)."""
    try:
        body.validate_level()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Organisation not found.",
                )

            consent = get_product_analytics_block(org.settings_json)
            updated_consent = set_level(consent, body.level)
            new_settings = merge_product_analytics_block(org.settings_json, updated_consent)
            org.settings_json = new_settings

            return LevelUpdateResponse(
                level=updated_consent["level"],
                level_changed_at=updated_consent.get("level_changed_at"),  # type: ignore[arg-type]
            )
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("product_analytics.update_level unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
