"""Alert evaluation engine — sliding-window rule matching with cooldown."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.error_tracking.alert_dispatcher import dispatch_alert
from modulo.db.models.error_group import ErrorGroup
from modulo.db.models.error_notification_rule import ErrorNotificationRule

_log = logging.getLogger(__name__)


@dataclass
class TriggeredAlert:
    rule_id: uuid.UUID
    rule_name: str
    action_type: str
    webhook_url: str | None
    error_group_id: uuid.UUID
    fingerprint: str
    level: str
    count: int
    environment: str | None = None


@dataclass(frozen=True)
class _CooldownKey:
    rule_id: uuid.UUID
    group_id: uuid.UUID

    def __str__(self) -> str:
        return f"cooldown:{self.rule_id}:{self.group_id}"


class AlertEngine:
    """Evaluates error events against notification rules with cooldown.

    Cooldown is in-memory by default. When ``redis_client`` is provided,
    cooldown state is persisted to Redis for multi-process deployments.
    """

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client
        self._cooldowns: dict[_CooldownKey, float] = {}
        if redis_client is None:
            _log.warning(
                "AlertEngine: No Redis client — cooldown state is in-memory only (not shared across processes)"
            )

    async def evaluate(
        self,
        org_id: uuid.UUID,
        session: AsyncSession,
        error_group_id: uuid.UUID,
        fingerprint: str,
        level: str,
        count: int,
        *,
        environment: str | None = None,
    ) -> list[TriggeredAlert]:
        """Evaluate all enabled rules for *org_id* and return triggered alerts.

        Cooldown: if the same rule+group fired within the rule's cooldown
        period, it is skipped.  Returns a list of ``TriggeredAlert`` that
        the caller should dispatch.
        """
        result = await session.execute(
            select(ErrorNotificationRule).where(
                ErrorNotificationRule.organisation_id == org_id,
            )
        )
        raw_rules: list[ErrorNotificationRule] = list(result.scalars().all())
        rules = [r for r in raw_rules if r.enabled]

        triggered: list[TriggeredAlert] = []
        now = time.time()

        for rule in rules:
            if rule.condition_level != level:
                continue
            if count < rule.condition_min_count:
                continue

            ck = _CooldownKey(rule_id=rule.id, group_id=error_group_id)
            last_fired = await self._get_last_fired(ck)
            if last_fired is not None and (now - last_fired) < rule.cooldown_seconds:
                _log.debug(
                    "alert.cooldown_skip",
                    extra={"rule_id": str(rule.id), "group_id": str(error_group_id)},
                )
                continue

            await self._set_last_fired(ck, now)

            triggered.append(
                TriggeredAlert(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    action_type=rule.action_type,
                    webhook_url=rule.webhook_url,
                    error_group_id=error_group_id,
                    fingerprint=fingerprint,
                    level=level,
                    count=count,
                    environment=environment,
                )
            )

        return triggered

    async def dispatch_all(
        self,
        org_id: uuid.UUID,
        alerts: list[TriggeredAlert],
        session: AsyncSession,
        error_group: ErrorGroup | None = None,
    ) -> None:
        """Dispatch a list of triggered alerts, swallowing per-alert failures."""
        for alert in alerts:
            try:
                await dispatch_alert(
                    org_id=org_id,
                    alert=alert,
                    session=session,
                    error_group=error_group,
                )
            except Exception:
                _log.exception(
                    "alert.dispatch_failed",
                    extra={"rule_id": str(alert.rule_id), "group_id": str(alert.error_group_id)},
                )

    async def _get_last_fired(self, key: _CooldownKey) -> float | None:
        if self._redis is not None:
            raw = await self._redis.get(str(key))
            if raw:
                try:
                    return json.loads(raw)
                except (ValueError, TypeError):
                    return None
            return None
        return self._cooldowns.get(key)

    async def _set_last_fired(self, key: _CooldownKey, value: float) -> None:
        if self._redis is not None:
            await self._redis.setex(str(key), 86400, json.dumps(value))
        else:
            self._cooldowns[key] = value
