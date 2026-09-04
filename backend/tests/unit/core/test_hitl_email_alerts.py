"""Unit tests for the FAR-602 HITL email-alert module.

Covers the acceptance matrix: preference resolution, recipient resolution
(SQL role/active filters + preference matrix), the no-throw dispatch
contract, and the fire-and-forget scheduling from ``create_gate``.
"""

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import hitl_email_alerts
from modulo.core.email_service import EmailSendingError
from modulo.core.hitl_email_alerts import (
    dispatch_hitl_email_alerts,
    resolve_hitl_email_pref,
    resolve_hitl_email_recipients,
    schedule_hitl_email_dispatch,
)

_ORG = uuid.uuid4()
_PIPELINE = uuid.uuid4()
_OTHER_PIPELINE = uuid.uuid4()
_RUN = uuid.uuid4()
_GATE = "review-step"
_RUNNER_EMAIL = "runner@example.com"
_OPERATOR_EMAIL = "operator@example.com"
_PUBLIC_URL = "https://app.example.com"
_SUBJECT = f"HITL gate awaiting review - {_GATE}"
_RUN_LINK = f"{_PUBLIC_URL}/runs/{_RUN}"


def _settings_mock() -> MagicMock:
    settings = MagicMock()
    settings.modulo_public_url = _PUBLIC_URL
    settings.smtp_host = "smtp.example.com"
    return settings


def _session_returning(rows: list[tuple[str, object]]) -> AsyncMock:
    """Session double whose ``execute`` returns ``.all()`` = *rows*."""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    return session


class TestResolveHitlEmailPref:
    """Pure preference-resolution matrix: absent key = False; overrides win."""

    @pytest.mark.parametrize(
        ("preferences", "expected"),
        [
            pytest.param(None, False, id="no_preferences_at_all"),
            pytest.param({}, False, id="empty_preferences"),
            pytest.param({"theme": "dark"}, False, id="hitl_email_key_absent"),
            pytest.param({"hitl_email": {}}, False, id="empty_hitl_email_block"),
            pytest.param({"hitl_email": {"default": False}}, False, id="default_false"),
            pytest.param({"hitl_email": {"default": True}}, True, id="default_true"),
            pytest.param(
                {"hitl_email": {"default": False, "pipeline_overrides": {str(_PIPELINE): True}}},
                True,
                id="override_true_beats_default_false",
            ),
            pytest.param(
                {"hitl_email": {"default": True, "pipeline_overrides": {str(_PIPELINE): False}}},
                False,
                id="override_false_beats_default_true",
            ),
            pytest.param(
                {"hitl_email": {"default": True, "pipeline_overrides": {str(_OTHER_PIPELINE): False}}},
                True,
                id="override_for_other_pipeline_ignored",
            ),
            pytest.param({"hitl_email": {"default": "yes"}}, False, id="non_bool_default_is_off"),
            pytest.param({"hitl_email": "on"}, False, id="malformed_block_is_off"),
        ],
    )
    def test_resolution_matrix(self, preferences: object, expected: bool) -> None:
        assert resolve_hitl_email_pref(preferences, _PIPELINE) is expected


