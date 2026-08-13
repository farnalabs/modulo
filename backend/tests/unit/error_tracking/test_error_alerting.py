"""Tests for error alerting — AlertEngine, dispatch, CRUD, and max rules enforcement."""

from __future__ import annotations

import json
import uuid
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.models.error_notification_rule import ErrorNotificationRuleCreate, ErrorNotificationRuleUpdate
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.email_service import EmailSendingError
from modulo.core.error_tracking.alert_dispatcher import _format_slack_payload
from modulo.core.error_tracking.alerting import AlertEngine, TriggeredAlert, _CooldownKey
from modulo.db.models.error_notification_rule import ErrorNotificationRule

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_GROUP_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_RULE_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")


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


class _FakeSettings:
    """Minimal stand-in for ``Settings`` with the SMTP fields the dispatcher reads."""

    def __init__(
        self,
        *,
        smtp_host: str = "smtp.default.example.com",
        smtp_port: int = 587,
        smtp_username: str = "user",
        smtp_password: str = "pass",
        email_from: str = "alerts@example.com",
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        self.email_from = email_from

    def model_copy(self, update: dict[str, object] | None = None, **kwargs: object) -> _FakeSettings:
        merged = {
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_username": self.smtp_username,
            "smtp_password": self.smtp_password,
            "email_from": self.email_from,
        }
        merged.update(update or {})
        return _FakeSettings(**merged)  # type: ignore[arg-type]


def _make_dispatch_session(
    *,
    org: object | None = None,
    memberships: list[MagicMock] | None = None,
    accounts: list[MagicMock] | None = None,
) -> AsyncMock:
    """Session whose ``get(Organisation, ...)`` returns *org* and whose two
    ``execute`` calls (memberships, then active accounts) drain the queues."""
    session = _make_session()
    session.get = AsyncMock(return_value=org)
    result_queue = [memberships or [], accounts or []]

    def _execute(_stmt: object) -> MagicMock:
        return MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=result_queue.pop(0)))))

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _make_membership(account_id: uuid.UUID) -> MagicMock:
    m = MagicMock()
    m.account_id = account_id
    return m


def _make_account(email: str) -> MagicMock:
    a = MagicMock()
    a.email = email
    a.active = True
    return a


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

    async def test_webhook_payload_carries_contract_fields(self) -> None:
        """FAR-151 webhook payload must carry alert_id + group_id + elevation_signal
        + attempt_n + run_group_id so downstream consumers can correlate a fired
        alert with its run group and elevation."""
        import json

        from modulo.core.error_tracking.alert_dispatcher import _dispatch_webhook

        group_id = uuid.uuid4()
        run_group_id = uuid.uuid4()
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Test Webhook",
            action_type="webhook",
            webhook_url="https://example.com/hook",
            error_group_id=group_id,
            fingerprint="fp123",
            level="critical",
            count=1,
            environment="production",
            signal="agent.failed",
            elevation_signal="agent.failed",
            attempt_n=2,
            run_group_id=run_group_id,
        )

        captured: dict[str, object] = {}

        class _FakeResponse:
            is_success = True

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, **kwargs: object) -> _FakeResponse:
                captured["body"] = json.loads(kwargs["content"].decode())
                return _FakeResponse()

        with patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", _FakeClient):
            await _dispatch_webhook(alert, "Agent failed on retry", "/admin/errors/x")

        body = captured["body"]
        assert body["alert_id"] == str(alert.alert_id)
        assert body["group_id"] == str(group_id)
        assert body["elevation_signal"] == "agent.failed"
        assert body["attempt_n"] == 2
        assert body["run_group_id"] == str(run_group_id)

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


