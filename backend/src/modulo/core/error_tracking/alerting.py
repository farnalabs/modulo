"""Alert evaluation engine — sliding-window rule matching with cooldown.

One cooldown family (FAR-151 §15.8): ``(rule_id, fingerprint)`` cross-run
suppression plus ``(rule_id, fingerprint, run_id)`` per-run enumeration. Keys
follow the documented ``alert_cooldown:{org_id}:{rule_id}:{fingerprint}``
format (backend AGENTS.md lesson) and extend it with a ``:run:{run_id}`` suffix
for the per-run key. Signal-keyed rules (``rule.signal`` set) match only signal
events carrying the same signal; NULL-signal (legacy) rules keep matching by
level only.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.error_tracking.alert_dispatcher import dispatch_alert
from modulo.core.error_tracking.metrics import (
    record_alert_delivery_failed,
    record_alert_suppressed,
    record_error_alert,
)
from modulo.db.models.error_event import ErrorEvent
from modulo.db.models.error_group import ErrorGroup
from modulo.db.models.error_notification_rule import ErrorNotificationRule

_log = logging.getLogger(__name__)

_COOLDOWN_TTL = 86400  # 24 hours — max safe window for cooldown persistence

# Lifecycle event names carried on webhook/in-app deliveries.
ALERT_EVENT_OPENED = "error_alert"
ALERT_EVENT_RESOLVED = "alert_resolved"


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
    alert_id: uuid.UUID = field(default_factory=uuid.uuid4)
    signal: str | None = None
    elevation_signal: str | None = None
    attempt_n: int | None = None
    run_group_id: uuid.UUID | None = None


@dataclass(frozen=True)
class _CooldownKey:
    org_id: uuid.UUID
    rule_id: uuid.UUID
    fingerprint: str
    run_id: str | None = None

    def __str__(self) -> str:
        base = f"alert_cooldown:{self.org_id}:{self.rule_id}:{self.fingerprint}"
        if self.run_id is not None:
            return f"{base}:run:{self.run_id}"
        return base


class AlertEngine:
    """Evaluates error events against notification rules with cooldown.

    Cooldown state is persisted to Redis for multi-process deployments.
    """

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client
        self._in_memory_cooldown: dict[str, float] = {}

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
        signal: str | None = None,
        run_id: str | None = None,
        elevation_signal: str | None = None,
        attempt_n: int | None = None,
        run_group_id: uuid.UUID | None = None,
    ) -> list[TriggeredAlert]:
        """Evaluate all enabled rules for *org_id* and return triggered alerts.

        Cooldown: if the same rule+group fired within the rule's cooldown
        period, it is skipped.  Returns a list of ``TriggeredAlert`` that
        the caller should dispatch.
        """
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
            alert = await self._evaluate_rule(
                rule=rule,
                org_id=org_id,
                session=session,
                error_group_id=error_group_id,
                fingerprint=fingerprint,
                level=level,
                count=count,
                now=now,
                environment=environment,
                signal=signal,
                run_id=run_id,
                elevation_signal=elevation_signal,
                attempt_n=attempt_n,
                run_group_id=run_group_id,
            )
            if alert is not None:
                triggered.append(alert)

        return triggered

    async def _evaluate_rule(
        self,
        *,
        rule: ErrorNotificationRule,
        org_id: uuid.UUID,
        session: AsyncSession,
        error_group_id: uuid.UUID,
        fingerprint: str,
        level: str,
        count: int,
        now: float,
        environment: str | None,
        signal: str | None,
        run_id: str | None,
        elevation_signal: str | None,
        attempt_n: int | None,
        run_group_id: uuid.UUID | None,
    ) -> TriggeredAlert | None:
        """Evaluate a single rule. Returns a ``TriggeredAlert`` or ``None``.

        Encapsulates the per-rule control flow (signal/level match, window
        count, cooldown suppression, cooldown write) so ``evaluate`` stays a
        thin loop. Behavior is identical to the previous inline implementation.
        """
        if not self._rule_matches(rule, signal, level):
            return None

        min_count = rule.condition_min_count if rule.condition_min_count is not None else 0
        effective_count = await self._resolve_effective_count(
            rule=rule,
            session=session,
            org_id=org_id,
            fingerprint=fingerprint,
            count=count,
        )
        if effective_count < min_count:
            return None

        cross_key = _CooldownKey(org_id=org_id, rule_id=rule.id, fingerprint=fingerprint)
        run_key = (
            _CooldownKey(org_id=org_id, rule_id=rule.id, fingerprint=fingerprint, run_id=run_id)
            if run_id is not None
            else None
        )
        if await self._is_suppressed(rule=rule, now=now, cross_key=cross_key, run_key=run_key, run_id=run_id):
            return None

        await self._persist_firing(rule=rule, cross_key=cross_key, run_key=run_key, now=now)

        return self._build_triggered_alert(
            rule=rule,
            error_group_id=error_group_id,
            fingerprint=fingerprint,
            level=level,
            count=effective_count,
            environment=environment,
            signal=signal,
            elevation_signal=elevation_signal,
            attempt_n=attempt_n,
            run_group_id=run_group_id,
        )

    @staticmethod
    def _rule_matches(rule: ErrorNotificationRule, signal: str | None, level: str) -> bool:
        """Return whether *rule* fires for the incoming event.

        Signal-keyed rules (FAR-151 §15.8) fire only on a matching signal
        event and never on legacy (NULL-signal) events. Legacy rules match on
        the condition level.
        """
        rule_signal = getattr(rule, "signal", None)
        if isinstance(rule_signal, str):
            return signal is not None and rule_signal == signal
        return rule.condition_level == level

    async def _resolve_effective_count(
        self,
        *,
        rule: ErrorNotificationRule,
        session: AsyncSession,
        org_id: uuid.UUID,
        fingerprint: str,
        count: int,
    ) -> int:
        """Return the count used for the min-count check.

        Uses the sliding-window event count when the rule has a window,
        otherwise the raw incoming ``count``.
        """
        window_seconds = rule.condition_window_seconds or 0
        if window_seconds <= 0:
            return count
        try:
            return await self._count_events_in_window(
                session=session,
                org_id=org_id,
                fingerprint=fingerprint,
                window_seconds=window_seconds,
            )
        except Exception:
            _log.exception(
                "alert.window_count_failed",
                extra={"rule_id": str(rule.id), "window_seconds": window_seconds},
            )
            return -1

    async def _is_suppressed(
        self,
        *,
        rule: ErrorNotificationRule,
        now: float,
        cross_key: _CooldownKey,
        run_key: _CooldownKey | None,
        run_id: str | None,
    ) -> bool:
        """Return ``True`` if the rule is cooldown-suppressed for this event.

        Checks the cross-run key first, then (when present) the per-run key
        (FAR-151 §15.8). Records suppression and logs on each skip. A cooldown
        read failure is treated as "not suppressed" (fail-open).
        """
        cross_last = await self._safe_get_last_fired(rule, cross_key)
        if cross_last is not None and (now - cross_last) < rule.cooldown_seconds:
            _log.debug("alert.cooldown_skip", extra={"rule_id": str(rule.id), "fingerprint": cross_key.fingerprint})
            record_alert_suppressed(str(rule.id))
            return True

        if run_key is not None:
            run_last = await self._safe_get_last_fired(rule, run_key)
            if run_last is not None:
                # Per-run enumeration (FAR-151 §15.8): this run already fired
                # this rule+fingerprint — it must not fire again, regardless of
                # the cross-run window.
                _log.debug(
                    "alert.cooldown_run_skip",
                    extra={"rule_id": str(rule.id), "fingerprint": run_key.fingerprint, "run_id": run_id},
                )
                record_alert_suppressed(str(rule.id))
                return True

        return False

    async def _persist_firing(
        self,
        *,
        rule: ErrorNotificationRule,
        cross_key: _CooldownKey,
        run_key: _CooldownKey | None,
        now: float,
    ) -> None:
        """Record the firing time on the cross-run key and (when present) the per-run key."""
        try:
            await self._set_last_fired(cross_key, now)
        except Exception:
            _log.exception("alert.cooldown_write_failed", extra={"rule_id": str(rule.id)})
        if run_key is not None:
            try:
                await self._set_last_fired(run_key, now)
            except Exception:
                _log.exception("alert.cooldown_write_failed", extra={"rule_id": str(rule.id)})

    async def _safe_get_last_fired(self, rule: ErrorNotificationRule, key: _CooldownKey) -> float | None:
        """Read the last-fired time, treating a read failure as "none" (fail-open)."""
        try:
            return await self._get_last_fired(key)
        except Exception:
            _log.exception("alert.cooldown_read_failed", extra={"rule_id": str(rule.id)})
            return None

    @staticmethod
    def _build_triggered_alert(
        *,
        rule: ErrorNotificationRule,
        error_group_id: uuid.UUID,
        fingerprint: str,
        level: str,
        count: int,
        environment: str | None,
        signal: str | None,
        elevation_signal: str | None,
        attempt_n: int | None,
        run_group_id: uuid.UUID | None,
    ) -> TriggeredAlert:
        """Construct a ``TriggeredAlert`` from a firing rule and the event context."""
        return TriggeredAlert(
            rule_id=rule.id,
            rule_name=rule.name,
            action_type=rule.action_type,
            webhook_url=rule.webhook_url,
            error_group_id=error_group_id,
            fingerprint=fingerprint,
            level=level,
            count=count,
            environment=environment,
            signal=signal,
            elevation_signal=elevation_signal,
            attempt_n=attempt_n,
            run_group_id=run_group_id,
        )

    async def _count_events_in_window(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        fingerprint: str,
        window_seconds: int,
    ) -> int:
        """Count events matching *fingerprint* created within the last *window_seconds*.

        Returns zero if no events fall inside the window. The cutoff uses the
        application clock so it is consistent across rule evaluations.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
        result = await session.execute(
            select(func.count())
            .select_from(ErrorEvent)
            .where(
                ErrorEvent.organisation_id == org_id,
                ErrorEvent.fingerprint == fingerprint,
                ErrorEvent.created_at >= cutoff,
            ),
        )
        return int(result.scalar_one())

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
                record_alert_delivery_failed(str(alert.rule_id), alert.action_type)
                _log.exception(
                    "alert.dispatch_failed",
                    extra={"rule_id": str(alert.rule_id), "group_id": str(alert.error_group_id)},
                )

    async def _get_last_fired(self, key: _CooldownKey) -> float | None:
        val = self._in_memory_cooldown.get(str(key))
        if val is not None:
            return val
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
        return None

    async def _set_last_fired(self, key: _CooldownKey, value: float) -> None:
        self._in_memory_cooldown[str(key)] = value
        if self._redis is not None:
            try:
                await self._redis.setex(str(key), _COOLDOWN_TTL, json.dumps(value))
            except Exception:
                _log.exception("alert.cooldown_redis_set_failed", extra={"key": str(key)})
