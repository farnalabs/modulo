"""Tests for FAR-151 per-signal ingestion, seeded default alert rules, tombstones,
fire-once retry alerts, and alert_resolved lifecycle events.

Exercise the service layer in ``modulo.core.error_tracking`` with mocked
sessions/DB helpers (unit, no DB). SQLAlchemy objects are built from the real
models so column wiring (``signal``, ``is_default``) is covered.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.error_tracking import (
    DEFAULT_ALERT_RULES,
    SEEDED_DEFAULTS_VERSION,
    clear_default_rule_tombstone,
    emit_alert_resolved,
    emit_retry_deferred_alert,
    emit_signal_event,
    restore_default_alert_rules_for_org,
    seed_default_alert_rules,
    seed_default_alert_rules_for_org,
    signal_fingerprint,
    tombstone_default_rule,
)
from modulo.db.models.error_event import ErrorEvent
from modulo.db.models.error_notification_rule import DeletedDefault, ErrorNotificationRule

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _configure_begin(session: MagicMock) -> MagicMock:
    """Make a plain MagicMock session usable as ``async with factory() as session, session.begin():``.

    Mirrors AsyncSession's own context-manager protocol (``async with session:``
    begins a transaction): ``__aenter__`` must return the session itself, and
    ``session.begin()`` must return an async transaction context manager.
    """
    begin_cm = AsyncMock()
    begin_cm.__aenter__.return_value = session
    begin_cm.__aexit__.return_value = None
    session.begin.return_value = begin_cm
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


def _rule_mock(**overrides: object) -> MagicMock:
    rule = MagicMock(spec=ErrorNotificationRule)
    rule.id = uuid.uuid4()
    rule.name = "Old name"
    rule.enabled = True
    rule.condition_level = "error"
    rule.condition_min_count = 1
    rule.condition_window_seconds = 300
    rule.action_type = "in_app"
    rule.webhook_url = None
    rule.cooldown_seconds = 300
    rule.signal = "agent.failed"
    rule.is_default = True
    for k, v in overrides.items():
        setattr(rule, k, v)
    return rule


def _seed_session(
    *, tombstoned: set[str] | None = None, existing_rules: list[MagicMock | None] | None = None
) -> AsyncMock:
    """Session whose ``execute`` dispatches by table name.

    ``deleted_defaults`` queries return *tombstoned* signals; per-signal rule
    lookups return from *existing_rules* in DEFAULT_ALERT_RULES order.
    """
    session = _make_session()
    existing_iter = iter(existing_rules or [])
    tomb_result = MagicMock()
    tomb_result.all.return_value = [(s,) for s in (tombstoned or set())]
    tomb_result.scalar_one_or_none.return_value = None

    def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        compiled = str(stmt)
        if "deleted_defaults" in compiled:
            return tomb_result
        return MagicMock(scalar_one_or_none=MagicMock(return_value=next(existing_iter, None)))

    session.execute = AsyncMock(side_effect=_execute)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    return _configure_begin(session)


class TestSignalFingerprint:
    def test_stable_per_signal_and_pipeline(self) -> None:
        pipeline = uuid.uuid4()
        first = signal_fingerprint("agent.failed", pipeline)
        second = signal_fingerprint("agent.failed", pipeline)
        assert first == second

    def test_differs_by_pipeline(self) -> None:
        pipeline_a = uuid.uuid4()
        pipeline_b = uuid.uuid4()
        assert signal_fingerprint("agent.failed", pipeline_a) != signal_fingerprint("agent.failed", pipeline_b)

    def test_differs_by_signal(self) -> None:
        pipeline = uuid.uuid4()
        assert signal_fingerprint("agent.failed", pipeline) != signal_fingerprint("agent.stall", pipeline)

    def test_hex_length(self) -> None:
        assert len(signal_fingerprint("agent.failed", None)) == 64

    def test_no_pipeline_keeps_signal_partition(self) -> None:
        first = signal_fingerprint("agent.failed", None)
        second = signal_fingerprint("agent.failed", None)
        assert first == second
        assert signal_fingerprint("agent.failed", None) != signal_fingerprint("agent.no_op", None)


class TestSeedForOrg:
    async def test_adds_all_missing_signal_rules(self) -> None:
        session = _seed_session(existing_rules=[None] * len(DEFAULT_ALERT_RULES))
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            seeded = await seed_default_alert_rules_for_org(session, _ORG_ID)

        assert seeded == len(DEFAULT_ALERT_RULES)
        assert session.add.call_count == len(DEFAULT_ALERT_RULES)
        added_signals: set[str] = set()
        for call in session.add.call_args_list:
            rule = call.args[0]
            assert isinstance(rule, ErrorNotificationRule)
            assert rule.organisation_id == _ORG_ID
            assert rule.is_default is True
            assert rule.signal in {spec["signal"] for spec in DEFAULT_ALERT_RULES}
            added_signals.add(rule.signal)
        assert added_signals == {spec["signal"] for spec in DEFAULT_ALERT_RULES}

    async def test_idempotent_when_all_present(self) -> None:
        existing = [_rule_mock(signal=spec["signal"]) for spec in DEFAULT_ALERT_RULES]
        session = _seed_session(existing_rules=existing)
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            seeded = await seed_default_alert_rules_for_org(session, _ORG_ID)
        # All rows exist and are is_default=true → force-updated, never duplicated.
        assert seeded == len(DEFAULT_ALERT_RULES)
        assert session.add.call_count == 0

    async def test_skips_tombstoned_signals(self) -> None:
        tombstoned = {DEFAULT_ALERT_RULES[0]["signal"]}
        session = _seed_session(tombstoned=tombstoned, existing_rules=[None] * len(DEFAULT_ALERT_RULES))
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            seeded = await seed_default_alert_rules_for_org(session, _ORG_ID)

        assert seeded == len(DEFAULT_ALERT_RULES) - 1
        added_signals = {call.args[0].signal for call in session.add.call_args_list}
        assert tombstoned.isdisjoint(added_signals)

    async def test_never_touches_edited_rows(self) -> None:
        edited = _rule_mock(signal=DEFAULT_ALERT_RULES[0]["signal"], is_default=False, name="User custom")
        existing = [edited] + [None] * (len(DEFAULT_ALERT_RULES) - 1)
        session = _seed_session(existing_rules=existing)
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            seeded = await seed_default_alert_rules_for_org(session, _ORG_ID)

        # The edited row is not force-updated and not duplicated.
        assert edited.name == "User custom"
        assert seeded == len(DEFAULT_ALERT_RULES) - 1

    async def test_version_bump_force_updates_never_edited_rows(self) -> None:
        spec = DEFAULT_ALERT_RULES[0]
        existing_rule = _rule_mock(signal=spec["signal"], is_default=True, name="Stale", action_type="email")
        session = _seed_session(existing_rules=[existing_rule] + [None] * (len(DEFAULT_ALERT_RULES) - 1))
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            await seed_default_alert_rules_for_org(session, _ORG_ID)

        assert existing_rule.name == spec["name"]
        assert existing_rule.condition_level == spec["level"]
        assert existing_rule.action_type == spec["action_type"]


class TestSeedAllOrgs:
    def _marker_session(self, marker_value: object | None) -> MagicMock:
        session = MagicMock()
        marker = MagicMock() if marker_value is not None else None
        if marker is not None:
            marker.value = marker_value

        def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            compiled = str(stmt)
            if "system_config" in compiled:
                return MagicMock(scalar_one_or_none=MagicMock(return_value=marker))
            if "organisations" in compiled:
                return MagicMock(all=MagicMock(return_value=[(uuid.uuid4(),)]))
            if "deleted_defaults" in compiled:
                return MagicMock(all=MagicMock(return_value=[]))
            return MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        session.execute = AsyncMock(side_effect=_execute)
        session.add = MagicMock()
        session.flush = AsyncMock()
        return _configure_begin(session)

    def _factory(self, session: AsyncMock) -> MagicMock:
        factory = MagicMock()
        factory.return_value = session
        return factory

    async def test_first_seed_seeds_and_writes_marker(self) -> None:
        session = self._marker_session(None)
        factory = self._factory(session)
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            seeded = await seed_default_alert_rules(factory)

        assert seeded == len(DEFAULT_ALERT_RULES)
        system_config_added = any(
            getattr(call.args[0], "key", None) == "seeded_defaults_version" for call in session.add.call_args_list
        )
        assert system_config_added

    async def test_second_seed_returns_zero(self) -> None:
        session = self._marker_session(SEEDED_DEFAULTS_VERSION)
        factory = self._factory(session)
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            seeded = await seed_default_alert_rules(factory)
        assert seeded == 0
        assert session.add.call_count == 0

    async def test_version_bump_reseeds(self) -> None:
        session = self._marker_session(SEEDED_DEFAULTS_VERSION - 1)
        factory = self._factory(session)
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            seeded = await seed_default_alert_rules(factory)
        assert seeded == len(DEFAULT_ALERT_RULES)


class TestTombstones:
    async def test_tombstone_then_seed_skips(self) -> None:
        signal = DEFAULT_ALERT_RULES[0]["signal"]
        session = _seed_session(tombstoned=set(), existing_rules=[None] * len(DEFAULT_ALERT_RULES))
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            await tombstone_default_rule(session, _ORG_ID, signal)
        added = session.add.call_args.args[0]
        assert isinstance(added, DeletedDefault)
        assert added.signal == signal
        assert added.organisation_id == _ORG_ID

    async def test_clear_tombstone_returns_true_only_when_present(self) -> None:
        row = MagicMock(spec=DeletedDefault)
        session = _make_session()
        result = MagicMock(scalar_one_or_none=MagicMock(return_value=row))
        session.execute = AsyncMock(return_value=result)
        session.delete = AsyncMock()
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            cleared = await clear_default_rule_tombstone(session, _ORG_ID, "agent.failed")
        assert cleared is True
        session.delete.assert_called_once_with(row)

    async def test_restore_clears_tombstones_and_reseeds(self) -> None:
        row = MagicMock(spec=DeletedDefault)
        session = _make_session()
        tomb_result = MagicMock()
        tomb_result.scalars.return_value.all.return_value = [row]
        tomb_result.all.return_value = []
        tomb_result.scalar_one_or_none.return_value = None
        rules_result = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            if "deleted_defaults" in str(stmt):
                return tomb_result
            return rules_result

        session.execute = AsyncMock(side_effect=_execute)
        session.delete = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        with patch("modulo.core.error_tracking.set_rls_org", AsyncMock()):
            seeded = await restore_default_alert_rules_for_org(session, _ORG_ID)
        assert seeded == len(DEFAULT_ALERT_RULES)
        session.delete.assert_called_once_with(row)


class TestEmitSignalEvent:
    async def test_creates_event_with_stable_fingerprint_and_evaluates(self) -> None:
        session = _make_session()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.count = 1
        engine = MagicMock()
        engine.evaluate = AsyncMock(return_value=[])
        with (
            patch("modulo.core.error_tracking.get_error_group_by_fingerprint", AsyncMock(return_value=None)),
            patch("modulo.core.error_tracking.upsert_error_group", AsyncMock(return_value=group)) as upsert,
            patch("modulo.core.error_tracking._get_alert_engine", AsyncMock(return_value=engine)),
        ):
            result = await emit_signal_event(
                session,
                _ORG_ID,
                signal="agent.failed",
                pipeline_id=None,
                message="Agent failed on retry",
                level="critical",
                run_id="run-1",
            )

        assert result["group_id"] == str(group.id)
        assert result["is_new"] is True
        added = session.add.call_args.args[0]
        assert isinstance(added, ErrorEvent)
        assert added.signal == "agent.failed"
        assert added.fingerprint == signal_fingerprint("agent.failed", None)
        upsert.assert_awaited_once()
        engine.evaluate.assert_awaited_once()
        kwargs = engine.evaluate.await_args.kwargs
        assert kwargs["signal"] == "agent.failed"
        assert kwargs["run_id"] == "run-1"

    async def test_dispatch_all_called_when_rules_fire(self) -> None:
        session = _make_session()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.count = 1
        alert = MagicMock()
        engine = MagicMock()
        engine.evaluate = AsyncMock(return_value=[alert])
        engine.dispatch_all = AsyncMock()
        with (
            patch("modulo.core.error_tracking.get_error_group_by_fingerprint", AsyncMock(return_value=None)),
            patch("modulo.core.error_tracking.upsert_error_group", AsyncMock(return_value=group)),
            patch("modulo.core.error_tracking._get_alert_engine", AsyncMock(return_value=engine)),
        ):
            await emit_signal_event(
                session,
                _ORG_ID,
                signal="agent.stall",
                pipeline_id=None,
                message="stalled",
                level="warning",
            )
        engine.dispatch_all.assert_awaited_once()

    async def test_evaluation_failure_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _make_session()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.count = 1
        engine = MagicMock()
        engine.evaluate = AsyncMock(side_effect=RuntimeError("db down"))
        with (
            patch("modulo.core.error_tracking.get_error_group_by_fingerprint", AsyncMock(return_value=None)),
            patch("modulo.core.error_tracking.upsert_error_group", AsyncMock(return_value=group)),
            patch("modulo.core.error_tracking._get_alert_engine", AsyncMock(return_value=engine)),
        ):
            result = await emit_signal_event(
                session, _ORG_ID, signal="contract.schema", pipeline_id=None, message="x", level="warning"
            )
        assert "group_id" in result
        assert "error_tracking.signal_alert_evaluation_failed" in caplog.text


class TestRetryDeferredAlertFireOnce:
    async def test_fires_once_per_run_group_signal(self) -> None:
        session = _make_session()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.count = 1
        engine = MagicMock()
        engine.evaluate = AsyncMock(return_value=[])
        run_group_id = uuid.uuid4()
        with (
            patch("modulo.core.error_tracking.upsert_error_group", AsyncMock(return_value=group)),
            patch("modulo.core.error_tracking._get_alert_engine", AsyncMock(return_value=engine)),
        ):
            first = await emit_retry_deferred_alert(
                session,
                _ORG_ID,
                run_id="run-1",
                run_group_id=run_group_id,
                signal="agent.failed",
                pipeline_id=None,
                message="retry superseded",
                attempt_n=2,
                reason="superseded by newer run",
            )
            second = await emit_retry_deferred_alert(
                session,
                _ORG_ID,
                run_id="run-2",
                run_group_id=run_group_id,
                signal="agent.failed",
                pipeline_id=None,
                message="retry superseded again",
                attempt_n=3,
                reason="superseded by newer run",
            )

        assert first is True
        assert second is False
        # Only the first emission evaluated rules.
        engine.evaluate.assert_awaited_once()
        kwargs = engine.evaluate.await_args.kwargs
        assert kwargs["attempt_n"] == 2
        assert str(kwargs["run_group_id"]) == str(run_group_id)
        assert kwargs["elevation_signal"] == "agent.failed"

    async def test_different_run_group_fires_again(self) -> None:
        session = _make_session()
        group = MagicMock()
        group.id = uuid.uuid4()
        group.count = 1
        engine = MagicMock()
        engine.evaluate = AsyncMock(return_value=[])
        with (
            patch("modulo.core.error_tracking.upsert_error_group", AsyncMock(return_value=group)),
            patch("modulo.core.error_tracking._get_alert_engine", AsyncMock(return_value=engine)),
        ):
            await emit_retry_deferred_alert(
                session,
                _ORG_ID,
                run_id="r1",
                run_group_id=uuid.uuid4(),
                signal="agent.failed",
                pipeline_id=None,
                message="m",
                attempt_n=2,
                reason="superseded",
            )
            second = await emit_retry_deferred_alert(
                session,
                _ORG_ID,
                run_id="r2",
                run_group_id=uuid.uuid4(),
                signal="agent.failed",
                pipeline_id=None,
                message="m",
                attempt_n=2,
                reason="superseded",
            )
        assert second is True


class TestEmitAlertResolved:
    async def test_dispatches_resolution_with_rule_webhook(self) -> None:
        session = _make_session()
        rule = _rule_mock(signal="agent.failed", webhook_url="https://hooks.example.com/resolve")
        rule_result = MagicMock(scalar_one_or_none=MagicMock(return_value=rule))
        session.execute = AsyncMock(return_value=rule_result)
        group_id = uuid.uuid4()

        with patch(
            "modulo.core.error_tracking.alert_dispatcher.dispatch_alert_resolved", new=AsyncMock()
        ) as resolved_mock:
            await emit_alert_resolved(session, _ORG_ID, signal="agent.failed", group_id=group_id, reason="superseded")

        resolved_mock.assert_awaited_once()
        kwargs = resolved_mock.await_args.kwargs
        assert kwargs["webhook_url"] == "https://hooks.example.com/resolve"
        assert str(kwargs["group_id"]) == str(group_id)
        assert kwargs["signal"] == "agent.failed"
        assert kwargs["reason"] == "superseded"

    async def test_dispatch_failure_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _make_session()
        rule_result = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        session.execute = AsyncMock(return_value=rule_result)
        with patch(
            "modulo.core.error_tracking.alert_dispatcher.dispatch_alert_resolved",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            await emit_alert_resolved(
                session, _ORG_ID, signal="agent.failed", group_id=uuid.uuid4(), reason="superseded"
            )
        assert "error_tracking.alert_resolved_dispatch_failed" in caplog.text
