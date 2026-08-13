"""Tests for the FAR-151 cooldown family and per-signal rule matching.

The cooldown family (§15.8) is ``(rule_id, fingerprint)`` cross-run suppression
plus ``(rule_id, fingerprint, run_id)`` per-run enumeration, using the documented
``alert_cooldown:{org_id}:{rule_id}:{fingerprint}`` key format (backend AGENTS.md
lesson) extended with a ``:run:{run_id}`` suffix. Signal-keyed rules fire only on
matching signal events; NULL-signal (legacy) rules keep matching by level.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.error_tracking.alerting import AlertEngine, _CooldownKey
from modulo.db.models.error_notification_rule import ErrorNotificationRule

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_GROUP_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _make_rule(**overrides: object) -> MagicMock:
    rule = MagicMock(spec=ErrorNotificationRule)
    rule.id = uuid.uuid4()
    rule.organisation_id = _ORG_ID
    rule.name = "Test Rule"
    rule.enabled = True
    rule.condition_level = "error"
    rule.condition_min_count = 1
    rule.condition_window_seconds = 0
    rule.action_type = "in_app"
    rule.webhook_url = None
    rule.cooldown_seconds = 300
    rule.signal = None
    for k, v in overrides.items():
        setattr(rule, k, v)
    return rule


def _session_with_rules(rules: list[MagicMock]) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    rules_result = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=rules))))
    session.execute = AsyncMock(return_value=rules_result)
    return session


class TestCooldownKeyFormat:
    def test_cross_run_key_matches_documented_format(self) -> None:
        rule_id = uuid.uuid4()
        key = _CooldownKey(org_id=_ORG_ID, rule_id=rule_id, fingerprint="fp123")
        assert str(key) == f"alert_cooldown:{_ORG_ID}:{rule_id}:fp123"

    def test_per_run_key_extends_cross_run_key(self) -> None:
        rule_id = uuid.uuid4()
        key = _CooldownKey(org_id=_ORG_ID, rule_id=rule_id, fingerprint="fp123", run_id="run-1")
        assert str(key) == f"alert_cooldown:{_ORG_ID}:{rule_id}:fp123:run:run-1"

    def test_keys_are_org_scoped(self) -> None:
        """The cooldown key includes org_id so two orgs never share a cooldown."""
        rule_id = uuid.uuid4()
        k1 = _CooldownKey(org_id=_ORG_ID, rule_id=rule_id, fingerprint="fp")
        k2 = _CooldownKey(org_id=uuid.uuid4(), rule_id=rule_id, fingerprint="fp")
        assert str(k1) != str(k2)


class TestCrossRunCooldown:
    async def test_same_fingerprint_suppressed_within_cooldown(self) -> None:
        rule = _make_rule()
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        alerts1 = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1)
        assert len(alerts1) == 1
        alerts2 = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1)
        assert len(alerts2) == 0

    async def test_different_fingerprint_not_suppressed(self) -> None:
        rule = _make_rule()
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1)
        alerts = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp456", "error", 1)
        assert len(alerts) == 1

    async def test_suppression_fires_metric(self) -> None:
        from modulo.core.error_tracking import alerting as alerting_mod

        rule = _make_rule()
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        with patch.object(alerting_mod, "record_alert_suppressed") as suppressed:
            await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1)
            await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1)
            suppressed.assert_called_once_with(str(rule.id))

    async def test_no_suppression_metric_on_first_fire(self) -> None:
        from modulo.core.error_tracking import alerting as alerting_mod

        rule = _make_rule()
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        with patch.object(alerting_mod, "record_alert_suppressed") as suppressed:
            await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1)
            suppressed.assert_not_called()


class TestPerRunEnumeration:
    async def test_same_run_does_not_refire_but_other_run_does(self) -> None:
        """Within one run a rule fires once; a different run fires again.

        ``cooldown_seconds=0`` isolates the per-run enumeration from the
        cross-run window — the per-run key is an absolute fire-once per run.
        """
        rule = _make_rule(cooldown_seconds=0)
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        a1 = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1, run_id="run-A")
        assert len(a1) == 1
        a2 = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1, run_id="run-A")
        assert len(a2) == 0
        b1 = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1, run_id="run-B")
        assert len(b1) == 1

    async def test_per_run_suppression_fires_metric(self) -> None:
        from modulo.core.error_tracking import alerting as alerting_mod

        rule = _make_rule(cooldown_seconds=0)
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        with patch.object(alerting_mod, "record_alert_suppressed") as suppressed:
            await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1, run_id="run-A")
            await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "error", 1, run_id="run-A")
            suppressed.assert_called_once_with(str(rule.id))


class TestSignalRuleMatching:
    async def test_signal_rule_fires_on_matching_signal(self) -> None:
        rule = _make_rule(signal="agent.failed")
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        alerts = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "critical", 1, signal="agent.failed")
        assert len(alerts) == 1

    async def test_signal_rule_ignores_other_signal(self) -> None:
        rule = _make_rule(signal="agent.failed")
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        alerts = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "critical", 1, signal="agent.no_op")
        assert len(alerts) == 0

    async def test_signal_rule_never_fires_on_legacy_event(self) -> None:
        """A signal-keyed rule must never fire on an event with no signal."""
        rule = _make_rule(signal="agent.failed")
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        alerts = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "critical", 1)
        assert len(alerts) == 0

    async def test_legacy_rule_fires_on_signal_event_by_level(self) -> None:
        """NULL-signal (legacy) rules keep matching signal events by level."""
        rule = _make_rule(signal=None, condition_level="critical")
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        alerts = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "critical", 1, signal="agent.failed")
        assert len(alerts) == 1

    async def test_signal_rule_level_does_not_gate_matching(self) -> None:
        """The signal match is primary — the rule fires regardless of the
        event's level once the signal matches (the level is derived from the
        registry severity for signal events)."""
        rule = _make_rule(signal="agent.failed", condition_level="warning")
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        alerts = await engine.evaluate(_ORG_ID, session, _GROUP_ID, "fp123", "critical", 1, signal="agent.failed")
        assert len(alerts) == 1

    async def test_alert_carries_signal_context_fields(self) -> None:
        rule = _make_rule(signal="agent.failed")
        engine = AlertEngine(redis_client=None)
        session = _session_with_rules([rule])

        alerts = await engine.evaluate(
            _ORG_ID,
            session,
            _GROUP_ID,
            "fp123",
            "critical",
            1,
            signal="agent.failed",
            run_id="run-9",
            elevation_signal="agent.failed",
            attempt_n=2,
            run_group_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        )
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.signal == "agent.failed"
        assert alert.elevation_signal == "agent.failed"
        assert alert.attempt_n == 2
        assert str(alert.run_group_id) == "00000000-0000-0000-0000-000000000099"
        assert alert.alert_id is not None