class TestResolveHitlEmailRecipients:
    async def test_no_preference_resolves_empty(self) -> None:
        rows = [(_RUNNER_EMAIL, None), (_OPERATOR_EMAIL, {})]
        recipients = await resolve_hitl_email_recipients(_session_returning(rows), _ORG, _PIPELINE)
        assert not recipients

    async def test_default_false_excluded(self) -> None:
        rows = [(_RUNNER_EMAIL, {"hitl_email": {"default": False}})]
        recipients = await resolve_hitl_email_recipients(_session_returning(rows), _ORG, _PIPELINE)
        assert not recipients

    async def test_default_true_includes_claim_permitted_users(self) -> None:
        rows = [
            (_RUNNER_EMAIL, {"hitl_email": {"default": True}}),
            (_OPERATOR_EMAIL, {"hitl_email": {"default": True}}),
        ]
        recipients = await resolve_hitl_email_recipients(_session_returning(rows), _ORG, _PIPELINE)
        assert recipients == [_RUNNER_EMAIL, _OPERATOR_EMAIL]

    async def test_override_true_selects_only_that_pipeline(self) -> None:
        rows = [
            (_RUNNER_EMAIL, {"hitl_email": {"default": False, "pipeline_overrides": {str(_PIPELINE): True}}}),
            (_OPERATOR_EMAIL, {"hitl_email": {"default": False}}),
        ]
        recipients = await resolve_hitl_email_recipients(_session_returning(rows), _ORG, _PIPELINE)
        assert recipients == [_RUNNER_EMAIL]

    async def test_override_false_excluded_even_with_default_true(self) -> None:
        rows = [
            (_RUNNER_EMAIL, {"hitl_email": {"default": True, "pipeline_overrides": {str(_PIPELINE): False}}}),
            (_OPERATOR_EMAIL, {"hitl_email": {"default": True}}),
        ]
        recipients = await resolve_hitl_email_recipients(_session_returning(rows), _ORG, _PIPELINE)
        assert recipients == [_OPERATOR_EMAIL]

    async def test_duplicate_recipients_are_deduplicated(self) -> None:
        rows = [
            (_RUNNER_EMAIL, {"hitl_email": {"default": True}}),
            (_RUNNER_EMAIL, {"hitl_email": {"default": True}}),
        ]
        recipients = await resolve_hitl_email_recipients(_session_returning(rows), _ORG, _PIPELINE)
        assert recipients == [_RUNNER_EMAIL]

    async def test_query_filters_to_active_claim_permitted_members(self) -> None:
        """The SQL scopes to the org, the hitl.claim roles, and active members.

        Users WITHOUT hitl.claim (e.g. viewer) are excluded at the query
        level regardless of their preference.
        """
        session = _session_returning([])
        await resolve_hitl_email_recipients(session, _ORG, _PIPELINE)
        stmt = session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "org_memberships.organisation_id" in compiled
        for role in ("'runner'", "'operator'", "'admin'"):
            assert role in compiled
        assert "'viewer'" not in compiled
        assert "deactivated_at" in compiled
        assert "accounts.active" in compiled


class TestDispatchHitlEmailAlerts:
    async def test_sends_one_email_per_recipient_with_subject_and_run_link(self) -> None:
        rows = [
            (_RUNNER_EMAIL, {"hitl_email": {"default": True}}),
            (_OPERATOR_EMAIL, {"hitl_email": {"default": True}}),
        ]
        with (
            patch.object(hitl_email_alerts, "get_settings", return_value=_settings_mock()),
            patch.object(hitl_email_alerts, "send_email") as mock_send,
        ):
            await dispatch_hitl_email_alerts(_session_returning(rows), _ORG, _PIPELINE, _RUN, _GATE)

        assert mock_send.call_count == 2
        first_args = mock_send.call_args_list[0].args
        assert first_args[1] == [_RUNNER_EMAIL]
        assert first_args[2] == _SUBJECT
        assert _RUN_LINK in first_args[3]  # body_html
        assert _RUN_LINK in first_args[4]  # body_text
        assert mock_send.call_args_list[1].args[1] == [_OPERATOR_EMAIL]

    async def test_no_recipients_sends_nothing(self) -> None:
        with (
            patch.object(hitl_email_alerts, "get_settings", return_value=_settings_mock()),
            patch.object(hitl_email_alerts, "send_email") as mock_send,
        ):
            await dispatch_hitl_email_alerts(_session_returning([]), _ORG, _PIPELINE, _RUN, _GATE)
        mock_send.assert_not_called()

    async def test_send_email_failure_does_not_propagate(self, caplog: pytest.LogCaptureFixture) -> None:
        rows = [
            (_RUNNER_EMAIL, {"hitl_email": {"default": True}}),
            (_OPERATOR_EMAIL, {"hitl_email": {"default": True}}),
        ]
        with (
            patch.object(hitl_email_alerts, "get_settings", return_value=_settings_mock()),
            patch.object(hitl_email_alerts, "send_email", side_effect=EmailSendingError("smtp down")),
            caplog.at_level(logging.WARNING, logger="modulo.core.hitl_email_alerts"),
        ):
            await dispatch_hitl_email_alerts(_session_returning(rows), _ORG, _PIPELINE, _RUN, _GATE)

        assert "hitl_email.dispatch_failed" in caplog.text

    async def test_per_recipient_failure_isolates_the_rest(self) -> None:
        """One recipient's SMTP failure never blocks the others."""
        rows = [
            (_RUNNER_EMAIL, {"hitl_email": {"default": True}}),
            (_OPERATOR_EMAIL, {"hitl_email": {"default": True}}),
        ]
        mock_send = MagicMock(side_effect=[EmailSendingError("smtp down"), None])
        with (
            patch.object(hitl_email_alerts, "get_settings", return_value=_settings_mock()),
            patch.object(hitl_email_alerts, "send_email", mock_send),
        ):
            await dispatch_hitl_email_alerts(_session_returning(rows), _ORG, _PIPELINE, _RUN, _GATE)

        assert mock_send.call_count == 2

    async def test_resolution_failure_does_not_propagate(self, caplog: pytest.LogCaptureFixture) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db gone"))
        with (
            patch.object(hitl_email_alerts, "get_settings", return_value=_settings_mock()),
            patch.object(hitl_email_alerts, "send_email") as mock_send,
            caplog.at_level(logging.WARNING, logger="modulo.core.hitl_email_alerts"),
        ):
            await dispatch_hitl_email_alerts(session, _ORG, _PIPELINE, _RUN, _GATE)

        mock_send.assert_not_called()
        assert "hitl_email.dispatch_failed" in caplog.text


