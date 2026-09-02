"""Unit tests for the ``ongoing`` trigger top-up (FAR-158).

Covers ``_ongoing_topup`` (the DB phase: lock -> pause -> spend -> count ->
effective target -> create runs -> events -> last_fired_at; does NOT commit),
``_dispatch_ongoing_runs`` (post-commit queue phase), ``fire_ongoing_trigger``
(the transaction + dispatch wrapper), the ``_count_ongoing_runs`` status-only
predicate, ``_advance_ongoing_next_fire``, the persistent-failure deactivation
guard, and the SAQ worker wiring.

Mock/fake based (no real Postgres/Redis). The multi-machine race is covered at
the control-flow level exactly like the cron/polling tests: ``_ongoing_topup``
reads a fixed statement-routed mocked session and ``create_run`` / event
logging are data-driven via patched side effects.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch

ORG = uuid.uuid4()
TRIGGER_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()


def _mock_result(**kwargs: Any) -> MagicMock:
    result = MagicMock()
    for name, value in kwargs.items():
        getattr(result, name).return_value = value
    return result


def _make_trigger(**overrides: Any) -> MagicMock:
    """Trigger-like double with the defaults the top-up reads."""
    from modulo.db.models.trigger import Trigger

    defaults: dict[str, Any] = {
        "id": TRIGGER_ID,
        "organisation_id": ORG,
        "pipeline_id": PIPELINE_ID,
        "active": True,
        "max_concurrent_runs": 3,
        "daily_spend_limit": None,
        # A pinned snapshot by default so the top-up takes the (mocked-row)
        # pinned-resolution path; snapshot-specific tests override this.
        "config_json": {"snapshot_id": str(uuid.uuid4())},
        "cron_timezone": None,
    }
    defaults.update(overrides)
    trigger = MagicMock(spec=Trigger)
    for key, value in defaults.items():
        setattr(trigger, key, value)
    return trigger


class _Begin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


# Sentinel: resolve the snapshot from the trigger's pinned config_json. Passing
# an explicit ``snapshot=None`` means "no snapshot row" (pinned-but-missing).
_AUTO_SNAPSHOT = object()


class _RoutedSession:
    """Async session double routing statements to canned results (postgresql).

    ``_count_ongoing_runs`` / ``_log_ongoing_event`` / ``_org_is_paused_degraded``
    are patched in the tests, so only the lock / trigger / pipeline / snapshot /
    cost / update statements reach ``execute``.
    """

    def __init__(
        self,
        *,
        trigger: MagicMock,
        pipeline_max: int = 10,
        snapshot: Any = _AUTO_SNAPSHOT,
        lock: bool = True,
        today_cost: Decimal = Decimal(0),
    ) -> None:
        self._trigger = trigger
        self._pipeline_max = pipeline_max
        self._lock = lock
        self._today_cost = today_cost
        if snapshot is _AUTO_SNAPSHOT:
            pinned = (trigger.config_json or {}).get("snapshot_id")
            try:
                snapshot = uuid.UUID(str(pinned)) if pinned else None
            except (ValueError, TypeError):
                snapshot = None
        self._snapshot = snapshot
        self.executed: list[tuple[Any, Any]] = []
        self.added: list[object] = []
        self.begin_cm = _Begin()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return self.begin_cm

    def begin_nested(self) -> _Begin:
        return _Begin()

    def get_bind(self) -> Any:
        return self._get_bind()

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        s = str(stmt).lower()
        if "set_config" in s:
            return MagicMock()
        if "try_advisory" in s:
            return _mock_result(scalar_one=self._lock)
        if "update triggers" in s:
            return MagicMock()
        if "from triggers" in s:
            return _mock_result(scalar_one_or_none=self._trigger)
        if "from pipelines" in s:
            return _mock_result(scalar_one_or_none=self._pipeline_max)
        if "from pipeline_snapshots" in s:
            return _mock_result(scalar_one_or_none=self._snapshot)
        if "total_cost_usd" in s or "coalesce" in s:
            return _mock_result(scalar_one=self._today_cost)
        return MagicMock()


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
        "fernet_key": "b" * 44,
    }
    base.update(overrides)
    return MagicMock(**base)


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


def _make_run() -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    return run


def _make_run_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
    return _make_run()


async def _run_topup(
    session: _RoutedSession,
    *,
    now: datetime,
    in_flight: int = 0,
    latest_snapshot_id: uuid.UUID | None = None,
    redis_client: Any = None,
    outcome: dict[str, Any] | None = None,
    create_run: AsyncMock | None = None,
    paused: bool = False,
) -> tuple[list[Any], AsyncMock, AsyncMock]:
    """Drive ``_ongoing_topup`` with the standard patch set and await it.

    ``_count_ongoing_runs`` is stubbed to ``in_flight`` (the status-only
    semantics are pinned separately), ``_org_is_paused_degraded`` to
    ``paused``, ``_log_ongoing_event`` to a recorder, and ``create_run`` (when
    given) to ``_make_run_side_effect``.

    Returns ``(created_runs, create_run_mock, log_event_mock)``.
    """
    if create_run is None:
        create_run = AsyncMock(side_effect=_make_run_side_effect)
    with (
        patch.object(ch, "_count_ongoing_runs", new_callable=AsyncMock, return_value=in_flight),
        patch.object(ch, "_org_is_paused_degraded", new_callable=AsyncMock, return_value=paused),
        patch.object(ch, "_log_ongoing_event", new_callable=AsyncMock) as log_event,
        patch("modulo.db.crud.run.create_run", create_run),
    ):
        created = await ch._ongoing_topup(
            session,
            trigger_id=TRIGGER_ID,
            org_id=ORG,
            pipeline_id=PIPELINE_ID,
            now=now,
            latest_snapshot_id=latest_snapshot_id,
            redis_client=redis_client,
            outcome=outcome,
        )
    return created, create_run, log_event


# ---------------------------------------------------------------------------
# _count_ongoing_runs — status-only predicate
# ---------------------------------------------------------------------------


class TestCountOngoingRuns:
    @pytest.mark.asyncio
    async def test_predicate_counts_pending_running_claimed_only(self) -> None:
        """The count is status-only over ONGOING_ACTIVE_STATUSES: pending counts
        (the queued semantic), awaiting_human is EXCLUDED, and there is NO
        cancellation_requested filter."""
        executed: list[Any] = []

        async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
            executed.append(stmt)
            return _mock_result(scalar_one=2)

        session = _RoutedSession(trigger=_make_trigger())
        session.execute = _execute  # type: ignore[method-assign]
        count = await ch._count_ongoing_runs(session, TRIGGER_ID)

        assert count == 2
        assert len(executed) == 1
        # The IN-list is an expanding bind param — inline the values to inspect
        # the actual status vocabulary.
        sql = str(executed[0].compile(compile_kwargs={"literal_binds": True}))
        assert "runs.status IN" in sql
        assert "pending" in sql
        assert "running" in sql
        assert "claimed" in sql
        for excluded in ("awaiting_human", "stalled", "eval_failed", "cancelled"):
            assert excluded not in sql, f"{excluded} must not be counted"
        assert "cancellation_requested" not in sql, "count is status-only — no cancellation filter"

    @pytest.mark.asyncio
    async def test_topup_creates_run_when_awaiting_human_run_parked(self) -> None:
        """A never-answered HITL gate must not starve the pool: with 1
        awaiting_human run parked (excluded from ONGOING_ACTIVE_STATUSES) and a
        target of 1, the top-up still creates 1 run."""
        now = datetime.now(UTC)
        session = _RoutedSession(
            trigger=_make_trigger(max_concurrent_runs=1, config_json={"snapshot_id": str(uuid.uuid4())})
        )
        outcome: dict[str, Any] = {}
        created, create_run, _ = await _run_topup(session, now=now, in_flight=0, outcome=outcome)

        assert len(created) == 1
        create_run.assert_awaited_once()


# ---------------------------------------------------------------------------
# _ongoing_topup — creation / no-op matrix
# ---------------------------------------------------------------------------


class TestOngoingTopup:
    @pytest.mark.asyncio
    async def test_below_target_creates_exactly_target_minus_in_flight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger(max_concurrent_runs=3))
        outcome: dict[str, Any] = {}
        created, create_run, log_event = await _run_topup(session, now=now, in_flight=1, outcome=outcome)

        assert len(created) == 2, "must create exactly (target 3 - in_flight 1)"
        assert create_run.await_count == 2
        assert log_event.await_count == 2
        for call in log_event.await_args_list:
            assert call.kwargs["result"] == "accepted"
            assert call.kwargs["run_id"] is not None
        # last_fired_at is stamped only when runs are created.
        assert any(
            "update triggers" in str(s).lower() and "last_fired_at" in str(s).lower() for s, _ in session.executed
        )

    @pytest.mark.asyncio
    async def test_at_target_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger(max_concurrent_runs=3))
        outcome: dict[str, Any] = {}
        created, create_run, log_event = await _run_topup(session, now=now, in_flight=3, outcome=outcome)

        assert created == []
        assert outcome["status"] == "noop"
        assert outcome["in_flight"] == 3
        create_run.assert_not_called()
        log_event.assert_not_called()
        assert not any("update triggers" in str(s).lower() for s, _ in session.executed), (
            "no-op must not stamp last_fired_at"
        )

    @pytest.mark.asyncio
    async def test_above_target_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger(max_concurrent_runs=3))
        outcome: dict[str, Any] = {}
        created, _, _ = await _run_topup(session, now=now, in_flight=5, outcome=outcome)

        assert created == []
        assert outcome["status"] == "noop"

    @pytest.mark.asyncio
    async def test_effective_target_is_min_of_trigger_and_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        # Pipeline cap (2) lower than trigger target (5) -> 2 created.
        session = _RoutedSession(trigger=_make_trigger(max_concurrent_runs=5), pipeline_max=2)
        created, create_run, _ = await _run_topup(session, now=now, in_flight=0)
        assert len(created) == 2
        create_run.assert_awaited()
        # Pipeline cap raised above the trigger target -> trigger target governs.
        session = _RoutedSession(trigger=_make_trigger(max_concurrent_runs=3), pipeline_max=10)
        created, create_run, _ = await _run_topup(session, now=now, in_flight=0)
        assert len(created) == 3

    @pytest.mark.asyncio
    async def test_pipeline_missing_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger())
        session._pipeline_max = None
        outcome: dict[str, Any] = {}
        created, create_run, _ = await _run_topup(session, now=now, in_flight=0, outcome=outcome)
        assert created == []
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "pipeline_not_found"
        create_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_inactive_trigger_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger(active=False))
        outcome: dict[str, Any] = {}
        created, create_run, _ = await _run_topup(session, now=now, in_flight=0, outcome=outcome)
        assert created == []
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "trigger_inactive_or_missing"
        create_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_trigger_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger())
        session._trigger = None
        outcome: dict[str, Any] = {}
        created, create_run, _ = await _run_topup(session, now=now, in_flight=0, outcome=outcome)
        assert created == []
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "trigger_inactive_or_missing"
        create_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_trigger_read_filters_soft_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The trigger SELECT filters deleted_at IS NULL — a soft-deleted ongoing
        trigger can never top up."""
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger())
        await _run_topup(session, now=now, in_flight=0)
        trigger_stmts = [s for s, _ in session.executed if "from triggers" in str(s).lower()]
        assert trigger_stmts
        assert "deleted_at IS NULL" in str(trigger_stmts[0])

    @pytest.mark.asyncio
    async def test_busy_lock_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger(), lock=False)
        outcome: dict[str, Any] = {}
        created, create_run, _ = await _run_topup(session, now=now, in_flight=0, outcome=outcome)
        assert created == []
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "trigger_busy"
        create_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_spend_limit_reached_skips_and_stamps_last_fired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Over-budget daemon: spend_limit_reached event, skip-NOT-defer
        (last_fired_at still stamped), zero runs."""
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        trigger = _make_trigger(daily_spend_limit=Decimal("50.00"))
        session = _RoutedSession(trigger=trigger, today_cost=Decimal("60.00"))
        outcome: dict[str, Any] = {}
        created, create_run, log_event = await _run_topup(session, now=now, in_flight=0, outcome=outcome)

        assert created == []
        create_run.assert_not_called()
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "spend_limit"
        assert log_event.await_count == 1
        assert log_event.await_args.kwargs["result"] == "spend_limit_reached"
        assert any(
            "update triggers" in str(s).lower() and "last_fired_at" in str(s).lower() for s, _ in session.executed
        )

    @pytest.mark.asyncio
    async def test_spend_limit_below_creates_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        trigger = _make_trigger(max_concurrent_runs=2, daily_spend_limit=Decimal("50.00"))
        session = _RoutedSession(trigger=trigger, today_cost=Decimal("10.00"))
        created, create_run, _ = await _run_topup(session, now=now, in_flight=0)
        assert len(created) == 2
        create_run.assert_awaited()

    @pytest.mark.asyncio
    async def test_org_paused_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger())
        outcome: dict[str, Any] = {}
        created, create_run, log_event = await _run_topup(session, now=now, in_flight=0, outcome=outcome, paused=True)
        assert created == []
        create_run.assert_not_called()
        log_event.assert_not_called()
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "triggers_paused"

    @pytest.mark.asyncio
    async def test_triggers_paused_mid_loop_stops_creating(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TOCTOU race backstop: the org pauses MID top-up -> the loop stops, the
        runs already created are returned (they dispatch below), no SAQ retry
        storm from an unhandled exception."""
        _patch_env(monkeypatch)
        from modulo.core.exceptions import TriggersPausedError

        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger(max_concurrent_runs=5))
        create_run = AsyncMock(side_effect=[_make_run(), TriggersPausedError(org_id=ORG)])
        outcome: dict[str, Any] = {}
        created, mock_cr, _ = await _run_topup(session, now=now, in_flight=0, outcome=outcome, create_run=create_run)
        assert len(created) == 1
        assert mock_cr.await_count == 2  # second call raised
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "triggers_paused"