class TestDispatchWebhookNoUrl:
    async def test_webhook_without_url_warns_and_skips(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import dispatch_alert

        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="No URL",
            action_type="webhook",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        session = _make_session()

        with caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"):
            await dispatch_alert(_ORG_ID, alert, session)

        assert any("alert.webhook_no_url" in rec.message for rec in caplog.records)

    async def test_slack_webhook_payload_is_formatted_for_slack(self) -> None:
        from modulo.core.error_tracking.alert_dispatcher import _dispatch_webhook

        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Slack Rule",
            action_type="webhook",
            webhook_url="https://hooks.slack.com/services/T000/B000/XXXX",
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="critical",
            count=2,
            environment="prod",
        )
        captured: dict[str, object] = {}

        class _FakeResponse:
            is_success = True

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, **kwargs: object) -> _FakeResponse:
                captured["body"] = json.loads(kwargs["content"].decode())
                return _FakeResponse()

        with patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", _FakeClient):
            await _dispatch_webhook(alert, "Service down", "/admin/errors/x")

        body = captured["body"]
        assert "text" in body
        assert "\U0001f534" in body["text"]
        assert "Slack Rule" in body["text"]

    async def test_webhook_http_error_records_failed_delivery(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import _dispatch_webhook

        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="HTTP Fail",
            action_type="webhook",
            webhook_url="https://example.com/hook",
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )

        class _FakeResponse:
            is_success = False
            status_code = 500

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, **kwargs: object) -> _FakeResponse:
                return _FakeResponse()

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", _FakeClient),
            patch("modulo.core.error_tracking.alert_dispatcher.record_alert_delivery_failed") as record_failed,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await _dispatch_webhook(alert, "boom", "/admin/errors/x")

        record_failed.assert_called_once_with(str(alert.rule_id), "webhook")
        assert any("alert.webhook_http_error" in rec.message for rec in caplog.records)


# =========================================================================
# In-app dispatch
# =========================================================================


class TestDispatchInApp:
    async def test_in_app_records_delivery_log(self) -> None:
        from modulo.core.error_tracking.alert_dispatcher import dispatch_alert

        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="In App Rule",
            action_type="in_app",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        session = _make_session()
        error_group = MagicMock()
        error_group.sample_event = MagicMock()
        error_group.sample_event.message = "DB slow"

        await dispatch_alert(_ORG_ID, alert, session, error_group)

        session.add.assert_called_once()
        entry = session.add.call_args[0][0]
        assert entry.organisation_id == _ORG_ID
        assert entry.event_type == "error_alert"
        assert entry.status == "in_app"
        assert entry.last_error == "[error] In App Rule: DB slow (count=1)"

    async def test_unknown_action_type_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import dispatch_alert

        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Unknown",
            action_type="pagerduty",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        session = _make_session()

        with caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"):
            await dispatch_alert(_ORG_ID, alert, session)

        assert any("alert.unknown_action_type" in rec.message for rec in caplog.records)
        assert any(getattr(rec, "action_type", None) == "pagerduty" for rec in caplog.records)


# =========================================================================
# Email dispatch
# =========================================================================


class TestDispatchEmail:
    async def test_email_sends_to_active_admins_with_org_smtp(self) -> None:
        from modulo.core.error_tracking.alert_dispatcher import _dispatch_email

        org = MagicMock()
        org.settings_json = {
            "email": {"smtp_host": "smtp.org.example.com", "smtp_port": 465, "email_from": "org@example.com"}
        }
        account_id = uuid.uuid4()
        session = _make_dispatch_session(
            org=org,
            memberships=[_make_membership(account_id)],
            accounts=[_make_account("admin@example.com")],
        )
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Email Rule",
            action_type="email",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="critical",
            count=3,
            environment="prod",
        )

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_FakeSettings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as send_email,
        ):
            await _dispatch_email(_ORG_ID, alert, "sample <&> message", "/admin/errors/x", session)

        send_email.assert_called_once()
        effective_settings = send_email.call_args[0][0]
        to_emails = send_email.call_args[0][1]
        assert effective_settings.smtp_host == "smtp.org.example.com"
        assert effective_settings.smtp_port == 465
        assert effective_settings.email_from == "org@example.com"
        assert to_emails == ["admin@example.com"]
        subject = send_email.call_args[0][2]
        assert subject == "[Modulo Alert] critical: Email Rule"

    async def test_email_falls_back_to_settings_smtp_when_org_has_none(self) -> None:
        from modulo.core.error_tracking.alert_dispatcher import _dispatch_email

        org = MagicMock()
        org.settings_json = {}
        account_id = uuid.uuid4()
        session = _make_dispatch_session(
            org=org,
            memberships=[_make_membership(account_id)],
            accounts=[_make_account("admin@example.com")],
        )
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Fallback",
            action_type="email",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )

        with (
            patch(
                "modulo.core.error_tracking.alert_dispatcher.get_settings",
                return_value=_FakeSettings(smtp_host="smtp.default.example.com"),
            ),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as send_email,
        ):
            await _dispatch_email(_ORG_ID, alert, "msg", "/admin/errors/x", session)

        assert send_email.call_args[0][0].smtp_host == "smtp.default.example.com"

    async def test_email_no_smtp_host_warns_and_skips(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import _dispatch_email

        org = MagicMock()
        org.settings_json = {}
        session = _make_dispatch_session(org=org)

        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="No SMTP",
            action_type="email",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_FakeSettings(smtp_host="")),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as send_email,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await _dispatch_email(_ORG_ID, alert, "msg", "/admin/errors/x", session)

        send_email.assert_not_called()
        assert any("alert.email_disabled_no_smtp_host" in rec.message for rec in caplog.records)

    async def test_email_no_org_falls_back_to_settings_host(self) -> None:
        from modulo.core.error_tracking.alert_dispatcher import _dispatch_email

        session = _make_dispatch_session(
            org=None,
            memberships=[_make_membership(uuid.uuid4())],
            accounts=[_make_account("admin@example.com")],
        )
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="No Org",
            action_type="email",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )

        with (
            patch(
                "modulo.core.error_tracking.alert_dispatcher.get_settings",
                return_value=_FakeSettings(smtp_host="smtp.global.example.com"),
            ),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as send_email,
        ):
            await _dispatch_email(_ORG_ID, alert, "msg", "/admin/errors/x", session)

        assert send_email.call_args[0][0].smtp_host == "smtp.global.example.com"

    async def test_email_no_admins_warns_and_skips(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import _dispatch_email

        org = MagicMock()
        org.settings_json = {"email": {"smtp_host": "smtp.org.example.com"}}
        session = _make_dispatch_session(org=org, memberships=[])
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="No Admins",
            action_type="email",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_FakeSettings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as send_email,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await _dispatch_email(_ORG_ID, alert, "msg", "/admin/errors/x", session)

        send_email.assert_not_called()
        assert any("alert.email_no_admins" in rec.message for rec in caplog.records)

    async def test_email_no_active_admins_warns_and_skips(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import _dispatch_email

        org = MagicMock()
        org.settings_json = {"email": {"smtp_host": "smtp.org.example.com"}}
        session = _make_dispatch_session(org=org, memberships=[_make_membership(uuid.uuid4())], accounts=[])
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="No Active",
            action_type="email",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_FakeSettings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as send_email,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await _dispatch_email(_ORG_ID, alert, "msg", "/admin/errors/x", session)

        send_email.assert_not_called()
        assert any("alert.email_no_active_admins" in rec.message for rec in caplog.records)

    async def test_email_send_error_records_failed_delivery(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import _dispatch_email

        org = MagicMock()
        org.settings_json = {"email": {"smtp_host": "smtp.org.example.com"}}
        session = _make_dispatch_session(
            org=org,
            memberships=[_make_membership(uuid.uuid4())],
            accounts=[_make_account("admin@example.com")],
        )
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Send Fail",
            action_type="email",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_FakeSettings()),
            patch(
                "modulo.core.error_tracking.alert_dispatcher.send_email",
                side_effect=EmailSendingError("relay refused"),
            ),
            patch("modulo.core.error_tracking.alert_dispatcher.record_alert_delivery_failed") as record_failed,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await _dispatch_email(_ORG_ID, alert, "msg", "/admin/errors/x", session)

        record_failed.assert_called_once_with(str(alert.rule_id), "email")
        assert any("alert.email_send_failed" in rec.message for rec in caplog.records)

    async def test_email_unexpected_db_error_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import _dispatch_email

        session = _make_session()
        session.get = AsyncMock(side_effect=RuntimeError("db connection lost"))
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="DB Down",
            action_type="email",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_FakeSettings()),
            caplog.at_level("ERROR", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await _dispatch_email(_ORG_ID, alert, "msg", "/admin/errors/x", session)

        assert any("alert.email_dispatch_error" in rec.message for rec in caplog.records)

    async def test_dispatch_routes_email_action_type(self) -> None:
        from modulo.core.error_tracking.alert_dispatcher import dispatch_alert

        org = MagicMock()
        org.settings_json = {"email": {"smtp_host": "smtp.org.example.com"}}
        session = _make_dispatch_session(
            org=org,
            memberships=[_make_membership(uuid.uuid4())],
            accounts=[_make_account("admin@example.com")],
        )
        alert = TriggeredAlert(
            rule_id=uuid.uuid4(),
            rule_name="Via Dispatch",
            action_type="email",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        error_group = MagicMock()
        error_group.sample_event = MagicMock()
        error_group.sample_event.message = "DB slow"

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=_FakeSettings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as send_email,
        ):
            await dispatch_alert(_ORG_ID, alert, session, error_group)

        send_email.assert_called_once()
        assert send_email.call_args[0][1] == ["admin@example.com"]


# =========================================================================
# Resolved lifecycle event dispatch
# =========================================================================


class TestDispatchResolved:
    async def test_resolved_records_in_app_without_webhook(self) -> None:
        from modulo.core.error_tracking.alert_dispatcher import dispatch_alert_resolved

        session = _make_session()

        await dispatch_alert_resolved(
            _ORG_ID,
            group_id=_GROUP_ID,
            signal="agent.failed",
            reason="retried",
            session=session,
        )

        session.add.assert_called_once()
        entry = session.add.call_args[0][0]
        assert entry.event_type == "alert_resolved"
        assert entry.status == "in_app"
        assert entry.last_error == "agent.failed resolved: retried"

    async def test_resolved_posts_webhook_payload(self) -> None:
        from modulo.core.error_tracking.alert_dispatcher import dispatch_alert_resolved

        session = _make_session()
        captured: dict[str, object] = {}

        class _FakeResponse:
            is_success = True

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, **kwargs: object) -> _FakeResponse:
                captured["body"] = json.loads(kwargs["content"].decode())
                return _FakeResponse()

        with patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", _FakeClient):
            await dispatch_alert_resolved(
                _ORG_ID,
                group_id=_GROUP_ID,
                signal="agent.failed",
                reason="retried",
                session=session,
                webhook_url="https://example.com/resolved",
            )

        assert captured["body"]["event"] == "alert_resolved"
        assert captured["body"]["group_id"] == str(_GROUP_ID)
        assert captured["body"]["signal"] == "agent.failed"
        assert captured["body"]["reason"] == "retried"

    async def test_resolved_webhook_http_error_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import dispatch_alert_resolved

        session = _make_session()

        class _FakeResponse:
            is_success = False
            status_code = 502

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, **kwargs: object) -> _FakeResponse:
                return _FakeResponse()

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", _FakeClient),
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await dispatch_alert_resolved(
                _ORG_ID,
                group_id=_GROUP_ID,
                signal="agent.failed",
                reason="retried",
                session=session,
                webhook_url="https://example.com/resolved",
            )

        assert any("alert.resolved_webhook_http_error" in rec.message for rec in caplog.records)

    async def test_resolved_webhook_request_error_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.error_tracking.alert_dispatcher import dispatch_alert_resolved

        session = _make_session()

        class _FakeClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, **kwargs: object) -> None:
                raise httpx.RequestError("connection refused")

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", _FakeClient),
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await dispatch_alert_resolved(
                _ORG_ID,
                group_id=_GROUP_ID,
                signal="agent.failed",
                reason="retried",
                session=session,
                webhook_url="https://example.com/resolved",
            )

        assert any("alert.resolved_webhook_request_failed" in rec.message for rec in caplog.records)


# =========================================================================
# Cooldown persistence / error paths
# =========================================================================


class TestAlertEngineCooldownErrorPaths:
    async def test_cooldown_read_failure_triggers_alert(self, caplog: pytest.LogCaptureFixture) -> None:
        rule = _make_rule()
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules([rule])

        async def _boom(_key: object) -> float:
            raise RuntimeError("redis down")

        engine._get_last_fired = _boom  # type: ignore[assignment]

        with caplog.at_level("ERROR", logger="modulo.core.error_tracking.alerting"):
            alerts = await engine.evaluate(
                org_id=_ORG_ID,
                session=session,
                error_group_id=_GROUP_ID,
                fingerprint="abc123",
                level="error",
                count=1,
            )

        assert len(alerts) == 1
        assert any("alert.cooldown_read_failed" in rec.message for rec in caplog.records)

    async def test_cooldown_write_failure_still_triggers(self, caplog: pytest.LogCaptureFixture) -> None:
        rule = _make_rule()
        engine = AlertEngine(redis_client=MagicMock())
        session = _make_session_with_rules([rule])

        async def _boom(_key: object, _value: float) -> None:
            raise RuntimeError("redis down")

        engine._set_last_fired = _boom  # type: ignore[assignment]

        with caplog.at_level("ERROR", logger="modulo.core.error_tracking.alerting"):
            alerts = await engine.evaluate(
                org_id=_ORG_ID,
                session=session,
                error_group_id=_GROUP_ID,
                fingerprint="abc123",
                level="error",
                count=1,
            )

        assert len(alerts) == 1
        assert any("alert.cooldown_write_failed" in rec.message for rec in caplog.records)

    async def test_redis_cooldown_persists_and_reads_back(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=b"1000.0")
        redis.setex = AsyncMock()

        engine = AlertEngine(redis_client=redis)
        session = _make_session_with_rules([_make_rule()])

        alerts = await engine.evaluate(
            org_id=_ORG_ID,
            session=session,
            error_group_id=_GROUP_ID,
            fingerprint="abc123",
            level="error",
            count=1,
        )

        assert len(alerts) == 1
        redis.get.assert_awaited_once()
        redis.setex.assert_awaited_once()

    async def test_redis_cooldown_corrupt_json_falls_back_to_none(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=b"not-a-number")

        engine = AlertEngine(redis_client=redis)

        last_fired = await engine._get_last_fired(_CooldownKey(org_id=_ORG_ID, rule_id=_RULE_ID, fingerprint="abc"))

        assert last_fired is None

    async def test_redis_cooldown_get_failure_logs_and_falls_back(self, caplog: pytest.LogCaptureFixture) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(side_effect=RuntimeError("redis down"))

        engine = AlertEngine(redis_client=redis)

        with caplog.at_level("ERROR", logger="modulo.core.error_tracking.alerting"):
            last_fired = await engine._get_last_fired(_CooldownKey(org_id=_ORG_ID, rule_id=_RULE_ID, fingerprint="abc"))

        assert last_fired is None
        assert any("alert.cooldown_redis_get_failed" in rec.message for rec in caplog.records)

    async def test_redis_cooldown_set_failure_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        redis = MagicMock()
        redis.setex = AsyncMock(side_effect=RuntimeError("redis down"))

        engine = AlertEngine(redis_client=redis)

        with caplog.at_level("ERROR", logger="modulo.core.error_tracking.alerting"):
            await engine._set_last_fired(_CooldownKey(org_id=_ORG_ID, rule_id=_RULE_ID, fingerprint="abc"), 100.0)

        assert any("alert.cooldown_redis_set_failed" in rec.message for rec in caplog.records)


class TestDispatchAll:
    async def test_dispatches_each_alert(self) -> None:
        engine = AlertEngine(redis_client=MagicMock())
        alert = TriggeredAlert(
            rule_id=_RULE_ID,
            rule_name="Test Rule",
            action_type="in_app",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        session = _make_session()

        with (
            patch("modulo.core.error_tracking.alerting.record_error_alert") as mock_record,
            patch("modulo.core.error_tracking.alerting.dispatch_alert", new=AsyncMock()) as mock_dispatch,
        ):
            await engine.dispatch_all(_ORG_ID, [alert], session)

        mock_record.assert_called_once_with("error", "in_app")
        mock_dispatch.assert_awaited_once()

    async def test_swallows_dispatch_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = AlertEngine(redis_client=MagicMock())
        alert = TriggeredAlert(
            rule_id=_RULE_ID,
            rule_name="Test Rule",
            action_type="in_app",
            webhook_url=None,
            error_group_id=_GROUP_ID,
            fingerprint="fp",
            level="error",
            count=1,
        )
        session = _make_session()

        with (
            patch("modulo.core.error_tracking.alerting.record_error_alert") as mock_record,
            patch(
                "modulo.core.error_tracking.alerting.dispatch_alert",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            caplog.at_level("ERROR", logger="modulo.core.error_tracking.alerting"),
        ):
            await engine.dispatch_all(_ORG_ID, [alert], session)

        mock_record.assert_called_once_with("error", "in_app")
        assert any("alert.dispatch_failed" in rec.message for rec in caplog.records)


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
