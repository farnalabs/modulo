"""Unit tests for the FAR-611 approve-sweep anomaly alarm.

Covers: threshold crossing (6th approve across 2 pipelines alarms), the
single-pipeline non-alarm, cooldown suppression within the hour, failure
isolation (detection/notifier raising never fails the decision), and the
manager wiring (approve / approve_with_modification invoke the alarm).
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.hitl_manager import HITLManager, sweep_alarm
from modulo.core.hitl_manager.sweep_alarm import AUDIT_EVENT_TYPE, maybe_alarm_approve_sweep

from .conftest import _session_decide
from .test_hitl_manager import _GATE, _ORG, _RUN, _USER, _gate

_ORG_B = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clean_alarm_state():
    sweep_alarm.reset_alarm_state()
    yield
    sweep_alarm.reset_alarm_state()


def _alarm_session(count: int, distinct_pipelines: int) -> AsyncMock:
    """Session whose execute serves the detection aggregate: .one() -> (count, distinct)."""
    session = AsyncMock()
    result = MagicMock()
    result.one.return_value = (count, distinct_pipelines)
    session.execute = AsyncMock(return_value=result)
    return session


def _broken_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=RuntimeError("detection db down"))
    return session


@contextlib.contextmanager
def _sweep_patches():
    """Patch the alarm's outbound seams (audit, notification, webhook)."""
    with (
        patch("modulo.core.hitl_manager.sweep_alarm.append_audit_event", new_callable=AsyncMock) as mock_audit,
        patch(
            "modulo.core.hitl_manager.sweep_alarm._create_in_app_notification", new_callable=AsyncMock
        ) as mock_notify,
        patch("modulo.core.hitl_manager.sweep_alarm._schedule_sweep_webhook") as mock_webhook,
    ):
        yield mock_audit, mock_notify, mock_webhook


class TestThreshold:
    async def test_six_approves_two_pipelines_alarms(self):
        """The 6th approve across 2 distinct pipelines within the window alarms."""
        session = _alarm_session(6, 2)
        with _sweep_patches() as (mock_audit, mock_notify, mock_webhook):
            alarmed = await maybe_alarm_approve_sweep(
                session, org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC)
            )
        assert alarmed is True
        mock_audit.assert_awaited_once()
        assert mock_audit.await_args.kwargs["event_type"] == AUDIT_EVENT_TYPE
        mock_notify.assert_awaited_once()
        mock_webhook.assert_called_once()

    async def test_fifth_approve_does_not_alarm(self):
        """The count must EXCEED the threshold — 5 approves do not alarm."""
        session = _alarm_session(5, 2)
        with _sweep_patches() as (mock_audit, mock_notify, mock_webhook):
            alarmed = await maybe_alarm_approve_sweep(
                session, org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC)
            )
        assert alarmed is False
        mock_audit.assert_not_awaited()
        mock_notify.assert_not_awaited()
        mock_webhook.assert_not_called()

    async def test_six_approves_one_pipeline_does_not_alarm(self):
        """6 approves on ONE pipeline is unusual but not a cross-pipeline sweep."""
        session = _alarm_session(6, 1)
        with _sweep_patches() as (mock_audit, _mock_notify, mock_webhook):
            alarmed = await maybe_alarm_approve_sweep(
                session, org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC)
            )
        assert alarmed is False
        mock_audit.assert_not_awaited()
        mock_webhook.assert_not_called()

    async def test_no_actor_skips_detection(self):
        """Unattributed decisions (actor_id=None) cannot be swept — no query runs."""
        session = _alarm_session(50, 5)
        with _sweep_patches() as (mock_audit, _mock_notify, _mock_webhook):
            alarmed = await maybe_alarm_approve_sweep(
                session, org_id=_ORG, actor_id=None, gate=_gate(), now=datetime.now(UTC)
            )
        assert alarmed is False
        session.execute.assert_not_awaited()
        mock_audit.assert_not_awaited()

    async def test_alarm_payload_shape(self):
        """The alarm payload carries actor, count, distinct pipelines, window."""
        session = _alarm_session(9, 4)
        with _sweep_patches() as (mock_audit, _mock_notify, _mock_webhook):
            await maybe_alarm_approve_sweep(session, org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC))
        payload = mock_audit.await_args.kwargs["payload_json"]
        assert payload["approve_count"] == 9
        assert payload["distinct_pipeline_count"] == 4
        assert payload["window_seconds"] == sweep_alarm.SWEEP_WINDOW_SECONDS
        assert payload["actor"] == str(_USER)


