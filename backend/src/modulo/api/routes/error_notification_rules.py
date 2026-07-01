"""CRUD for error notification rules."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.models.error_notification_rule import (
    ErrorNotificationRuleCreate,
    ErrorNotificationRuleListResponse,
    ErrorNotificationRuleResponse,
    ErrorNotificationRuleUpdate,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.error_notification_rule import ErrorNotificationRule
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/errors/notification-rules", tags=["error-notification-rules"])

_MAX_RULES_PER_ORG = 10
_MAX_RULES_COMMUNITY = 3


def _serialize_rule(rule: ErrorNotificationRule) -> dict:
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


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required for notification rule management",
        )


@router.get("", response_model=ErrorNotificationRuleListResponse)
async def list_notification_rules(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

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
            select(func.count(ErrorNotificationRule.id)).where(
                ErrorNotificationRule.organisation_id == org_id
            )
        )
        total = count_result.scalar_one() or 0

    return {
        "items": [_serialize_rule(r) for r in rules],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("", response_model=ErrorNotificationRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_notification_rule(
    body: ErrorNotificationRuleCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

    _require_admin(principal)

    max_rules = _MAX_RULES_COMMUNITY if principal.org_role == "runner" else _MAX_RULES_PER_ORG

    async with session.begin():
        await set_rls_org(session, org_id)

        count_result = await session.execute(
            select(func.count(ErrorNotificationRule.id)).where(
                ErrorNotificationRule.organisation_id == org_id
            )
        )
        current_count = count_result.scalar_one() or 0

        if current_count >= max_rules:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Maximum {max_rules} notification rules per organisation reached",
            )

        rule = ErrorNotificationRule(
            organisation_id=org_id,
            name=body.name,
            enabled=body.enabled,
            condition_level=body.condition_level,
            condition_min_count=body.condition_min_count,
            condition_window_seconds=body.condition_window_seconds,
            action_type=body.action_type,
            webhook_url=body.webhook_url,
            cooldown_seconds=body.cooldown_seconds,
        )
        session.add(rule)
        await session.flush()

    return _serialize_rule(rule)


@router.put("/{rule_id}", response_model=ErrorNotificationRuleResponse)
async def update_notification_rule(
    rule_id: uuid.UUID,
    body: ErrorNotificationRuleUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

    _require_admin(principal)

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

        if body.name is not None:
            rule.name = body.name
        if body.enabled is not None:
            rule.enabled = body.enabled
        if body.condition_level is not None:
            rule.condition_level = body.condition_level
        if body.condition_min_count is not None:
            rule.condition_min_count = body.condition_min_count
        if body.condition_window_seconds is not None:
            rule.condition_window_seconds = body.condition_window_seconds
        if body.action_type is not None:
            rule.action_type = body.action_type
        if body.webhook_url is not None:
            rule.webhook_url = body.webhook_url
        if body.cooldown_seconds is not None:
            rule.cooldown_seconds = body.cooldown_seconds

        rule.updated_at = datetime.now(UTC)
        await session.flush()

    return _serialize_rule(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification_rule(
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

    _require_admin(principal)

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
