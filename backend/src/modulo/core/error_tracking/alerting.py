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
from modulo.core.error_tracking.metrics import record_error_alert
from modulo.db.models.error_group import ErrorGroup
from modulo.db.models.error_notification_rule import ErrorNotificationRule

_log = logging.getLogger(__name__)

_COOLDOWN_TTL = 86400  # 24 hours — max safe window for cooldown persistence


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
    fingerprint: str

    def __str__(self) -> str:
        return f"cooldown:{self.rule_id}:{self.fingerprint}"


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
                "AlertEngine: No Redis client — cooldown state is in-memory only (not shared across processes)",
            )

    def _evict_expired_cooldowns(self) -> None:
        now = time.time()
        expired = [k for k, v in self._cooldowns.items() if (now - v) >= _COOLDOWN_TTL]
        for k in expired:
            self._cooldowns.pop(k, None)

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
        self._evict_expired_cooldowns()
        result = await session.execute(
            select(ErrorNotificationRule).where(
                ErrorNotificationRule.organisation_id == org_id,
            ),
        )
        raw_rules: list[ErrorNotificationRule] = list(result.scalars().all())
        rules = [r for r in raw_rules if r.enabled]

        triggered: list[TriggeredAlert] = []
        now = time.time()

        for rule in rules:
            if rule.condition_level != level:
                continue
            min_count = rule.condition_min_count if rule.condition_min_count is not None else 0
            if count < min_count:
                continue

            ck = _CooldownKey(rule_id=rule.id, fingerprint=fingerprint)
            try:
                last_fired = await self._get_last_fired(ck)
            except Exception:
                _log.exception("alert.cooldown_read_failed", extra={"rule_id": str(rule.id)})
                last_fired = None
            if last_fired is not None and (now - last_fired) < rule.cooldown_seconds:
                _log.debug(
                    "alert.cooldown_skip",
                    extra={"rule_id": str(rule.id), "fingerprint": fingerprint},
                )
                continue

            try:
                await self._set_last_fired(ck, now)
            except Exception:
                _log.exception("alert.cooldown_write_failed", extra={"rule_id": str(rule.id)})

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
                ),
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
                record_error_alert(alert.level, alert.action_type)
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
            try:
                raw = await self._redis.get(str(key))
                if raw:
                    try:
                        return float(json.loads(raw))
                    except (ValueError, TypeError):
                        return None
            except Exception:
                _log.exception("alert.cooldown_redis_get_failed", extra={"key": str(key)})
        return self._cooldowns.get(key)

    async def _set_last_fired(self, key: _CooldownKey, value: float) -> None:
        if self._redis is not None:
            try:
                await self._redis.setex(str(key), _COOLDOWN_TTL, json.dumps(value))
            except Exception:
                _log.exception("alert.cooldown_redis_set_failed", extra={"key": str(key)})
        self._cooldowns[key] = value
