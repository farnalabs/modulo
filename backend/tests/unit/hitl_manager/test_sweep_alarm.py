"""Unit tests for the FAR-611 approve-sweep anomaly alarm.

Covers: threshold crossing (6th approve across 2 pipelines alarms), the
single-pipeline non-alarm, cooldown suppression within the hour, failure
isolation (detection/notifier raising never fails the decision), the
manager wiring (approve / approve_with_modification invoke the alarm), the
aggregate detection query covering BOTH decision surfaces (approve +
manual delivery, FAR-611 review fix), the detection savepoint (a DB error
on the detection SELECT never aborts the decision transaction), and the
no-duplicate-notification contract (the alarm writes no in-app
notification row itself — the webhook dispatch creates it).
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.hitl_manager import HITLManager, sweep_alarm
from modulo.core.hitl_manager.sweep_alarm import AUDIT_EVENT_TYPE, maybe_alarm_approve_sweep
from modulo.core.notifier.event_mapper import NotificationEventMapper

from .conftest import _session_decide
from .test_hitl_manager import _GATE, _ORG, _RUN, _USER, _gate

_ORG_B = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clean_alarm_state():
    sweep_alarm.reset_alarm_state()
    yield
    sweep_alarm.reset_alarm_state()


def _stub_begin_nested(session: AsyncMock) -> None:
    """Mirror the real AsyncSession.begin_nested() shape on a mock.

    The real method is synchronous and returns the async context manager
    directly; a plain AsyncMock's call returns an unawaited coroutine, which
    breaks ``async with session.begin_nested():``. Same stub as conftest's
    ``_session_decide``.
    """
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=cm)


def _alarm_session(count: int, distinct_pipelines: int) -> AsyncMock:
    """Session whose execute serves the detection aggregate: .one() -> (count, distinct)."""
    session = AsyncMock()
    result = MagicMock()
    result.one.return_value = (count, distinct_pipelines)
    session.execute = AsyncMock(return_value=result)
    _stub_begin_nested(session)
    return session


def _broken_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=RuntimeError("detection db down"))
    _stub_begin_nested(session)
    return session


@contextlib.contextmanager
def _sweep_patches():
    """Patch the alarm's outbound seams (audit, in-app mapper, webhook).

    The in-app mapper patch asserts the NO-duplicate-notification contract:
    the alarm must never write a notification row itself — the fire-and-
    forget webhook dispatch (``dispatch_event``) creates it in its own
    transaction (hitl_overdue sibling pattern, FAR-611 review fix).
    """
    with (
        patch("modulo.core.hitl_manager.sweep_alarm.append_audit_event", new_callable=AsyncMock) as mock_audit,
        patch.object(NotificationEventMapper, "create_from_event", new_callable=AsyncMock) as mock_notify,
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
        # The alarm itself writes NO in-app notification — dispatch_event does.
        mock_notify.assert_not_awaited()
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

    async def test_mixed_approves_and_manual_deliveries_alarm(self):
        """A sweep of 6 MIXED approve + deliver_manual decisions across 2
        pipelines trips the alarm — manual deliveries count as decisions
        (FAR-611 review fix: deliver_manual resumes the run past the gate,
        the same impact an approve has)."""
        session = _alarm_session(6, 2)
        with _sweep_patches() as (mock_audit, _mock_notify, mock_webhook):
            alarmed = await maybe_alarm_approve_sweep(
                session, org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC)
            )
        assert alarmed is True
        mock_audit.assert_awaited_once()
        mock_webhook.assert_called_once()


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

    async def test_detection_db_error_rolls_back_detection_savepoint(self):
        """A DB error on the detection SELECT must not poison the decision
        transaction: the query runs inside its own savepoint whose rollback
        contains the failure, the alarm stays no-throw, no cooldown is armed,
        and the surrounding approve commits (FAR-611 review fix)."""
        events: list[str] = []

        @contextlib.asynccontextmanager
        async def _savepoint():
            events.append("enter")
            try:
                yield
            except Exception:
                events.append("rollback")
                raise
            events.append("release")

        session = _broken_session()
        session.begin_nested = MagicMock(side_effect=_savepoint)
        with _sweep_patches():
            alarmed = await maybe_alarm_approve_sweep(
                session, org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC)
            )
        assert alarmed is False
        assert events == ["enter", "rollback"]
        # The rolled-back detection must not arm the cooldown.
        with _sweep_patches() as (mock_audit, _mock_notify, _mock_webhook):
            recovered = await maybe_alarm_approve_sweep(
                _alarm_session(6, 2), org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC)
            )
        assert recovered is True
        mock_audit.assert_awaited_once()

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

    async def test_audit_emission_failure_rolls_back_emission_savepoint(self):
        """A DB error on the emission audit append must not poison the
        decision transaction: the emission runs inside a savepoint whose
        rollback undoes it, the alarm stays no-throw, and no cooldown is
        armed."""
        events: list[str] = []

        @contextlib.asynccontextmanager
        async def _savepoint():
            events.append("enter")
            try:
                yield
            except Exception:
                events.append("rollback")
                raise
            events.append("release")

        session = _alarm_session(6, 2)
        session.begin_nested = MagicMock(side_effect=_savepoint)
        with _sweep_patches() as (mock_audit, _mock_notify, mock_webhook):
            mock_audit.side_effect = RuntimeError("audit append failed")
            alarmed = await maybe_alarm_approve_sweep(
                session, org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC)
            )
        assert alarmed is False
        # Two savepoints: the detection (released cleanly) and the emission
        # (rolled back by the audit failure).
        assert events == ["enter", "release", "enter", "rollback"]
        mock_webhook.assert_not_called()
        # The rolled-back emission must not arm the cooldown.
        with _sweep_patches() as (mock_audit2, _mock_notify, _mock_webhook):
            recovered = await maybe_alarm_approve_sweep(
                _alarm_session(6, 2), org_id=_ORG, actor_id=_USER, gate=_gate(), now=datetime.now(UTC)
            )
        assert recovered is True
        mock_audit2.assert_awaited_once()

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
        # The decision event types are bound parameters, not SQL literals.
        param_values = list(compiled.params.values())
        flat = [
            item for value in param_values for item in (list(value) if isinstance(value, (list, tuple)) else [value])
        ]
        for event_type in sweep_alarm._DECISION_EVENT_TYPES:
            assert event_type in flat

    async def test_count_query_covers_approve_and_manual_delivery(self):
        """Both decision surfaces are counted (FAR-611 review fix): the IN
        clause carries ``hitl.output_delivered`` AND ``hitl.manual_delivery``
        so a mixed approve + manual-delivery sweep trips one aggregate
        threshold."""
        session = _alarm_session(1, 1)
        await sweep_alarm.count_recent_approves(
            session,
            org_id=_ORG,
            actor_id=_USER,
            window_start=datetime.now(UTC) - timedelta(seconds=60),
        )
        stmt = session.execute.await_args.args[0]
        compiled = stmt.compile()
        assert "IN" in str(compiled).upper()
        param_values = list(compiled.params.values())
        flat = [
            item for value in param_values for item in (list(value) if isinstance(value, (list, tuple)) else [value])
        ]
        assert "hitl.output_delivered" in flat
        assert "hitl.manual_delivery" in flat

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