def _dispatch_task_harness() -> tuple[MagicMock, AsyncMock]:
    """Factory + session doubles for the background-dispatch task."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


async def _drain_tasks() -> None:
    """Yield control so the scheduled fire-and-forget task can complete."""
    for _ in range(5):
        await asyncio.sleep(0)


class TestScheduleHitlEmailDispatch:
    async def test_schedules_background_dispatch_with_own_session(self) -> None:
        factory, session = _dispatch_task_harness()
        dispatched = AsyncMock()
        with (
            patch.object(hitl_email_alerts, "_dispatch_session_factory", return_value=factory),
            patch.object(hitl_email_alerts, "set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch.object(hitl_email_alerts, "dispatch_hitl_email_alerts", dispatched),
        ):
            schedule_hitl_email_dispatch(_ORG, _PIPELINE, _RUN, _GATE)
            await _drain_tasks()

        dispatched.assert_awaited_once_with(session, _ORG, _PIPELINE, _RUN, _GATE)
        mock_rls.assert_awaited_once()

    async def test_session_factory_failure_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch.object(hitl_email_alerts, "_dispatch_session_factory", side_effect=RuntimeError("no engine")),
            caplog.at_level(logging.WARNING, logger="modulo.core.hitl_email_alerts"),
        ):
            schedule_hitl_email_dispatch(_ORG, _PIPELINE, _RUN, _GATE)
            await _drain_tasks()

        assert "hitl_email.dispatch_failed" in caplog.text

    async def test_task_reference_retained_while_pending_then_cleared(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch.object(hitl_email_alerts, "_dispatch_session_factory", side_effect=RuntimeError("no engine")),
            caplog.at_level(logging.WARNING, logger="modulo.core.hitl_email_alerts"),
        ):
            schedule_hitl_email_dispatch(_ORG, _PIPELINE, _RUN, _GATE)
            assert hitl_email_alerts._PENDING_DISPATCH_TASKS
            await _drain_tasks()
        assert not hitl_email_alerts._PENDING_DISPATCH_TASKS

    def test_schedule_without_running_loop_returns(self, caplog: pytest.LogCaptureFixture) -> None:
        """No running event loop (sync caller): log and return, never raise."""
        with caplog.at_level(logging.WARNING, logger="modulo.core.hitl_email_alerts"):
            schedule_hitl_email_dispatch(_ORG, _PIPELINE, _RUN, _GATE)
        assert "hitl_email.dispatch_no_running_loop" in caplog.text
