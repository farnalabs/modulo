"""Tests for error alerting — AlertEngine, dispatch, CRUD, and max rules enforcement."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.models.error_notification_rule import ErrorNotificationRuleCreate, ErrorNotificationRuleUpdate
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.error_tracking.alert_dispatcher import _format_slack_payload
from modulo.core.error_tracking.alerting import AlertEngine, TriggeredAlert
from modulo.db.models.error_notification_rule import ErrorNotificationRule

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_GROUP_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _make_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


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
    for k, v in overrides.items():
        setattr(rule, k, v)
    return rule


def _make_session_with_rules(
    rules: list[MagicMock],
    *,
    window_counts: list[int] | None = None,
) -> AsyncMock:
    """Build a session whose first ``execute`` returns *rules* and subsequent
    ``execute`` calls return window event counts from *window_counts*.

    Rules are returned first because ``evaluate()`` loads rules before it
    computes per-rule window counts.
    """
    session = _make_session()
    queue = list(window_counts or [])
    rules_result = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=rules))))
    rules_loaded = False

    def _execute(_stmt: object) -> MagicMock:
        nonlocal rules_loaded
        if not rules_loaded:
            rules_loaded = True
            return rules_result
        if queue:
            return MagicMock(scalar_one=MagicMock(return_value=queue.pop(0)))
        return rules_result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _make_rules_app_with_count(rule_count: int) -> FastAPI:
    """Build the notification-rules router against a session reporting *rule_count* existing rules."""
    from modulo.api.routes.error_notification_rules import router as rules_router

    app = FastAPI()
    app.include_router(rules_router)

    session = MagicMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__.return_value = session
    begin_cm.__aexit__.return_value = None
    session.begin.return_value = begin_cm
    session.info = {}
    count_result = MagicMock()
    count_result.scalar_one.return_value = rule_count
    session.execute = AsyncMock(return_value=count_result)
    session.flush = AsyncMock()
    session.add = MagicMock()

    async def _override_db() -> MagicMock:
        return session

    async def _override_user() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=uuid.uuid4(),
            org_role="admin",
        )

    from modulo.api.dependencies import get_db_session
    from modulo.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db_session] = _override_db
    return app


# =========================================================================
# Alert evaluation — parametrized
# =========================================================================


class TestAlertEngineEvaluate:
    """Parametrized: evaluate scenarios across level, count, rules combinations."""

    @pytest.mark.parametrize(
        ("rules", "level", "count", "expected_alerts", "expected_action_type"),
        [
            pytest.param([_make_rule()], "error", 1, 1, "in_app", id="level_match_triggers_alert"),
            pytest.param([_make_rule()], "warning", 1, 0, None, id="level_mismatch_skips"),
            pytest.param([_make_rule(condition_min_count=5)], "error", 3, 0, None, id="count_below_threshold_skips"),
            pytest.param(
                [_make_rule(condition_min_count=5)], "error", 5, 1, "in_app", id="count_meets_threshold_triggers"
            ),
            pytest.param(
                [_make_rule(name="Rule A"), _make_rule(name="Rule B")],
                "error",
                1,
                2,
                None,
                id="multiple_rules_all_triggered",
            ),
            pytest.param([_make_rule(enabled=False)], "error", 1, 0, None, id="disabled_rule_not_evaluated"),
            pytest.param([], "error", 1, 0, None, id="no_rules_for_org_returns_empty"),
        ],
    )
    async def test_evaluate(
        self,
        rules: list[MagicMock],
        level: str,
        count: int,
        expected_alerts: int,
        expected_action_type: str | None,
    ) -> None:
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules(rules)

        alerts = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level=level,
            count=count,
        )
        assert len(alerts) == expected_alerts
        if expected_alerts > 0 and expected_action_type:
            assert alerts[0].action_type == expected_action_type


# =========================================================================
# Condition window — time-boxed event counting
# =========================================================================


class TestConditionWindow:
    """condition_window_seconds filters alerts on events within the window."""

    async def test_window_count_meets_threshold_triggers(self) -> None:
        rule = _make_rule(condition_min_count=5, condition_window_seconds=300)
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules([rule], window_counts=[5])

        alerts = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=1,
        )
        assert len(alerts) == 1
        assert alerts[0].count == 5

    async def test_window_count_below_threshold_skips(self) -> None:
        rule = _make_rule(condition_min_count=5, condition_window_seconds=300)
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules([rule], window_counts=[4])

        alerts = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=10,
        )
        assert len(alerts) == 0

    async def test_window_zero_falls_back_to_lifetime_count(self) -> None:
        rule = _make_rule(condition_min_count=3, condition_window_seconds=0)
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules([rule])

        alerts = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=3,
        )
        assert len(alerts) == 1
        assert alerts[0].count == 3

    async def test_window_query_failure_skips_rule(self) -> None:
        rule = _make_rule(condition_min_count=1, condition_window_seconds=300)
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session()
        rules_result = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[rule]))))
        rules_loaded = False

        def _execute(_stmt: object) -> MagicMock:
            nonlocal rules_loaded
            if not rules_loaded:
                rules_loaded = True
                return rules_result
            raise RuntimeError("db unavailable")

        session.execute = AsyncMock(side_effect=_execute)

        alerts = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=5,
        )
        assert len(alerts) == 0

    async def test_window_applied_per_rule_independently(self) -> None:
        windowed = _make_rule(name="Windowed", condition_min_count=2, condition_window_seconds=300)
        lifetime = _make_rule(name="Lifetime", condition_min_count=2, condition_window_seconds=0)
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules([windowed, lifetime], window_counts=[1])

        alerts = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=2,
        )
        assert len(alerts) == 1
        assert alerts[0].rule_name == "Lifetime"

    async def test_window_query_is_org_and_fingerprint_scoped(self) -> None:
        rule = _make_rule(condition_min_count=1, condition_window_seconds=300)
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules([rule], window_counts=[1])

        await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=1,
        )

        stmt = session.execute.await_args.args[0]
        compiled = str(stmt)
        assert "error_events" in compiled
        assert "fingerprint" in compiled
        assert "organisation_id" in compiled


# =========================================================================
# Cooldown
# =========================================================================


class TestAlertEngineCooldown:
    async def test_same_rule_group_does_not_duplicate_within_cooldown(self) -> None:
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules([_make_rule()])

        alerts1 = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=1,
        )
        assert len(alerts1) == 1

        alerts2 = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=1,
        )
        assert len(alerts2) == 0

    async def test_different_group_not_affected_by_cooldown(self) -> None:
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules([_make_rule()])
        other_group_id = uuid.UUID("00000000-0000-0000-0000-000000000099")

        await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=1,
        )

        alerts = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=other_group_id,
            fingerprint="def456",
            level="error",
            count=1,
        )
        assert len(alerts) == 1

    async def test_different_rule_not_affected_by_cooldown(self) -> None:
        r1 = _make_rule(name="Rule A", condition_min_count=1)
        r2 = _make_rule(name="Rule B", condition_min_count=5)
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules([r1, r2])

        await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=1,
        )

        alerts = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=5,
        )
        assert len(alerts) == 1
        assert alerts[0].rule_name == "Rule B"


# =========================================================================
# Dispatch
# =========================================================================


class TestDispatchWebhook:
    async def test_slack_format(self) -> None:
        result = _format_slack_payload(
            payload={
                "rule": "Critical Alert",
                "group_id": "g-123",
                "level": "critical",
                "count": 5,
                "message": "DB down",
                "environment": "production",
                "url": "/admin/errors/g-123",
            },
            emoji="\U0001f534",
        )
        assert "\U0001f534" in result["text"]
        assert "Critical Alert" in result["text"]
        assert "g-123" in result["text"]

    async def test_webhook_payload_structure(self) -> None:
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Test Webhook",
            action_type="webhook",
            webhook_url="https://example.com/hook",
            error_group_id=_GROUP_ID,
            fingerprint="fp123",
            level="error",
            count=3,
            environment="staging",
        )

        from modulo.core.error_tracking.alert_dispatcher import _build_summary

        summary = _build_summary(alert, "something went wrong")
        assert "[error]" in summary
        assert "Test Webhook" in summary
        assert "count=3" in summary

    async def test_dispatch_swallows_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import dispatch_alert

        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Fail Rule",
            action_type="webhook",
            webhook_url="https://invalid.url/that/will/fail",
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        session = _make_session()
        error_group = MagicMock()
        error_group.sample_event = None

        with caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"):
            await dispatch_alert(_ORG_ID, alert, session, error_group)

        assert any("alert.webhook_request_failed" in rec.message for rec in caplog.records)


# =========================================================================
# Max rules enforcement (CRUD patterns)
# =========================================================================


class TestCRUDRules:
    async def test_create_rule_schema_valid(self) -> None:
        body = ErrorNotificationRuleCreate(
            name="My Rule",
            condition_level="critical",
            action_type="webhook",
            webhook_url="https://hooks.example.com/alert",
        )
        assert body.name == "My Rule"
        assert body.condition_level == "critical"
        assert body.action_type == "webhook"

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            pytest.param("condition_level", "debug", "condition_level must be one of", id="invalid_level"),
            pytest.param("action_type", "sms", "action_type must be one of", id="invalid_action"),
            pytest.param("webhook_url", "ftp://bad.com", "webhook_url must start with", id="invalid_webhook_url"),
        ],
    )
    async def test_create_rule_invalid_field(self, field: str, value: object, match: str) -> None:
        kwargs: dict = {"name": "Bad", field: value}
        if field == "webhook_url":
            kwargs["action_type"] = "webhook"
        with pytest.raises(ValidationError, match=match):
            ErrorNotificationRuleCreate(**kwargs)

    async def test_update_rule_partial(self) -> None:
        body = ErrorNotificationRuleUpdate(name="Renamed")
        assert body.name == "Renamed"
        assert body.enabled is None

    def test_create_rule_rejects_when_at_max(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(_make_rules_app_with_count(10))
        resp = client.post(
            "/api/v1/errors/notification-rules",
            json={"name": "Rule 11", "condition_level": "error", "action_type": "in_app"},
        )
        assert resp.status_code == 422

    def test_create_rule_allows_below_max(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(_make_rules_app_with_count(9))
        resp = client.post(
            "/api/v1/errors/notification-rules",
            json={"name": "Rule 10", "condition_level": "error", "action_type": "in_app"},
        )
        assert resp.status_code == 201


# =========================================================================
# Models
# =========================================================================


class TestErrorNotificationRuleModel:
    def test_required_columns_exist(self) -> None:
        cols = {c.name: c for c in ErrorNotificationRule.__table__.columns}
        for col_name in (
            "name",
            "enabled",
            "condition_level",
            "condition_min_count",
            "condition_window_seconds",
            "action_type",
            "cooldown_seconds",
            "webhook_url",
            "organisation_id",
            "id",
            "created_at",
            "updated_at",
        ):
            assert col_name in cols, f"Missing column: {col_name}"

    def test_check_constraints_exist(self) -> None:
        constraints = list(ErrorNotificationRule.__table__.constraints)
        constraint_names = {c.name for c in constraints if hasattr(c, "name") and c.name}
        assert "ck_enr_condition_level" in constraint_names
        assert "ck_enr_action_type" in constraint_names

    def test_defaults(self) -> None:
        assert ErrorNotificationRule.__table__.columns["enabled"].server_default is not None
        assert ErrorNotificationRule.__table__.columns["condition_min_count"].server_default is not None
        assert ErrorNotificationRule.__table__.columns["condition_window_seconds"].server_default is not None
        assert ErrorNotificationRule.__table__.columns["cooldown_seconds"].server_default is not None
