"""Unit tests for the FAR-602 HITL email-alert module.

Covers the acceptance matrix: preference resolution, recipient resolution
(SQL role/active filters + preference matrix), the no-throw dispatch
contract, and the fire-and-forget scheduling from ``create_gate``.
"""

import asyncio
import logging
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from modulo.core import hitl_email_alerts
from modulo.core.email_service import EmailSendingError
from modulo.core.hitl_email_alerts import (
    dispatch_hitl_email_alerts,
    resolve_hitl_email_pref,
    resolve_hitl_email_recipients,
    schedule_hitl_email_dispatch,
    send_hitl_email_alerts,
)

# The lazy ``patch("modulo.db.session.get_shared_engine")`` in
# test_dispatch_session_factory_builds_on_the_shared_engine imports
# modulo.db.session at TEST time; its module-level ``_build_engine()`` needs
# Settings. There is no ``.env`` in worktrees, so provide the minimum env the
# same way as tests/unit/tools/conftest.py — setdefault so explicit CI values
# always win. (Module-level imports above do not touch db.session; only the
# patch-time import does, which runs after this block.)
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "b" * 32)
os.environ.setdefault("REDIS_URL", "")

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
    async def test_sends_only_after_the_resolution_session_closes(self) -> None:
        """Pins the iteration-2 split: the SMTP send must run AFTER the DB
        session/transaction has closed, so a slow SMTP host can never pin a
        pooled connection from the shared engine."""
        factory, session = _dispatch_task_harness()
        events: list[str] = []

        def _record_close(*_args: object, **_kwargs: object) -> bool:
            events.append("session_closed")
            return False

        async def _record_send(*_args: object, **_kwargs: object) -> None:
            events.append("sent")

        factory.return_value.__aexit__.side_effect = _record_close
        with (
            patch.object(hitl_email_alerts, "_dispatch_session_factory", return_value=factory),
            patch.object(hitl_email_alerts, "set_rls_org", new_callable=AsyncMock),
            patch.object(hitl_email_alerts, "resolve_hitl_email_recipients", AsyncMock(return_value=[_RUNNER_EMAIL])),
            patch.object(hitl_email_alerts, "send_hitl_email_alerts", AsyncMock(side_effect=_record_send)),
        ):
            schedule_hitl_email_dispatch(_ORG, _PIPELINE, _RUN, _GATE)
            await _drain_tasks()

        assert events == ["session_closed", "sent"]
        assert session.begin.called

    def test_dispatch_session_factory_builds_on_the_shared_engine(self) -> None:
        """One engine per process: the factory must build on get_shared_engine
        (a second engine would bypass the shared pool + its sizing)."""
        with patch("modulo.db.session.get_shared_engine") as mock_engine:
            factory = hitl_email_alerts._dispatch_session_factory()
        mock_engine.assert_called_once()
        assert isinstance(factory, async_sessionmaker)

    async def test_scheduled_task_uses_the_session_once(self) -> None:
        factory, session = _dispatch_task_harness()
        resolved = AsyncMock(return_value=[_RUNNER_EMAIL])
        sent = AsyncMock()
        with (
            patch.object(hitl_email_alerts, "_dispatch_session_factory", return_value=factory),
            patch.object(hitl_email_alerts, "set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch.object(hitl_email_alerts, "resolve_hitl_email_recipients", resolved),
            patch.object(hitl_email_alerts, "send_hitl_email_alerts", sent),
        ):
            schedule_hitl_email_dispatch(_ORG, _PIPELINE, _RUN, _GATE)
            await _drain_tasks()

        resolved.assert_awaited_once_with(session, _ORG, _PIPELINE)
        sent.assert_awaited_once_with([_RUNNER_EMAIL], _RUN, _GATE, None)
        mock_rls.assert_awaited_once()

    async def test_resolution_failure_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        factory, _session = _dispatch_task_harness()
        with (
            patch.object(hitl_email_alerts, "_dispatch_session_factory", return_value=factory),
            patch.object(hitl_email_alerts, "set_rls_org", new_callable=AsyncMock),
            patch.object(
                hitl_email_alerts,
                "resolve_hitl_email_recipients",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ),
            patch.object(hitl_email_alerts, "send_hitl_email_alerts") as mock_send,
            caplog.at_level(logging.WARNING, logger="modulo.core.hitl_email_alerts"),
        ):
            schedule_hitl_email_dispatch(_ORG, _PIPELINE, _RUN, _GATE)
            await _drain_tasks()

        assert "hitl_email.dispatch_failed" in caplog.text
        mock_send.assert_not_called()

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


class TestSendHitlEmailAlerts:
    async def test_no_recipients_is_a_no_op(self) -> None:
        with (
            patch.object(hitl_email_alerts, "get_settings") as mock_settings,
            patch.object(hitl_email_alerts, "send_email") as mock_send,
        ):
            await send_hitl_email_alerts([], _RUN, _GATE)
        mock_settings.assert_not_called()
        mock_send.assert_not_called()

    async def test_sends_one_email_per_recipient(self) -> None:
        with (
            patch.object(hitl_email_alerts, "get_settings", return_value=_settings_mock()),
            patch.object(hitl_email_alerts, "send_email") as mock_send,
        ):
            await send_hitl_email_alerts([_RUNNER_EMAIL, _OPERATOR_EMAIL], _RUN, _GATE)

        assert mock_send.call_count == 2
        assert mock_send.call_args_list[0].args[1] == [_RUNNER_EMAIL]
        assert mock_send.call_args_list[1].args[1] == [_OPERATOR_EMAIL]

    async def test_cancellation_propagates(self) -> None:
        """Cancellation contract: CancelledError re-raises (repo pin b72396c16)."""
        with (
            patch.object(hitl_email_alerts, "get_settings", return_value=_settings_mock()),
            patch.object(hitl_email_alerts, "send_email", side_effect=asyncio.CancelledError),
            pytest.raises(asyncio.CancelledError),
        ):
            await send_hitl_email_alerts([_RUNNER_EMAIL], _RUN, _GATE)

    async def test_briefing_passed_through_to_build_email(self) -> None:
        """Briefing dict is forwarded into _build_email."""
        briefing = {
            "description": "Review the generated code",
            "reason": "Changes are high-risk",
            "condition_result": {"expression": "output.score", "value": "0.92"},
            "artifacts": [{"node_id": str(uuid.uuid4()), "summary": "Generated PR #123"}],
        }
        with (
            patch.object(hitl_email_alerts, "get_settings", return_value=_settings_mock()),
            patch.object(hitl_email_alerts, "send_email") as mock_send,
        ):
            await send_hitl_email_alerts([_RUNNER_EMAIL], _RUN, _GATE, briefing)

        assert mock_send.call_count == 1
        args = mock_send.call_args_list[0].args
        body_text = args[4]
        assert "Review the generated code" in body_text
        assert "Changes are high-risk" in body_text
        assert "output.score" in body_text
        assert "0.92" in body_text

    async def test_none_briefing_falls_back_to_legacy(self) -> None:
        """None briefing produces the original label-only email."""
        with (
            patch.object(hitl_email_alerts, "get_settings", return_value=_settings_mock()),
            patch.object(hitl_email_alerts, "send_email") as mock_send,
        ):
            await send_hitl_email_alerts([_RUNNER_EMAIL], _RUN, _GATE, None)

        args = mock_send.call_args_list[0].args
        body_text = args[4]
        assert f"Gate: {_GATE}" in body_text
        assert "Run:" in body_text
        assert "Description:" not in body_text


class TestBuildEmailBriefing:
    """Tests for _build_email briefing rendering."""

    def _full_briefing(self) -> dict:
        return {
            "description": "Please approve the deployment",
            "reason": "All tests pass",
            "condition_result": {
                "expression": "output.status",
                "value": "success",
                "evaluated_at_node": "node-1",
            },
            "artifacts": [{"node_id": "node-1", "summary": "Deployed to staging"}],
            "trigger": "condition",
            "pipeline_name": "Deploy Pipeline",
        }

    def test_full_briefing_renders_all_fields(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        subject, body_html, body_text = _build_email(_GATE, _RUN_LINK, self._full_briefing())
        assert _GATE in subject
        assert "Please approve the deployment" in body_text
        assert "All tests pass" in body_text
        assert "output.status" in body_text
        assert "success" in body_text
        assert "Deployed to staging" in body_text
        assert _RUN_LINK in body_text

        assert "Please approve the deployment" in body_html
        assert "All tests pass" in body_html
        assert "output.status" in body_html
        assert "success" in body_html
        assert "Deployed to staging" in body_html
        assert _RUN_LINK in body_html

    def test_partial_briefing_description_only(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        briefing = {"description": "Review the output"}
        _, body_html, body_text = _build_email(_GATE, _RUN_LINK, briefing)
        assert "Review the output" in body_text
        assert "Reason:" not in body_text
        assert "Condition:" not in body_text
        assert "Review the output" in body_html

    def test_partial_briefing_condition_only(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        briefing = {
            "condition_result": {
                "expression": "len(state)",
                "value": "3",
            }
        }
        _, _, body_text = _build_email(_GATE, _RUN_LINK, briefing)
        assert "len(state)" in body_text
        assert "3" in body_text
        assert "Description:" not in body_text

    def test_none_briefing_legacy_format(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        _, body_html, body_text = _build_email(_GATE, _RUN_LINK, None)
        assert f"Gate: {_GATE}" in body_text
        assert f"Run: {_RUN_LINK}" in body_text
        assert "Description:" not in body_text
        assert f"Gate: {_GATE}" in body_html

    def test_empty_briefing_legacy_format(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        _, _, body_text = _build_email(_GATE, _RUN_LINK, {})
        assert f"Gate: {_GATE}" in body_text
        assert "Description:" not in body_text

    def test_html_escape_in_description(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        briefing = {"description": "<script>alert('xss')</script>"}
        _, body_html, body_text = _build_email(_GATE, _RUN_LINK, briefing)
        assert "<script>" not in body_html
        assert "&lt;script&gt;" in body_html
        assert "<script>" in body_text  # plain text is not escaped

    def test_html_escape_in_reason(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        briefing = {"reason": "Risk: <b>high</b>"}
        _, body_html, _ = _build_email(_GATE, _RUN_LINK, briefing)
        assert "<b>high</b>" not in body_html
        assert "&lt;b&gt;high&lt;/b&gt;" in body_html

    def test_html_escape_in_condition_value(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        briefing = {
            "condition_result": {
                "expression": "x",
                "value": "<img src=x onerror=alert(1)>",
            }
        }
        _, body_html, _ = _build_email(_GATE, _RUN_LINK, briefing)
        assert "<img" not in body_html
        assert "&lt;img" in body_html

    def test_html_escape_in_artifact_summary(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        briefing = {"artifacts": [{"node_id": "n1", "summary": "Output: <b>bold</b>"}]}
        _, body_html, _ = _build_email(_GATE, _RUN_LINK, briefing)
        assert "<b>bold</b>" not in body_html
        assert "&lt;b&gt;bold&lt;/b&gt;" in body_html

    def test_artifact_truncation_with_marker(self) -> None:
        from modulo.core.hitl_email_alerts import _EMAIL_BODY_BUDGET_CHARS, _build_email

        long_summary = "x" * 2000
        briefing = {"artifacts": [{"node_id": "n1", "summary": long_summary}]}
        _, _, body_text = _build_email(_GATE, _RUN_LINK, briefing)
        assert "…" in body_text
        assert len(body_text) < _EMAIL_BODY_BUDGET_CHARS + 300  # some header overhead

    def test_condition_result_absent_key_does_not_crash(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        briefing = {"condition_result": None}
        subject, _, body_text = _build_email(_GATE, _RUN_LINK, briefing)
        assert _GATE in subject
        assert "Condition:" not in body_text

    def test_artifacts_empty_list_no_artifact_section(self) -> None:
        from modulo.core.hitl_email_alerts import _build_email

        briefing = {"artifacts": []}
        _, _, body_text = _build_email(_GATE, _RUN_LINK, briefing)
        assert "Artifact:" not in body_text

    def test_gate_label_html_escaped_in_briefing_path(self) -> None:
        """gate_label with HTML is escaped in the briefing-path HTML body."""
        from modulo.core.hitl_email_alerts import _build_email

        xss_label = "<script>alert(1)</script>"
        briefing = {"description": "test"}
        _, body_html, _ = _build_email(xss_label, _RUN_LINK, briefing)
        assert "<script>" not in body_html
        assert "&lt;script&gt;" in body_html

    def test_long_briefing_no_truncated_html_entity(self) -> None:
        """Truncation must never split an HTML entity in the HTML body.

        The old code truncated *after* HTML escaping, which could turn
        ``&amp;`` into ``&am``.  This test uses ``&amp;`` repeatedly in a
        long description so truncation would land inside one if the old
        code path were still active.
        """
        from modulo.core.hitl_email_alerts import _build_email

        # Each "foo &amp; bar " is 16 chars.  Enough to exceed the budget.
        long_desc = ("foo &amp; bar " * 200).strip()
        briefing = {"description": long_desc}
        _, body_html, _ = _build_email(_GATE, _RUN_LINK, briefing)
        # Every ``&amp;`` in the HTML must be complete — no partial entities.
        import re

        partial_entities = re.findall(r"&[a-z]{1,5}(?![a-z;])", body_html)
        assert partial_entities == [], f"Found truncated HTML entities: {partial_entities}"


class TestScheduleHitlEmailDispatchBriefing:
    """Ensure the briefing is threaded through the scheduling chain."""

    async def test_briefing_reaches_send_hitl_email_alerts(self) -> None:
        factory, _session = _dispatch_task_harness()
        briefing = {"description": "test"}
        sent = AsyncMock()
        with (
            patch.object(hitl_email_alerts, "_dispatch_session_factory", return_value=factory),
            patch.object(hitl_email_alerts, "set_rls_org", new_callable=AsyncMock),
            patch.object(hitl_email_alerts, "resolve_hitl_email_recipients", AsyncMock(return_value=[_RUNNER_EMAIL])),
            patch.object(hitl_email_alerts, "send_hitl_email_alerts", sent),
        ):
            schedule_hitl_email_dispatch(_ORG, _PIPELINE, _RUN, _GATE, briefing)
            await _drain_tasks()

        sent.assert_awaited_once()
        assert sent.call_args.args[3] is briefing

    async def test_none_briefing_reaches_send(self) -> None:
        factory, _session = _dispatch_task_harness()
        sent = AsyncMock()
        with (
            patch.object(hitl_email_alerts, "_dispatch_session_factory", return_value=factory),
            patch.object(hitl_email_alerts, "set_rls_org", new_callable=AsyncMock),
            patch.object(hitl_email_alerts, "resolve_hitl_email_recipients", AsyncMock(return_value=[_RUNNER_EMAIL])),
            patch.object(hitl_email_alerts, "send_hitl_email_alerts", sent),
        ):
            schedule_hitl_email_dispatch(_ORG, _PIPELINE, _RUN, _GATE, None)
            await _drain_tasks()

        sent.assert_awaited_once()
        assert sent.call_args.args[3] is None
