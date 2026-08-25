"""CRUD for error notification rules."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, get_plan_context, require_permission
from modulo.api.models.error_notification_rule import (
    ErrorNotificationRuleCreate,
    ErrorNotificationRuleListResponse,
    ErrorNotificationRuleResponse,
    ErrorNotificationRuleUpdate,
)
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.feature_flags import PlanContext
from modulo.db.models.error_notification_rule import ErrorNotificationRule
from modulo.db.rls import set_rls_org

_MSG_NO_ORGANISATION = "No organisation"
_MSG_ERROR_TRACKING_NOT_AVAILABLE = "Error tracking is not available. Run database migrations to enable it."
_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE = "Error tracking is temporarily unavailable. Please try again."
_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE = "An unexpected error occurred while processing your request."
_CODE_ERROR_NOTIFICATION_MANAGE = "error_notification.manage"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/errors/notification-rules", tags=["error-notification-rules"])

_MAX_RULES_PER_ORG = 10
_MAX_RULES_COMMUNITY = 3


def _serialize_rule(rule: ErrorNotificationRule) -> dict[str, Any]:
    return {
        "id": str(rule.id),
        "name": rule.name,
        "enabled": rule.enabled,
        "condition_level": rule.condition_level,
        "condition_min_count": rule.condition_min_count,
        "condition_window_seconds": rule.condition_window_seconds,
        "action_type": rule.action_type,
        "webhook_url": rule.webhook_url,
        "cooldown_seconds": rule.cooldown_seconds,
        "created_at": rule.created_at.isoformat() if rule.created_at else "",
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else "",
    }


@router.get("", response_model=ErrorNotificationRuleListResponse)
@handle_db_errors("error_notification_rules.list_notification_rules")
async def list_notification_rules(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)

            result = await session.execute(
                select(ErrorNotificationRule)
                .where(ErrorNotificationRule.organisation_id == org_id)
                .order_by(ErrorNotificationRule.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            rules = list(result.scalars().all())

            count_result = await session.execute(
                select(func.count(ErrorNotificationRule.id)).where(ErrorNotificationRule.organisation_id == org_id)
            )
            total = count_result.scalar_one() or 0
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception("error_notification_rules.list_notification_rules")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("error_notification_rules.list_notification_rules")
        _log.warning("error_tracking.list_rules_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.list_rules_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc

    return {
        "items": [_serialize_rule(r) for r in rules],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post(
    "",
    response_model=ErrorNotificationRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors("error_notification_rules.create_notification_rule")
async def create_notification_rule(
    req: ErrorNotificationRuleCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERROR_NOTIFICATION_MANAGE),
    plan: PlanContext = Depends(get_plan_context),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    is_team = plan.feature_enabled("error_tracking")
    max_rules = _MAX_RULES_PER_ORG if is_team else _MAX_RULES_COMMUNITY
    if not is_team and req.action_type == "webhook":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Webhook notification rules require the Team tier",
        )

    try:
        async with session.begin():
            await set_rls_org(session, org_id)

            count_result = await session.execute(
                select(func.count(ErrorNotificationRule.id)).where(
                    ErrorNotificationRule.organisation_id == org_id,
                    ErrorNotificationRule.deleted_at.is_(None),
                )
            )
            current_count = count_result.scalar_one() or 0

            if current_count >= max_rules:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Maximum {max_rules} notification rules per organisation reached",
                )

            rule = ErrorNotificationRule(
                organisation_id=org_id,
                name=req.name,
                enabled=req.enabled,
                condition_level=req.condition_level,
                condition_min_count=req.condition_min_count,
                condition_window_seconds=req.condition_window_seconds,
                action_type=req.action_type,
                webhook_url=req.webhook_url,
                cooldown_seconds=req.cooldown_seconds,
            )
            session.add(rule)
            await session.flush()
    except HTTPException:
        raise
    except IntegrityError as exc:
        _log.warning("error_notification_rules.create_notification_rule_integrity")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Maximum {max_rules} notification rules per organisation reached",
        ) from exc
    except ProgrammingError as exc:
        _log.exception("error_notification_rules.create_notification_rule")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("error_notification_rules.create_notification_rule")
        _log.warning("error_tracking.create_rule_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.create_rule_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc

    return _serialize_rule(rule)


def _validate_webhook_tier_for_update(req: ErrorNotificationRuleUpdate, is_team: bool) -> None:
    if not is_team and (req.action_type == "webhook" or req.webhook_url is not None):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Webhook notification rules require the Team tier",
        )


def _apply_rule_updates(rule: ErrorNotificationRule, req: ErrorNotificationRuleUpdate) -> None:
    if req.name is not None:
        rule.name = req.name
    if req.enabled is not None:
        rule.enabled = req.enabled
    if req.condition_level is not None:
        rule.condition_level = req.condition_level
    if req.condition_min_count is not None:
        rule.condition_min_count = req.condition_min_count
    if req.condition_window_seconds is not None:
        rule.condition_window_seconds = req.condition_window_seconds
    if req.action_type is not None:
        rule.action_type = req.action_type
    if req.webhook_url is not None:
        rule.webhook_url = req.webhook_url
    if req.cooldown_seconds is not None:
        rule.cooldown_seconds = req.cooldown_seconds
    rule.updated_at = datetime.now(UTC)


def _translate_update_db_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProgrammingError):
        _log.exception("error_notification_rules.update_notification_rule")
        return HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        )
    if isinstance(exc, SQLAlchemyError):
        _log.exception("error_notification_rules.update_notification_rule")
        _log.warning("error_tracking.update_rule_db_error")
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        )
    _log.exception("error_tracking.update_rule_error")
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
    )


@router.put(
    "/{rule_id}",
    response_model=ErrorNotificationRuleResponse,
)
@handle_db_errors("error_notification_rules.update_notification_rule")
async def update_notification_rule(
    rule_id: uuid.UUID,
    req: ErrorNotificationRuleUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERROR_NOTIFICATION_MANAGE),
    plan: PlanContext = Depends(get_plan_context),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    is_team = plan.feature_enabled("error_tracking")
    _validate_webhook_tier_for_update(req, is_team)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)

            result = await session.execute(
                select(ErrorNotificationRule).where(
                    ErrorNotificationRule.organisation_id == org_id,
                    ErrorNotificationRule.id == rule_id,
                )
            )
            rule = result.scalar_one_or_none()

            if rule is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification rule not found")

            _apply_rule_updates(rule, req)
            await session.flush()
    except HTTPException:
        raise
    except Exception as exc:
        raise _translate_update_db_error(exc) from exc

    return _serialize_rule(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("error_notification_rules.delete_notification_rule")
async def delete_notification_rule(
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERROR_NOTIFICATION_MANAGE),
) -> None:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)

            result = await session.execute(
                select(ErrorNotificationRule).where(
                    ErrorNotificationRule.organisation_id == org_id,
                    ErrorNotificationRule.id == rule_id,
                )
            )
            rule = result.scalar_one_or_none()

            if rule is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification rule not found")

            await session.delete(rule)
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception("error_notification_rules.delete_notification_rule")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("error_notification_rules.delete_notification_rule")
        _log.warning("error_tracking.delete_rule_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.delete_rule_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc
