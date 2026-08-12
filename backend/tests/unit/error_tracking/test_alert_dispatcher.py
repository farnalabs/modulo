"""Tests for alert dispatch — in_app, email, and webhook delivery paths."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.error_tracking.alert_dispatcher import (
    _escape_html,
    _format_slack_payload,
    dispatch_alert,
)
from modulo.core.error_tracking.alerting import TriggeredAlert

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_GROUP_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_RULE_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")


def _make_alert(**overrides: object) -> TriggeredAlert:
    defaults: dict[str, object] = {
        "rule_id": _RULE_ID,
        "rule_name": "Test Rule",
        "action_type": "in_app",
        "webhook_url": None,
        "error_group_id": _GROUP_ID,
        "fingerprint": "fp123",
        "level": "error",
        "count": 3,
        "environment": "production",
    }
    defaults.update(overrides)
    return TriggeredAlert(**defaults)  # type: ignore[arg-type]


def _make_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _make_error_group(sample_message: str | None = None) -> MagicMock:
    group = MagicMock()
    if sample_message is None:
        group.sample_event = None
    else:
        event = MagicMock()
        event.message = sample_message
        group.sample_event = event
    return group


# =========================================================================
# dispatch_alert routing
# =========================================================================


class TestDispatchAlertRouting:
    async def test_unknown_action_type_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        alert = _make_alert(action_type="sms")
        session = _make_session()

        with caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"):
            await dispatch_alert(_ORG_ID, alert, session)

        assert any("alert.unknown_action_type" in rec.message for rec in caplog.records)

    async def test_in_app_dispatch_when_no_error_group(self) -> None:
        alert = _make_alert(action_type="in_app")
        session = _make_session()

        await dispatch_alert(_ORG_ID, alert, session)

        session.add.assert_called_once()
        entry = session.add.call_args.args[0]
        assert entry.event_type == "error_alert"
        assert entry.status == "in_app"
        assert entry.attempt_count == 1
        assert entry.organisation_id == _ORG_ID

    async def test_email_dispatch_routes_to_send_email(self) -> None:
        alert = _make_alert(action_type="email")
        session = _make_session()
        org = MagicMock()
        org.settings_json = {}
        session.get = AsyncMock(return_value=org)
        membership = MagicMock()
        membership.account_id = uuid.uuid4()
        account = MagicMock()
        account.email = "admin@example.com"
        account.active = True
        memberships_result = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[membership])))
        )
        accounts_result = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[account]))))
        session.execute = AsyncMock(side_effect=[memberships_result, accounts_result])

        settings = MagicMock()
        settings.smtp_host = "smtp.example.com"
        settings.smtp_port = 587
        settings.smtp_username = "user"
        settings.smtp_password = "pass"
        settings.email_from = "noreply@example.com"
        settings.model_copy = MagicMock(return_value=settings)

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as mock_send,
        ):
            await dispatch_alert(_ORG_ID, alert, session, _make_error_group("boom"))

        to_emails = mock_send.call_args.args[1]
        assert to_emails == ["admin@example.com"]


# =========================================================================
# In-app dispatch
# =========================================================================


class TestDispatchInApp:
    async def test_entry_uses_sample_message_summary(self) -> None:
        alert = _make_alert(action_type="in_app")
        session = _make_session()

        await dispatch_alert(_ORG_ID, alert, session, _make_error_group("sample failure"))

        entry = session.add.call_args.args[0]
        assert "[error] Test Rule: sample failure" in entry.last_error
        assert "count=3" in entry.last_error


# =========================================================================
# Email dispatch
# =========================================================================


class TestDispatchEmail:
    def _make_email_session(
        self, *, memberships: list[MagicMock] | None = None, accounts: list[MagicMock] | None = None
    ) -> AsyncMock:
        session = _make_session()
        org = MagicMock()
        org.settings_json = {"email": {"smtp_host": "org-smtp.example.com"}}
        session.get = AsyncMock(return_value=org)
        memberships_result = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=memberships or [])))
        )
        accounts_result = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=accounts or [])))
        )
        session.execute = AsyncMock(side_effect=[memberships_result, accounts_result])
        return session

    def _make_settings(self) -> MagicMock:
        settings = MagicMock()
        settings.smtp_host = "fallback-smtp.example.com"
        settings.smtp_port = 587
        settings.smtp_username = "user"
        settings.smtp_password = "pass"
        settings.email_from = "noreply@example.com"
        settings.model_copy = MagicMock(side_effect=lambda **kwargs: settings)
        return settings

    async def test_no_org_uses_settings_smtp_host(self) -> None:
        alert = _make_alert(action_type="email")
        session = _make_session()
        session.get = AsyncMock(return_value=None)
        membership = MagicMock()
        membership.account_id = uuid.uuid4()
        account = MagicMock()
        account.email = "a@example.com"
        account.active = True
        memberships_result = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[membership])))
        )
        accounts_result = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[account]))))
        session.execute = AsyncMock(side_effect=[memberships_result, accounts_result])

        settings = self._make_settings()

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as mock_send,
        ):
            await dispatch_alert(_ORG_ID, alert, session)

        effective = settings.model_copy.call_args.kwargs["update"]
        assert effective["smtp_host"] == "fallback-smtp.example.com"
        assert mock_send.call_args.args[1] == ["a@example.com"]

    async def test_no_effective_smtp_host_skips_send(self, caplog: pytest.LogCaptureFixture) -> None:
        alert = _make_alert(action_type="email")
        session = _make_session()
        org = MagicMock()
        org.settings_json = {}
        session.get = AsyncMock(return_value=org)
        settings = self._make_settings()
        settings.smtp_host = ""

        with (
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as mock_send,
        ):
            await dispatch_alert(_ORG_ID, alert, session)

        mock_send.assert_not_called()
        assert any("alert.email_disabled_no_smtp_host" in rec.message for rec in caplog.records)

    async def test_no_admins_skips_send(self, caplog: pytest.LogCaptureFixture) -> None:
        alert = _make_alert(action_type="email")
        session = self._make_email_session(memberships=[])

        with (
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=self._make_settings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as mock_send,
        ):
            await dispatch_alert(_ORG_ID, alert, session)

        mock_send.assert_not_called()
        assert any("alert.email_no_admins" in rec.message for rec in caplog.records)

    async def test_no_active_admins_skips_send(self, caplog: pytest.LogCaptureFixture) -> None:
        alert = _make_alert(action_type="email")
        session = self._make_email_session(memberships=[MagicMock(account_id=uuid.uuid4())], accounts=[])

        with (
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=self._make_settings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as mock_send,
        ):
            await dispatch_alert(_ORG_ID, alert, session)

        mock_send.assert_not_called()
        assert any("alert.email_no_active_admins" in rec.message for rec in caplog.records)

    async def test_send_failure_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        from modulo.core.email_service import EmailSendingError

        alert = _make_alert(action_type="email")
        session = self._make_email_session(
            memberships=[MagicMock(account_id=uuid.uuid4())],
            accounts=[MagicMock(email="a@example.com", active=True)],
        )

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise EmailSendingError("smtp down")

        with (
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=self._make_settings()),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", side_effect=_raise),
        ):
            await dispatch_alert(_ORG_ID, alert, session)

        assert any("alert.email_send_failed" in rec.message for rec in caplog.records)

    async def test_unexpected_error_logs_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        alert = _make_alert(action_type="email")
        session = _make_session()
        session.get = AsyncMock(side_effect=RuntimeError("db down"))

        with (
            caplog.at_level("ERROR", logger="modulo.core.error_tracking.alert_dispatcher"),
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=self._make_settings()),
        ):
            await dispatch_alert(_ORG_ID, alert, session)

        assert any("alert.email_dispatch_error" in rec.message for rec in caplog.records)


# =========================================================================
# Webhook dispatch
# =========================================================================


class TestDispatchWebhook:
    async def test_no_url_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        alert = _make_alert(action_type="webhook", webhook_url=None)
        session = _make_session()

        with caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"):
            await dispatch_alert(_ORG_ID, alert, session)

        assert any("alert.webhook_no_url" in rec.message for rec in caplog.records)

    async def test_slack_url_uses_slack_payload(self) -> None:
        alert = _make_alert(
            action_type="webhook", webhook_url="https://hooks.slack.com/services/T/B/X", level="critical"
        )
        session = _make_session()

        with patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient") as mock_client:
            resp = MagicMock()
            resp.is_success = True
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            await dispatch_alert(_ORG_ID, alert, session, _make_error_group("db down"))

        body = json.loads(mock_client.return_value.__aenter__.return_value.post.await_args.kwargs["content"])
        assert "\U0001f534" in body["text"]
        assert "Error Alert: Test Rule" in body["text"]

    async def test_non_success_http_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        alert = _make_alert(action_type="webhook", webhook_url="https://example.com/hook")
        session = _make_session()

        with (
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
            patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient") as mock_client,
        ):
            resp = MagicMock()
            resp.is_success = False
            resp.status_code = 500
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            await dispatch_alert(_ORG_ID, alert, session)

        assert any("alert.webhook_http_error" in rec.message for rec in caplog.records)


# =========================================================================
# Helpers
# =========================================================================


class TestHelpers:
    def test_escape_html_escapes_special_chars(self) -> None:
        assert _escape_html('<a href="x">&</a>') == "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;"

    def test_format_slack_payload_includes_all_fields(self) -> None:
        text = _format_slack_payload(
            payload={
                "rule": "R",
                "group_id": "g",
                "level": "warning",
                "count": 2,
                "message": "m" * 600,
                "environment": "staging",
                "url": "/admin/errors/g",
            },
            emoji="\u26aa",
        )["text"]
        assert "\u26aa" in text
        assert "R" in text
        assert "g" in text
        assert "staging" in text
        assert "/admin/errors/g" in text
        assert "m" * 500 in text