class TestCooldown:
    async def test_second_alarm_within_hour_is_suppressed(self):
        """During an ongoing sweep, only the FIRST crossing alarms per hour."""
        t0 = datetime.now(UTC)
        session = _alarm_session(6, 2)
        with _sweep_patches() as (mock_audit, _mock_notify, _mock_webhook):
            first = await maybe_alarm_approve_sweep(session, org_id=_ORG, actor_id=_USER, gate=_gate(), now=t0)
            later = t0 + timedelta(minutes=30)
            second = await maybe_alarm_approve_sweep(
                _alarm_session(22, 5), org_id=_ORG, actor_id=_USER, gate=_gate(), now=later
            )
        assert first is True
        assert second is False
        assert mock_audit.await_count == 1

    async def test_alarm_fires_again_after_cooldown(self):
        """A new sweep more than an hour later alarms again."""
        t0 = datetime.now(UTC)
        with _sweep_patches() as (mock_audit, _mock_notify, _mock_webhook):
            await maybe_alarm_approve_sweep(_alarm_session(6, 2), org_id=_ORG, actor_id=_USER, gate=_gate(), now=t0)
            again = await maybe_alarm_approve_sweep(
                _alarm_session(8, 3),
                org_id=_ORG,
                actor_id=_USER,
                gate=_gate(),
                now=t0 + sweep_alarm.SWEEP_ALARM_COOLDOWN + timedelta(minutes=1),
            )
        assert again is True
        assert mock_audit.await_count == 2

    async def test_cooldown_is_per_actor(self):
        """One actor's suppression never masks another actor's sweep."""
        t0 = datetime.now(UTC)
        other_actor = uuid.uuid4()
        with _sweep_patches() as (mock_audit, _mock_notify, _mock_webhook):
            await maybe_alarm_approve_sweep(_alarm_session(6, 2), org_id=_ORG, actor_id=_USER, gate=_gate(), now=t0)
            other = await maybe_alarm_approve_sweep(
                _alarm_session(7, 2), org_id=_ORG, actor_id=other_actor, gate=_gate(), now=t0
            )
        assert other is True
        assert mock_audit.await_count == 2

    async def test_cooldown_is_per_org(self):
        """Orgs are isolated — another org's sweep alarms independently."""
        t0 = datetime.now(UTC)
        with _sweep_patches() as (mock_audit, _mock_notify, _mock_webhook):
            await maybe_alarm_approve_sweep(_alarm_session(6, 2), org_id=_ORG, actor_id=_USER, gate=_gate(), now=t0)
            other_org = await maybe_alarm_approve_sweep(
                _alarm_session(7, 2), org_id=_ORG_B, actor_id=_USER, gate=_gate(), now=t0
            )
        assert other_org is True
        assert mock_audit.await_count == 2


class TestFailureIsolation:
    async def test_detection_failure_returns_false_and_does_not_raise(self):
        """A broken detection query never fails the (already committed) decision."""
        session = _broken_session()
        with _sweep_patches() as (mock_audit, mock_notify, mock_webhook):
            alarmed = await maybe_alarm_approve_sweep(
                session, org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC)
            )
        assert alarmed is False
        mock_audit.assert_not_awaited()
        mock_notify.assert_not_awaited()
        mock_webhook.assert_not_called()

    async def test_notifier_failure_does_not_raise(self):
        """A raising audit append is swallowed — the decision is never affected."""
        session = _alarm_session(6, 2)
        with patch(
            "modulo.core.hitl_manager.sweep_alarm.append_audit_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("audit db down"),
        ):
            alarmed = await maybe_alarm_approve_sweep(
                session, org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC)
            )
        assert alarmed is False

    async def test_emission_failure_does_not_arm_cooldown(self):
        """A failed emission must not suppress the NEXT real crossing."""
        t0 = datetime.now(UTC)
        with patch(
            "modulo.core.hitl_manager.sweep_alarm.append_audit_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("audit db down"),
        ):
            await maybe_alarm_approve_sweep(_alarm_session(6, 2), org_id=_ORG, actor_id=_USER, gate=_gate(), now=t0)
        with _sweep_patches():
            recovered = await maybe_alarm_approve_sweep(
                _alarm_session(8, 3), org_id=_ORG, actor_id=_USER, gate=_gate(), now=t0
            )
        assert recovered is True

    async def test_detection_failure_does_not_arm_cooldown(self):
        """A failed detection must not suppress the NEXT real crossing."""
        t0 = datetime.now(UTC)
        with _sweep_patches() as (mock_audit, _mock_notify, _mock_webhook):
            await maybe_alarm_approve_sweep(_broken_session(), org_id=_ORG, actor_id=_USER, gate=_gate(), now=t0)
            recovered = await maybe_alarm_approve_sweep(
                _alarm_session(6, 2), org_id=_ORG, actor_id=_USER, gate=_gate(), now=t0
            )
        assert recovered is True
        mock_audit.assert_awaited_once()


