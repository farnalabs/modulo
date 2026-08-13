"""QA lens tests for ``modulo.core.error_tracking.alert_dispatcher``.

Covers the dispatch routing matrix (in_app / email / webhook / unknown),
the email SMTP resolution and admin-recipient queries, the Slack-aware
webhook payload format, and the best-effort ``alert_resolved`` lifecycle
event — including every failure path that must log rather than propagate.
"""

from __future__ import annotations

import uuid
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.error_tracking.alert_dispatcher import (
    _build_summary,
    _dispatch_webhook,
    _escape_html,
    _format_slack_payload,
    dispatch_alert,
    dispatch_alert_resolved,
)
from modulo.core.error_tracking.alerting import TriggeredAlert
from modulo.db.models.organisation import Organisation

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_GROUP_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


class _FakeResponse:
    is_success = True


class _FakeResponseFail:
    is_success = False
    status_code = 500


class _FakeClient:
    """Configurable httpx.AsyncClient stand-in that records posted bodies."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.responses: list[object] = []
        self.posted: list[tuple[str, bytes]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: object) -> object:
        self.posted.append((url, kwargs["content"]))
        if self.responses:
            return self.responses.pop(0)
        return _FakeResponse()


class _FakeClientError:
    """Stand-in that raises RequestError on every POST."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: object) -> object:
        raise __import__("httpx").RequestError("boom")


def _alert(**overrides: object) -> TriggeredAlert:
    fields: dict[str, object] = {
        "rule_id": uuid.uuid4(),
        "rule_name": "Test Rule",
        "action_type": "webhook",
        "webhook_url": "https://example.com/hook",
        "error_group_id": _GROUP_ID,
        "fingerprint": "fp123",
        "level": "error",
        "count": 3,
        "environment": "staging",
    }
    fields.update(overrides)
    return TriggeredAlert(**fields)


def _session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