# ---------------------------------------------------------------------------
# _ongoing_topup — snapshot resolution
# ---------------------------------------------------------------------------


class TestOngoingSnapshotResolution:
    @pytest.mark.asyncio
    async def test_pinned_valid_snapshot_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        pinned = uuid.uuid4()
        session = _RoutedSession(
            trigger=_make_trigger(config_json={"snapshot_id": str(pinned), "input_template": {"topic": "x"}}),
            snapshot=pinned,
        )
        created, create_run, _ = await _run_topup(session, now=now, in_flight=0)
        assert len(created) == 3
        for call in create_run.await_args_list:
            assert call.kwargs["snapshot_id"] == pinned
            assert call.kwargs["trigger_type"] == "ongoing"
            assert call.kwargs["input_payload"] == {"topic": "x"}

    @pytest.mark.asyncio
    async def test_pinned_snapshot_missing_no_pipeline_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pinned-but-missing -> explicit no_pipeline event + skip (NEVER a
        silent auto-create)."""
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        pinned = uuid.uuid4()
        session = _RoutedSession(trigger=_make_trigger(config_json={"snapshot_id": str(pinned)}), snapshot=None)
        outcome: dict[str, Any] = {}
        created, create_run, log_event = await _run_topup(session, now=now, in_flight=0, outcome=outcome)
        assert created == []
        create_run.assert_not_called()
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "pinned_snapshot_missing"
        assert log_event.await_args.kwargs["result"] == "no_pipeline"

    @pytest.mark.asyncio
    async def test_invalid_pinned_snapshot_no_pipeline_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger(config_json={"snapshot_id": "not-a-uuid"}))
        outcome: dict[str, Any] = {}
        created, create_run, log_event = await _run_topup(session, now=now, in_flight=0, outcome=outcome)
        assert created == []
        create_run.assert_not_called()
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "invalid_pinned_snapshot"
        assert log_event.await_args.kwargs["result"] == "no_pipeline"

    @pytest.mark.asyncio
    async def test_no_pin_uses_latest_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        latest = uuid.uuid4()
        session = _RoutedSession(trigger=_make_trigger(config_json={}))
        created, create_run, _ = await _run_topup(session, now=now, in_flight=0, latest_snapshot_id=latest)
        assert len(created) == 3
        for call in create_run.await_args_list:
            assert call.kwargs["snapshot_id"] == latest

    @pytest.mark.asyncio
    async def test_no_pin_no_latest_auto_creates_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        auto_id = uuid.uuid4()
        session = _RoutedSession(trigger=_make_trigger(config_json={}))
        with patch(
            "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(id=auto_id),
        ) as auto_create:
            created, create_run, _ = await _run_topup(session, now=now, in_flight=0)
        auto_create.assert_awaited_once()
        assert len(created) == 3
        for call in create_run.await_args_list:
            assert call.kwargs["snapshot_id"] == auto_id

    @pytest.mark.asyncio
    async def test_auto_create_failure_no_pipeline_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger(config_json={}))
        outcome: dict[str, Any] = {}
        with patch(
            "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            return_value=None,
        ) as auto_create:
            created, create_run, log_event = await _run_topup(session, now=now, in_flight=0, outcome=outcome)
        auto_create.assert_awaited_once()
        assert created == []
        create_run.assert_not_called()
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "pipeline_not_found"
        assert log_event.await_args.kwargs["result"] == "no_pipeline"


# ---------------------------------------------------------------------------
# Persistent-failure deactivation
# ---------------------------------------------------------------------------


class TestPersistentFailureDeactivation:
    @pytest.mark.asyncio
    async def test_five_consecutive_failures_deactivates_trigger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        pinned = uuid.uuid4()
        session = _RoutedSession(trigger=_make_trigger(config_json={"snapshot_id": str(pinned)}), snapshot=None)
        redis_client = AsyncMock()
        redis_client.incr.return_value = ch.ONGOING_MAX_CONSECUTIVE_FAILURES
        await _run_topup(session, now=now, in_flight=0, redis_client=redis_client)

        update_stmts = [s for s, _ in session.executed if "update triggers" in str(s).lower()]
        assert update_stmts, "deactivation must issue an UPDATE triggers"
        assert "active" in str(update_stmts[0]).lower()

    @pytest.mark.asyncio
    async def test_failure_below_cap_does_not_deactivate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        pinned = uuid.uuid4()
        session = _RoutedSession(trigger=_make_trigger(config_json={"snapshot_id": str(pinned)}), snapshot=None)
        redis_client = AsyncMock()
        redis_client.incr.return_value = ch.ONGOING_MAX_CONSECUTIVE_FAILURES - 1
        await _run_topup(session, now=now, in_flight=0, redis_client=redis_client)
        assert not any("update triggers" in str(s).lower() for s, _ in session.executed)

    @pytest.mark.asyncio
    async def test_bump_without_redis_is_noop(self) -> None:
        session = _RoutedSession(trigger=_make_trigger())
        await ch._bump_ongoing_failure(session, None, TRIGGER_ID)
        assert not session.executed

    @pytest.mark.asyncio
    async def test_clear_without_redis_is_noop(self) -> None:
        assert await ch._clear_ongoing_failure(None, TRIGGER_ID) is None

    @pytest.mark.asyncio
    async def test_success_clears_failure_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        now = datetime.now(UTC)
        session = _RoutedSession(trigger=_make_trigger())
        redis_client = AsyncMock()
        await _run_topup(session, now=now, in_flight=0, redis_client=redis_client)
        redis_client.delete.assert_awaited_once_with(ch._ongoing_failure_key(TRIGGER_ID))


# ---------------------------------------------------------------------------
# _dispatch_ongoing_runs — post-commit queue phase
# ---------------------------------------------------------------------------


class TestDispatchOngoingRuns:
    @pytest.mark.asyncio
    async def test_one_failing_dispatch_does_not_abort_the_rest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        run_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        with (
            patch.object(ch, "get_settings", return_value=_settings()),
            patch(
                "modulo.core.dispatch.dispatch_run",
                new_callable=AsyncMock,
                side_effect=[RuntimeError("redis down"), ("ok", "job-2"), ("ok", "job-3")],
            ),
        ):
            outcomes = await ch._dispatch_ongoing_runs(None, ORG, run_ids)

        assert len(outcomes) == 3
        assert outcomes[0] == {"run_id": str(run_ids[0]), "outcome": "dispatch_error", "job_id": None}
        assert outcomes[1] == {"run_id": str(run_ids[1]), "outcome": "ok", "job_id": "job-2"}
        assert outcomes[2] == {"run_id": str(run_ids[2]), "outcome": "ok", "job_id": "job-3"}

    @pytest.mark.asyncio
    async def test_empty_run_list_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        with (
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.dispatch.dispatch_run", new_callable=AsyncMock) as dispatch,
        ):
            outcomes = await ch._dispatch_ongoing_runs(None, ORG, [])
        assert outcomes == []
        dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# fire_ongoing_trigger — transaction + dispatch wrapper
# ---------------------------------------------------------------------------


class TestFireOngoingTrigger:
    @pytest.mark.asyncio
    async def test_retry_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """fire_ongoing_trigger called twice: the first call commits 2 runs, so
        the second (a SAQ retry) counts those 2 as in-flight and creates 0."""
        _patch_env(monkeypatch)
        session = _RoutedSession(trigger=_make_trigger(max_concurrent_runs=2))
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        committed_in_flight: dict[str, int] = {}

        async def fake_count(s: Any, trigger_id: uuid.UUID) -> int:
            return committed_in_flight.get(str(trigger_id), 0)

        async def fake_dispatch(
            q: Any, org_id: uuid.UUID, run_ids: list[uuid.UUID], redis_client: Any = None
        ) -> list[dict[str, Any]]:
            committed_in_flight[str(TRIGGER_ID)] = len(run_ids)
            return [{"run_id": str(rid), "outcome": "ok", "job_id": None} for rid in run_ids]

        with (
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_ongoing_runs", new_callable=AsyncMock, side_effect=fake_count),
            patch.object(ch, "_org_is_paused_degraded", new_callable=AsyncMock, return_value=False),
            patch.object(ch, "_log_ongoing_event", new_callable=AsyncMock),
            patch("modulo.db.crud.run.create_run", new_callable=AsyncMock, side_effect=_make_run_side_effect),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "_dispatch_ongoing_runs", new_callable=AsyncMock, side_effect=fake_dispatch),
        ):
            redis_cls.from_url.return_value = redis_client
            first = await ch.fire_ongoing_trigger(
                trigger_id=TRIGGER_ID,
                org_id=ORG,
                pipeline_id=PIPELINE_ID,
                latest_snapshot_id=str(uuid.uuid4()),
            )
            second = await ch.fire_ongoing_trigger(
                trigger_id=TRIGGER_ID,
                org_id=ORG,
                pipeline_id=PIPELINE_ID,
                latest_snapshot_id=str(uuid.uuid4()),
            )

        assert first["status"] == "fired"
        assert first["created"] == 2
        assert len(first["dispatched"]) == 2
        assert second["status"] == "noop"
        assert second["created"] == 0
        assert not second["dispatched"]
        redis_client.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_stats_write_sets_self_expiring_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """2026-09 Redis audit: the per-trigger debug summary key
        (``saq:cron:stats:ongoing:{trigger_id}``) carries a TTL so keys for
        DELETED triggers self-expire instead of accumulating forever (the
        unbounded keyspace-growth offender). No consumer gates on this key —
        debug-only — so expiry loses no signal."""
        _patch_env(monkeypatch)
        session = _RoutedSession(trigger=_make_trigger(max_concurrent_runs=2))
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()

        with (
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ch, "_count_ongoing_runs", new_callable=AsyncMock, return_value=0),
            patch.object(ch, "_org_is_paused_degraded", new_callable=AsyncMock, return_value=False),
            patch.object(ch, "_log_ongoing_event", new_callable=AsyncMock),
            patch("modulo.db.crud.run.create_run", new_callable=AsyncMock, side_effect=_make_run_side_effect),
            patch.object(ch, "AsyncRedis") as redis_cls,
            patch.object(ch, "_dispatch_ongoing_runs", new_callable=AsyncMock, return_value=[]),
        ):
            redis_cls.from_url.return_value = redis_client
            await ch.fire_ongoing_trigger(
                trigger_id=TRIGGER_ID,
                org_id=ORG,
                pipeline_id=PIPELINE_ID,
                latest_snapshot_id=str(uuid.uuid4()),
            )

        stats_sets = [
            c for c in redis_client.set.await_args_list if c.args[0] == f"{ch._ONGOING_STATS_KEY_PREFIX}:{TRIGGER_ID}"
        ]
        assert stats_sets, "fire_ongoing_trigger must persist its per-trigger summary"
        assert stats_sets[0].kwargs["ex"] == ch._ONGOING_STATS_TTL_SECONDS
        # Derived from the ongoing cadence floor, not magic: a live trigger
        # refires at most every ONGOING_MIN_INTERVAL_SECONDS, so the TTL (10x)
        # always outlasts the refresh gap and a live trigger's key never
        # lapses between fires.
        assert ch._ONGOING_STATS_TTL_SECONDS > ch.ONGOING_MIN_INTERVAL_SECONDS

    def test_never_dispatched_pending_matches_reconcile_predicate(self) -> None:
        """A committed-but-never-dispatched pending run (dispatched_at IS NULL)
        is admitted by the first dispatcher_reconcile recovery branch."""
        from sqlalchemy.dialects import postgresql

        predicate = ch._build_re_dispatch_predicate(
            reenqueue_window=300,
            stale_window=600,
            capacity_redispatch_seconds=120,
        )
        sql = str(predicate.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        assert "runs.dispatched_at IS NULL" in sql
        assert "'pending'" in sql, "the recovery predicate must admit pending runs"


# ---------------------------------------------------------------------------
# _advance_ongoing_next_fire — scan cadence advance
# ---------------------------------------------------------------------------


class TestAdvanceOngoingNextFire:
    @pytest.mark.asyncio
    async def test_advances_with_scan_interval_floored_at_min(self) -> None:
        now = datetime.now(UTC)
        result = MagicMock()
        result.fetchone.return_value = (1,)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)

        advanced = await ch._advance_ongoing_next_fire(session, TRIGGER_ID, 10)

        assert advanced is True
        stmt = session.execute.await_args.args[0]
        params = session.execute.await_args.args[1]
        assert params["ttype"] == "ongoing"
        assert params["tid"] == str(TRIGGER_ID)
        # scan_interval 10 is floored up to ONGOING_MIN_INTERVAL_SECONDS (60).
        assert params["nf"] >= now + timedelta(seconds=ch.ONGOING_MIN_INTERVAL_SECONDS)
        assert "next_fire_at" in str(stmt)

    @pytest.mark.asyncio
    async def test_advance_uses_scan_interval_when_larger(self) -> None:
        now = datetime.now(UTC)
        result = MagicMock()
        result.fetchone.return_value = (1,)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)

        await ch._advance_ongoing_next_fire(session, TRIGGER_ID, 300)
        params = session.execute.await_args.args[1]
        assert params["nf"] >= now + timedelta(seconds=300)

    @pytest.mark.asyncio
    async def test_second_call_returns_false_when_epoch_already_advanced(self) -> None:
        """The atomic advance is WHERE-gated on next_fire_at <= now(); once
        advanced the row is no longer due, so the next tick's advance returns
        no row (mocked as fetchone() -> None) -> False."""
        session = AsyncMock()

        first_result = MagicMock()
        first_result.fetchone.return_value = (1,)
        second_result = MagicMock()
        second_result.fetchone.return_value = None
        session.execute = AsyncMock(side_effect=[first_result, second_result])

        assert await ch._advance_ongoing_next_fire(session, TRIGGER_ID, 60) is True
        assert await ch._advance_ongoing_next_fire(session, TRIGGER_ID, 60) is False


# ---------------------------------------------------------------------------
# SAQ worker wiring
# ---------------------------------------------------------------------------


class TestSaqWorkerWiring:
    @pytest.mark.asyncio
    async def test_wrapper_delegates_to_cron_helpers(self) -> None:
        from modulo.core import saq_worker as sw

        summary = {"status": "fired", "created": 1, "dispatched": [], "run_ids": [str(uuid.uuid4())]}
        with patch(
            "modulo.core.cron_helpers.fire_ongoing_trigger", new_callable=AsyncMock, return_value=summary
        ) as chf:
            result = await sw.fire_ongoing_trigger(
                _ctx={},
                trigger_id=str(TRIGGER_ID),
                org_id=str(ORG),
                pipeline_id=str(PIPELINE_ID),
                latest_snapshot_id=str(uuid.uuid4()),
            )
        chf.assert_awaited_once()
        assert result == summary

    def test_runs_functions_registers_ongoing(self) -> None:
        from modulo.core import saq_worker as sw

        names = [f[0] for f in sw._runs_functions()]
        assert "modulo.core.saq_worker.fire_ongoing_trigger" in names