class TestManagerWiring:
    async def test_approve_invokes_sweep_alarm(self):
        """approve() runs the sweep alarm after committing its audit event."""
        future = datetime.now(UTC) + timedelta(minutes=5)
        gate = _gate(account_id=_USER, claim_token="tok", expires_at=future)
        gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
        session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

        with (
            patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock),
            patch("modulo.core.hitl_manager.maybe_alarm_approve_sweep", new_callable=AsyncMock) as mock_alarm,
        ):
            mgr = HITLManager()
            await mgr.approve(
                session,
                run_id=_RUN,
                gate_id=_GATE,
                org_id=_ORG,
                claim_token="tok",
                actor_id=_USER,
            )

        mock_alarm.assert_awaited_once()
        assert mock_alarm.await_args.kwargs["org_id"] == _ORG
        assert mock_alarm.await_args.kwargs["actor_id"] == _USER
        assert mock_alarm.await_args.kwargs["gate"].id == gate_decided.id

    async def test_approve_with_modification_invokes_sweep_alarm(self):
        """approve_with_modification() runs the sweep alarm too."""
        future = datetime.now(UTC) + timedelta(minutes=5)
        gate = _gate(account_id=_USER, claim_token="tok", expires_at=future)
        gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
        session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

        with (
            patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock),
            patch("modulo.core.hitl_manager.maybe_alarm_approve_sweep", new_callable=AsyncMock) as mock_alarm,
        ):
            mgr = HITLManager()
            await mgr.approve_with_modification(
                session,
                run_id=_RUN,
                gate_id=_GATE,
                org_id=_ORG,
                claim_token="tok",
                modified_output={"value": 42},
                actor_id=_USER,
            )

        mock_alarm.assert_awaited_once()
        assert mock_alarm.await_args.kwargs["actor_id"] == _USER

    async def test_alarm_failure_does_not_fail_the_decision(self):
        """The no-throw alarm contract holds through the manager path."""
        future = datetime.now(UTC) + timedelta(minutes=5)
        gate = _gate(account_id=_USER, claim_token="tok", expires_at=future)
        gate_decided = _gate(account_id=None, claim_token=None, expires_at=None, decision="approved")
        session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)

        with (
            patch("modulo.core.hitl_manager.append_audit_event", new_callable=AsyncMock),
            patch(
                "modulo.core.hitl_manager.maybe_alarm_approve_sweep",
                new_callable=AsyncMock,
                side_effect=RuntimeError("alarm exploded"),
            ),
        ):
            mgr = HITLManager()
            result = await mgr.approve(
                session,
                run_id=_RUN,
                gate_id=_GATE,
                org_id=_ORG,
                claim_token="tok",
                actor_id=_USER,
            )

        assert result.decision == "approved"
        assert result.delivered_at is not None


class TestDetectionQuery:
    async def test_count_query_is_org_and_actor_scoped(self):
        """The detection statement filters on org, actor, decision event, window."""
        session = _alarm_session(1, 1)
        window_start = datetime.now(UTC) - timedelta(seconds=sweep_alarm.SWEEP_WINDOW_SECONDS)
        await sweep_alarm.count_recent_approves(session, org_id=_ORG, actor_id=_USER, window_start=window_start)
        stmt = session.execute.await_args.args[0]
        compiled = stmt.compile()
        sql = str(compiled)
        assert "audit_events" in sql
        assert "hitl_claims" in sql
        # The decision event type is a bound parameter, not a SQL literal.
        assert sweep_alarm._DECISION_EVENT_TYPE in str(compiled.params.values())

    async def test_real_hitl_claim_model_is_not_required(self):
        """count_recent_approves works against a plain session mock (shape parity)."""
        session = _alarm_session(7, 3)
        count, distinct = await sweep_alarm.count_recent_approves(
            session,
            org_id=_ORG,
            actor_id=_USER,
            window_start=datetime.now(UTC) - timedelta(seconds=60),
        )
        assert count == 7
        assert distinct == 3