class _MockSettings:
    def __init__(
        self,
        smtp_host: str = "fallback.example.com",
        smtp_port: int = 25,
        smtp_username: str = "u",
        smtp_password: str = "p",
        email_from: str = "f@example.com",
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        self.email_from = email_from

    def model_copy(self, *, update: dict[str, object]) -> _MockSettings:
        clone = _MockSettings(
            smtp_host=self.smtp_host,
            smtp_port=self.smtp_port,
            smtp_username=self.smtp_username,
            smtp_password=self.smtp_password,
            email_from=self.email_from,
        )
        for key, value in update.items():
            setattr(clone, key, value)
        return clone


def _run_sync(fn: object, *args: object, **kwargs: object) -> object:
    return fn(*args, **kwargs)


class TestEscapeHtml:
    def test_escapes_special_chars(self) -> None:
        assert _escape_html('<a href="x&y">') == "&lt;a href=&quot;x&amp;y&quot;&gt;"


class TestBuildSummary:
    def test_builds_summary(self) -> None:
        summary = _build_summary(_alert(), "something went wrong")
        assert "[error]" in summary
        assert "Test Rule" in summary
        assert "count=3" in summary


class TestDispatchRouting:
    async def test_unknown_action_type_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        alert = _alert(action_type="sms", webhook_url=None)
        with caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"):
            await dispatch_alert(_ORG_ID, alert, _session())
        assert any("alert.unknown_action_type" in rec.message for rec in caplog.records)

    async def test_in_app_creates_delivery_log(self) -> None:
        session = _session()
        error_group = MagicMock()
        error_group.sample_event = MagicMock()
        error_group.sample_event.message = "boom"

        await dispatch_alert(_ORG_ID, _alert(action_type="in_app"), session, error_group)

        assert session.add.call_count == 1
        entry = session.add.call_args.args[0]
        assert entry.event_type == "error_alert"
        assert entry.status == "in_app"
        assert entry.organisation_id == _ORG_ID
        assert entry.last_error == "[error] Test Rule: boom (count=3)"


class TestDispatchEmail:
    def _session_with_org(
        self,
        *,
        smtp_host: str = "smtp.org.example.com",
        memberships: list[MagicMock] | None = None,
        accounts: list[MagicMock] | None = None,
    ) -> AsyncMock:
        session = _session()
        org = MagicMock(spec=Organisation)
        org.settings_json = {"email": {"smtp_host": smtp_host}}

        def _get(model: object, org_id: object) -> MagicMock:
            if model is Organisation and org_id == _ORG_ID:
                return org
            return None

        session.get = AsyncMock(side_effect=_get)

        def _make_result(rows: list[MagicMock]) -> MagicMock:
            return MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows))))

        session.execute = AsyncMock(
            side_effect=[
                _make_result(memberships or []),
                _make_result(accounts or []),
            ]
        )
        return session

    def _admin_membership(self, account_id: uuid.UUID) -> MagicMock:
        m = MagicMock()
        m.account_id = account_id
        m.role = "admin"
        m.deactivated_at = None
        return m

    def _account(self, account_id: uuid.UUID, *, active: bool = True) -> MagicMock:
        a = MagicMock()
        a.id = account_id
        a.email = "admin@example.com"
        a.active = active
        return a

    async def test_email_success_sends_to_active_admins(self) -> None:
        account_id = uuid.uuid4()
        session = self._session_with_org(
            memberships=[self._admin_membership(account_id)],
            accounts=[self._account(account_id)],
        )
        settings = _MockSettings()

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as send,
            patch("modulo.core.error_tracking.alert_dispatcher.asyncio.to_thread", side_effect=_run_sync),
        ):
            await dispatch_alert(_ORG_ID, _alert(action_type="email"), session)

        send.assert_called_once()
        sent_settings = send.call_args.args[0]
        assert sent_settings.smtp_host == "smtp.org.example.com"
        assert send.call_args.args[1] == ["admin@example.com"]
        assert "[Modulo Alert] error: Test Rule" in send.call_args.args[2]
        assert "View in Modulo" in send.call_args.args[3]
        assert send.call_args.args[4].startswith("Modulo Alert: Test Rule")

    async def test_email_org_missing_uses_global_settings(self) -> None:
        account_id = uuid.uuid4()
        session = self._session_with_org(
            memberships=[self._admin_membership(account_id)],
            accounts=[self._account(account_id)],
        )
        session.get = AsyncMock(return_value=None)
        settings = _MockSettings(smtp_host="global.example.com")

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=True) as send,
            patch("modulo.core.error_tracking.alert_dispatcher.asyncio.to_thread", side_effect=_run_sync),
        ):
            await dispatch_alert(_ORG_ID, _alert(action_type="email"), session)

        send.assert_called_once()
        assert send.call_args.args[0].smtp_host == "global.example.com"

    async def test_email_no_smtp_host_logs_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        session = self._session_with_org(smtp_host="")
        settings = _MockSettings(smtp_host="")

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as send,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await dispatch_alert(_ORG_ID, _alert(action_type="email"), session)

        send.assert_not_called()
        assert any("alert.email_disabled_no_smtp_host" in rec.message for rec in caplog.records)

    async def test_email_no_admins_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        session = self._session_with_org(memberships=[])
        settings = _MockSettings(smtp_host="smtp.example.com")

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as send,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await dispatch_alert(_ORG_ID, _alert(action_type="email"), session)

        send.assert_not_called()
        assert any("alert.email_no_admins" in rec.message for rec in caplog.records)

    async def test_email_no_active_admins_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        account_id = uuid.uuid4()
        session = self._session_with_org(
            memberships=[self._admin_membership(account_id)],
            accounts=[],
        )
        settings = _MockSettings(smtp_host="smtp.example.com")

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email") as send,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await dispatch_alert(_ORG_ID, _alert(action_type="email"), session)

        send.assert_not_called()
        assert any("alert.email_no_active_admins" in rec.message for rec in caplog.records)

    async def test_email_send_error_records_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        account_id = uuid.uuid4()
        session = self._session_with_org(
            memberships=[self._admin_membership(account_id)],
            accounts=[self._account(account_id)],
        )
        settings = _MockSettings(smtp_host="smtp.example.com")

        from modulo.core.email_service import EmailSendingError

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch(
                "modulo.core.error_tracking.alert_dispatcher.send_email",
                side_effect=EmailSendingError("smtp down"),
            ),
            patch("modulo.core.error_tracking.alert_dispatcher.asyncio.to_thread", side_effect=_run_sync),
            patch("modulo.core.error_tracking.alert_dispatcher.record_alert_delivery_failed") as failed,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await dispatch_alert(_ORG_ID, _alert(action_type="email"), session)

        failed.assert_called_once()
        assert any("alert.email_send_failed" in rec.message for rec in caplog.records)

    async def test_email_send_false_noop(self) -> None:
        account_id = uuid.uuid4()
        session = self._session_with_org(
            memberships=[self._admin_membership(account_id)],
            accounts=[self._account(account_id)],
        )
        settings = _MockSettings(smtp_host="smtp.example.com")

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", return_value=False),
            patch("modulo.core.error_tracking.alert_dispatcher.asyncio.to_thread", side_effect=_run_sync),
        ):
            await dispatch_alert(_ORG_ID, _alert(action_type="email"), session)

    async def test_email_unexpected_error_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        account_id = uuid.uuid4()
        session = self._session_with_org(
            memberships=[self._admin_membership(account_id)],
            accounts=[self._account(account_id)],
        )
        settings = _MockSettings(smtp_host="smtp.example.com")

        with (
            patch("modulo.core.error_tracking.alert_dispatcher.get_settings", return_value=settings),
            patch("modulo.core.error_tracking.alert_dispatcher.send_email", side_effect=RuntimeError("boom")),
            patch("modulo.core.error_tracking.alert_dispatcher.asyncio.to_thread", side_effect=_run_sync),
            caplog.at_level("ERROR", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await dispatch_alert(_ORG_ID, _alert(action_type="email"), session)

        assert any("alert.email_dispatch_error" in rec.message for rec in caplog.records)


class TestDispatchWebhook:
    async def test_webhook_no_url_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"):
            await _dispatch_webhook(_alert(webhook_url=None), "", "/admin/errors/x")
        assert any("alert.webhook_no_url" in rec.message for rec in caplog.records)

    async def test_webhook_success_posts_contract_payload(self) -> None:
        run_group_id = uuid.uuid4()
        alert = _alert(
            level="critical",
            signal="agent.failed",
            elevation_signal="agent.failed",
            attempt_n=2,
            run_group_id=run_group_id,
        )
        client = _FakeClient()
        with patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", return_value=client):
            await _dispatch_webhook(alert, "Agent failed", "/admin/errors/x")

        import json

        url, content = client.posted[0]
        assert url == "https://example.com/hook"
        body = json.loads(content.decode())
        assert body["event"] == "error_alert"
        assert body["alert_id"] == str(alert.alert_id)
        assert body["group_id"] == str(_GROUP_ID)
        assert body["elevation_signal"] == "agent.failed"
        assert body["attempt_n"] == 2
        assert body["run_group_id"] == str(run_group_id)
        assert body["message"] == "Agent failed"

    async def test_webhook_slack_url_formats_payload(self) -> None:
        alert = _alert(webhook_url="https://hooks.slack.com/services/T00/B00/xxx", level="critical")
        client = _FakeClient()
        with patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", return_value=client):
            await _dispatch_webhook(alert, "DB down", "/admin/errors/x")

        import json

        body = json.loads(client.posted[0][1].decode())
        assert "\U0001f534" in body["text"]
        assert "*Error Alert: Test Rule*" in body["text"]
        assert "DB down" in body["text"]

    async def test_webhook_http_error_records_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        client = _FakeClient()
        client.responses = [_FakeResponseFail()]
        with (
            patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", return_value=client),
            patch("modulo.core.error_tracking.alert_dispatcher.record_alert_delivery_failed") as failed,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await _dispatch_webhook(_alert(), "", "/admin/errors/x")

        failed.assert_called_once()
        assert any("alert.webhook_http_error" in rec.message for rec in caplog.records)

    async def test_webhook_request_error_records_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", return_value=_FakeClientError()),
            patch("modulo.core.error_tracking.alert_dispatcher.record_alert_delivery_failed") as failed,
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await _dispatch_webhook(_alert(), "", "/admin/errors/x")

        failed.assert_called_once()
        assert any("alert.webhook_request_failed" in rec.message for rec in caplog.records)


class TestDispatchAlertResolved:
    async def test_resolved_without_webhook_records_in_app(self) -> None:
        session = _session()
        await dispatch_alert_resolved(
            _ORG_ID,
            group_id=_GROUP_ID,
            signal="agent.failed",
            reason="recovered",
            session=session,
        )

        assert session.add.call_count == 1
        entry = session.add.call_args.args[0]
        assert entry.event_type == "alert_resolved"
        assert entry.status == "in_app"
        assert entry.last_error == "agent.failed resolved: recovered"

    async def test_resolved_with_webhook_posts_payload(self) -> None:
        session = _session()
        client = _FakeClient()
        with patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", return_value=client):
            await dispatch_alert_resolved(
                _ORG_ID,
                group_id=_GROUP_ID,
                signal="agent.failed",
                reason="recovered",
                session=session,
                webhook_url="https://example.com/hook",
            )

        import json

        url, content = client.posted[0]
        assert url == "https://example.com/hook"
        body = json.loads(content.decode())
        assert body["event"] == "alert_resolved"
        assert body["group_id"] == str(_GROUP_ID)
        assert body["signal"] == "agent.failed"
        assert body["reason"] == "recovered"

    async def test_resolved_webhook_http_error_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _session()
        client = _FakeClient()
        client.responses = [_FakeResponseFail()]
        with (
            patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", return_value=client),
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await dispatch_alert_resolved(
                _ORG_ID,
                group_id=_GROUP_ID,
                signal="agent.failed",
                reason="recovered",
                session=session,
                webhook_url="https://example.com/hook",
            )

        assert any("alert.resolved_webhook_http_error" in rec.message for rec in caplog.records)

    async def test_resolved_webhook_request_error_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _session()
        with (
            patch("modulo.core.error_tracking.alert_dispatcher.httpx.AsyncClient", return_value=_FakeClientError()),
            caplog.at_level("WARNING", logger="modulo.core.error_tracking.alert_dispatcher"),
        ):
            await dispatch_alert_resolved(
                _ORG_ID,
                group_id=_GROUP_ID,
                signal="agent.failed",
                reason="recovered",
                session=session,
                webhook_url="https://example.com/hook",
            )

        assert any("alert.resolved_webhook_request_failed" in rec.message for rec in caplog.records)


class TestSlackPayloadFormat:
    def test_includes_emoji_and_contract_fields(self) -> None:
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
        assert "DB down" in result["text"]
