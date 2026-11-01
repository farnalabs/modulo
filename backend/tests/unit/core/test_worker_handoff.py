"""Unit tests for the FAR-1627 worker-shutdown run handoff primitives.

Locks the pieces that do not need a real database:

* the process-local in-flight registry semantics (register / re-register
  replaces / claim-token-fenced discard / claim-token-fenced cancellation
  observation / snapshot / clear),
* the ``track_inflight_run`` cancellation contract — a normal or failed exit
  deregisters, ``asyncio.CancelledError`` (SAQ shutdown) KEEPS the entry AND
  marks it cancellation-observed so the shutdown hook can release it, while an
  entry whose task never observed the cancellation is never handed back,
* the requeue gate (M1): a non-idempotent node-started run is never put on the
  un-gated re-dispatch leg, a never-retry ``retry_policy`` forbids the requeue,
  and a pre-node run keeps the sibling ``_should_redispatch_nodeless`` decision,
* the SAQ settings wiring — ``_base_worker_settings`` installs
  ``_shutdown_handoff_hook`` as the worker ``shutdown`` lifecycle callback,
* the error-code registration — ``harness.worker_lost`` is a registered
  canonical code that ``map_legacy_code`` passes through unchanged (never the
  misleading ``harness.dispatch_failed``), while the legacy raw ``worker_lost``
  alias keeps its historical target.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import modulo.core.pipeline_execution as pipeline_execution
import modulo.core.saq_worker as sw
import modulo.core.worker_handoff as wh
from modulo.core.pipeline_engine.error_codes import (
    ERROR_CODE_REGISTRY,
    LEGACY_ALIASES,
    ErrorCodeSpec,
    map_legacy_code,
)
from modulo.core.worker_handoff import (
    WORKER_LOST_ERROR_CODE,
    InFlightRunRegistry,
    get_inflight_registry,
    handback_inflight_runs,
    track_inflight_run,
)
from modulo.db.crud import row_lock


def test_registry_register_discard_snapshot() -> None:
    reg = InFlightRunRegistry()
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    org = uuid.uuid4()
    reg.register(run_id=run_a, org_id=org, claim_token="tok-a")
    reg.register(run_id=run_b, org_id=org, claim_token="tok-b")
    assert len(reg) == 2
    snap = reg.snapshot()
    assert {e.run_id for e in snap} == {run_a, run_b}
    assert all(e.org_id == org for e in snap)

    reg.discard(run_a, "tok-a")
    assert len(reg) == 1
    assert reg.snapshot()[0].run_id == run_b
    # Discarding with the WRONG token is a silent no-op (M2 fence).
    reg.discard(run_b, "tok-not-b")
    assert len(reg) == 1
    # Discarding an unknown id is a silent no-op (idempotency).
    reg.discard(run_a, "tok-a")
    assert len(reg) == 1

    reg.clear()
    assert len(reg) == 0
    assert not reg.snapshot()


def test_registry_reregister_replaces_claim_token() -> None:
    """A SAQ retry re-claims with a FRESH token — the latest claim wins.

    The handback is token-fenced, so a stale token in the registry would
    fence the run OUT of its own handback; re-registration (which
    ``track_inflight_run`` does on every claim) must replace the entry.
    """
    reg = InFlightRunRegistry()
    run_id = uuid.uuid4()
    org = uuid.uuid4()
    reg.register(run_id=run_id, org_id=org, claim_token="tok-1")
    reg.register(run_id=run_id, org_id=org, claim_token="tok-2")
    assert len(reg) == 1
    assert reg.snapshot()[0].claim_token == "tok-2"
    # A fresh claim starts UNOBSERVED — cancellation observation is per-attempt.
    assert reg.snapshot()[0].cancellation_observed is False


async def test_overlapping_attempt_discard_cannot_delete_newer_entry() -> None:
    """M2: an older attempt's exit must NOT delete the newer attempt's entry.

    SAQ's cancellation branch can re-enqueue a run even when ``cancel_tasks``
    reports the previous task not done, so two attempts for one run can
    overlap in one process. Without the claim-token fence the older attempt's
    exit popped the newer attempt's entry, and a later shutdown had NO entry
    for that run — it waited out the 30-minute heartbeat TTL (the exact
    failure FAR-1627 removes).
    """
    reg = InFlightRunRegistry()
    run_id = uuid.uuid4()
    org = uuid.uuid4()
    entered_1 = asyncio.Event()
    entered_2 = asyncio.Event()
    release_1 = asyncio.Event()
    release_2 = asyncio.Event()

    async def attempt(token: str, entered: asyncio.Event, release: asyncio.Event) -> None:
        async with track_inflight_run(run_id=run_id, org_id=org, claim_token=token, registry=reg):
            entered.set()
            await release.wait()

    task_1 = asyncio.create_task(attempt("tok-1", entered_1, release_1))
    await entered_1.wait()
    # The newer attempt re-claims (registers) the SAME run while attempt 1 is
    # still inside its scope — SAQ's cancellation branch re-enqueues even when
    # `cancel_tasks` reports the previous task not done.
    task_2 = asyncio.create_task(attempt("tok-2", entered_2, release_2))
    await entered_2.wait()
    assert len(reg) == 1
    assert reg.snapshot()[0].claim_token == "tok-2"

    # The OLDER attempt exits first — it must not delete the newer entry.
    release_1.set()
    await task_1
    assert len(reg) == 1
    assert reg.snapshot()[0].claim_token == "tok-2"

    release_2.set()
    await task_2
    assert len(reg) == 0


def test_mark_cancellation_observed_is_claim_token_fenced() -> None:
    """A superseded attempt's cancellation must not mark the successor observed.

    Otherwise a live successor's entry would read as ``cancellation_observed``
    and the shutdown handback would release a run whose task is still running
    (the M3 guarantee, broken from the other side).
    """
    reg = InFlightRunRegistry()
    run_id = uuid.uuid4()
    org = uuid.uuid4()
    reg.register(run_id=run_id, org_id=org, claim_token="tok-old")
    reg.register(run_id=run_id, org_id=org, claim_token="tok-new")

    reg.mark_cancellation_observed(run_id, "tok-old")  # superseded attempt
    assert reg.snapshot()[0].cancellation_observed is False

    reg.mark_cancellation_observed(run_id, "tok-new")
    assert reg.snapshot()[0].cancellation_observed is True

    # Unknown run id: silent no-op.
    reg.mark_cancellation_observed(uuid.uuid4(), "tok-new")
    assert len(reg) == 1


async def test_track_inflight_run_discards_on_normal_exit() -> None:
    reg = InFlightRunRegistry()
    run_id = uuid.uuid4()
    org = uuid.uuid4()
    async with track_inflight_run(run_id=run_id, org_id=org, claim_token="tok", registry=reg):
        assert len(reg) == 1
    assert len(reg) == 0


async def test_track_inflight_run_discards_on_non_cancel_exception() -> None:
    """A failed job's fate is owned by the after_process hook / terminal writer."""
    reg = InFlightRunRegistry()
    run_id = uuid.uuid4()
    org = uuid.uuid4()
    with pytest.raises(RuntimeError, match="boom"):
        async with track_inflight_run(run_id=run_id, org_id=org, claim_token="tok", registry=reg):
            raise RuntimeError("boom")
    assert len(reg) == 0


async def test_track_inflight_run_keeps_entry_on_cancellation() -> None:
    """THE FAR-1627 contract: SAQ shutdown cancellation must leave the entry.

    ``Worker.stop()`` cancels the in-flight job task, so
    ``asyncio.CancelledError`` tears through ``execute_run`` — the run has
    lost its executor and is exactly what the shutdown hook must release.
    The entry is additionally marked CANCELLATION-OBSERVED (M3): observing the
    exception is what proves the executor body unwound, and only observed
    entries are handed back.
    """
    reg = InFlightRunRegistry()
    run_id = uuid.uuid4()
    org = uuid.uuid4()
    with pytest.raises(asyncio.CancelledError):
        async with track_inflight_run(run_id=run_id, org_id=org, claim_token="tok", registry=reg):
            raise asyncio.CancelledError
    assert len(reg) == 1
    entry = reg.snapshot()[0]
    assert entry.run_id == run_id
    assert entry.claim_token == "tok"
    assert entry.cancellation_observed is True


async def test_handback_never_touches_unobserved_entries() -> None:
    """M3: an entry whose task never observed the cancellation is NOT handed back.

    SAQ runs the shutdown callbacks even when cancellation timed out, so a
    stuck task can still be alive at hook time. Its entry stays
    ``cancellation_observed=False``; the handback must not touch the database
    for it at all — the run falls back to the heartbeat-TTL sweeps rather than
    being re-dispatched against a live predecessor.
    """
    reg = InFlightRunRegistry()
    reg.register(run_id=uuid.uuid4(), org_id=uuid.uuid4(), claim_token="tok")
    engine = MagicMock()

    result = await handback_inflight_runs(engine, registry=reg)

    engine.connect.assert_not_called()
    assert result["handed_back"] == 0
    assert result["terminalised"] == 0
    assert result["skipped"] == 1
    # The registry is still drained: the process is shutting down.
    assert len(reg) == 0


# ---------------------------------------------------------------------------
# M1 — the requeue gate (idempotency + retry policy)
# ---------------------------------------------------------------------------


def _row(**overrides: object) -> SimpleNamespace:
    """A row-like object carrying every attribute the gate's readers consume."""
    defaults: dict[str, object] = {
        "status": "running",
        "dispatch_phase": "first_node_dispatched",
        "claim_count": 1,
        "node_attempt_count": 1,
        # FAR-2076: the ONE execution-budget measure the handback gate reads.
        "execution_attempt_count": 1,
        "node_token_usage": None,
        "started_at": datetime.now(UTC) - timedelta(seconds=10),
        "dispatched_at": datetime.now(UTC) - timedelta(seconds=10),
        "sandbox_dispatch_state": None,
        "observed_activity": False,
        "retry_policy": {},
        "outputs_absent": True,
        "checkpoints_absent": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_gate_refuses_non_idempotent_node_started_run() -> None:
    """A node-started run on a non-idempotent graph is NEVER re-queued.

    The reset's only recovery is dispatcher_reconcile's capacity-deferred leg,
    which re-dispatches as ``execute_run`` — a re-EXECUTION with no
    idempotency gate of its own (executor._fenced_pending_reset's contract).
    """
    from modulo.core.worker_handoff import _handback_decision

    decision = _handback_decision(_row(), {"nodes": [{"id": "n1", "idempotent": False}]})
    assert decision.may_requeue is False
    assert "non-idempotent" in decision.reason


def test_gate_requeues_idempotent_node_started_run() -> None:
    """The ticket's sanctioned outcome: an idempotent node-started run re-runs."""
    from modulo.core.worker_handoff import _handback_decision

    decision = _handback_decision(_row(), {"nodes": [{"id": "n1"}]})
    assert decision.may_requeue is True


def test_gate_requeues_zero_node_run_even_on_non_idempotent_graph() -> None:
    """Zero executed nodes => nothing to double-execute (the sibling premise)."""
    from modulo.core.worker_handoff import _handback_decision

    decision = _handback_decision(_row(dispatch_phase="claimed"), {"nodes": [{"id": "n1", "idempotent": False}]})
    assert decision.may_requeue is True


def test_gate_terminalises_node_started_run_with_observed_sandbox_activity() -> None:
    """Observed sandbox evidence must not be discarded by a re-claim."""
    from modulo.core.worker_handoff import _handback_decision

    decision = _handback_decision(
        _row(observed_activity=True),
        {"nodes": [{"id": "n1"}]},
    )

    assert decision.may_requeue is False
    assert "durable sandbox activity" in decision.reason


def test_gate_honours_never_retry_policy_on_node_started_run() -> None:
    """A policy covering neither failure nor stall forbids the requeue (M1)."""
    from modulo.core.worker_handoff import _handback_decision

    policy = {"on": ["eval_failed"], "max_retries": 3}
    decision = _handback_decision(_row(retry_policy=policy), {"nodes": [{"id": "n1"}]})
    assert decision.may_requeue is False
    assert "retry policy" in decision.reason


def test_gate_honours_never_retry_policy_on_pre_node_run() -> None:
    """The pre-node class keeps the sibling ``_should_redispatch_nodeless`` call."""
    from modulo.core.worker_handoff import _handback_decision

    policy = {"on": ["eval_failed"], "max_retries": 3}
    decision = _handback_decision(_row(dispatch_phase="claimed", retry_policy=policy), {})
    assert decision.may_requeue is False


def test_gate_honours_retry_budget_on_pre_node_run() -> None:
    """An exhausted stall-covered budget terminal-fails, as the sibling does."""
    from modulo.core.worker_handoff import _handback_decision

    policy = {"on": ["stall"], "max_retries": 2}
    decision = _handback_decision(
        _row(dispatch_phase="claimed", retry_policy=policy, claim_count=5, execution_attempt_count=3),
        {},
    )
    assert decision.may_requeue is False


def test_gate_treats_checkpointed_row_as_node_started() -> None:
    """A legacy NULL/absent phase with a checkpoint is NOT zero-node (FAR-1088)."""
    from modulo.core.worker_handoff import _handback_decision

    decision = _handback_decision(
        _row(dispatch_phase=None, checkpoints_absent=False),
        {"nodes": [{"id": "n1"}]},
    )
    assert decision.may_requeue is True  # idempotent graph, so requeue is still safe
    # ... but with a non-idempotent graph the same row must be refused:
    refused = _handback_decision(
        _row(dispatch_phase=None, checkpoints_absent=False),
        {"nodes": [{"id": "n1", "idempotent": False}]},
    )
    assert refused.may_requeue is False


def test_process_registry_is_module_wide_singleton() -> None:
    assert get_inflight_registry() is get_inflight_registry()


def test_worker_lost_code_is_registered_and_passes_through() -> None:
    """``harness.worker_lost`` is canonical — never remapped, never unknown.

    The whole truthfulness point of FAR-1627 is that the surfaced code names
    worker loss; if the alias table or the unknown-fallback could rewrite it
    to ``harness.dispatch_failed``, the acceptance criterion fails.
    """
    assert WORKER_LOST_ERROR_CODE in ERROR_CODE_REGISTRY
    assert isinstance(ERROR_CODE_REGISTRY[WORKER_LOST_ERROR_CODE], ErrorCodeSpec)
    assert map_legacy_code(WORKER_LOST_ERROR_CODE) == "harness.worker_lost"
    assert map_legacy_code(WORKER_LOST_ERROR_CODE) != "harness.dispatch_failed"


def test_legacy_raw_worker_lost_alias_is_unchanged() -> None:
    """The historical raw spelling keeps its pre-FAR-1627 alias target.

    The legacy stale-run sweep wrote raw ``worker_lost`` for a different
    class (non-SAQ, 5+ claims); repointing that alias would silently
    reclassify historical rows. The handback writes the DOTTED key instead.
    """
    assert LEGACY_ALIASES["worker_lost"] == "harness.dispatch_failed"


def test_base_worker_settings_installs_shutdown_handoff_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring pin: the SAQ settings dict carries the handoff as ``shutdown``.

    ``Worker(**settings)`` passes it straight through to the lifecycle
    callback SAQ awaits in ``stop()`` after the drain/cancel — so this pin is
    what proves the production worker actually runs the handback on SIGTERM.
    """
    monkeypatch.setattr(
        sw,
        "get_settings",
        lambda: SimpleNamespace(
            saq_redis_pool_size=10,
            saq_worker_concurrency=2,
            redis_url="redis://localhost:6379/0",
        ),
    )
    monkeypatch.setattr(sw, "_build_queue", lambda _name: MagicMock())
    settings = sw._base_worker_settings("runs", [])
    assert settings["shutdown"] is sw._shutdown_handoff_hook
    # The hook is an async function — SAQ awaits lifecycle callbacks.
    assert asyncio.iscoroutinefunction(sw._shutdown_handoff_hook)


async def test_shutdown_handoff_hook_swallows_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail-open with a LOUD log: a handback failure must never fail shutdown.

    An exception escaping the hook would surface as a worker crash instead of
    a logged, backstopped degradation (the heartbeat-TTL sweeps remain the
    safety net).
    """
    import logging

    async def _boom(_engine: object) -> dict[str, int]:
        raise RuntimeError("db down")

    monkeypatch.setattr(sw, "_get_async_engine", lambda: MagicMock())
    monkeypatch.setattr("modulo.core.worker_handoff.handback_inflight_runs", _boom)
    with caplog.at_level(logging.ERROR):
        await sw._shutdown_handoff_hook({})
    assert any("shutdown_handoff_failed" in r.message for r in caplog.records)


async def test_shutdown_handoff_hook_logs_handed_back_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    async def _ok(_engine: object) -> dict[str, int]:
        return {"handed_back": 3, "terminalised": 2, "skipped": 1}

    monkeypatch.setattr(sw, "_get_async_engine", lambda: MagicMock())
    monkeypatch.setattr("modulo.core.worker_handoff.handback_inflight_runs", _ok)
    with caplog.at_level(logging.WARNING):
        await sw._shutdown_handoff_hook({})
    assert any("handed_back=3" in r.message for r in caplog.records)
    assert any("terminalised=2" in r.message for r in caplog.records)


async def test_shutdown_handoff_hook_propagates_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second signal cancelling the shutdown sequence must not be swallowed.

    ``_shutdown_handoff_hook`` fail-opens on any ``Exception`` — but a
    ``CancelledError`` is the shutdown sequence itself being cancelled again
    and must propagate (never be downgraded to a logged degradation).
    """

    async def _cancel(_engine: object) -> dict[str, int]:
        raise asyncio.CancelledError

    monkeypatch.setattr(sw, "_get_async_engine", lambda: MagicMock())
    monkeypatch.setattr("modulo.core.worker_handoff.handback_inflight_runs", _cancel)
    with pytest.raises(asyncio.CancelledError):
        await sw._shutdown_handoff_hook({})


async def test_shutdown_handoff_hook_stays_quiet_when_nothing_handed_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing released and nothing terminalised: no shutdown summary WARNING.

    The summary line is emitted only when a run actually moved
    (``handed_back`` or ``terminalised``) — a clean sweep with only skipped
    entries must not raise the shutdown alarm.
    """
    import logging

    async def _nothing(_engine: object) -> dict[str, int]:
        return {"handed_back": 0, "terminalised": 0, "skipped": 4}

    monkeypatch.setattr(sw, "_get_async_engine", lambda: MagicMock())
    monkeypatch.setattr("modulo.core.worker_handoff.handback_inflight_runs", _nothing)
    with caplog.at_level(logging.WARNING):
        await sw._shutdown_handoff_hook({})
    assert not any("shutdown_handoff handed_back" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# The DB-orchestration body of ``handback_inflight_runs`` (FAR-1627)
# ---------------------------------------------------------------------------
#
# The pure-logic gate above is fully covered; the orchestration that walks the
# observed entries per org and issues the two fenced writes is only exercised
# end-to-end by the integration suite (which does NOT feed the CI coverage
# report — that comes from ``tests/unit`` alone). These unit tests drive every
# arm with a mocked engine, so the changed-lines gate sees the module's
# recovery branches covered: release, reset fence-out, run-not-visible,
# terminal arm (success / fence-out / write error / cancellation), per-org
# row-lock timeout, per-org generic failure, and per-org cancellation.


class _ResetResult:
    """Stand-in for a SQLAlchemy result with only ``fetchone`` consumed."""

    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


def _handback_engine(
    *,
    reset_rows: list[object] | None = None,
    execute_error: BaseException | None = None,
) -> MagicMock:
    """Engine double for :func:`handback_inflight_runs`.

    ``conn.execute`` answers the org ``set_config`` with a benign result and
    each ``_HANDBACK_RESET_SQL`` with the next ``reset_rows`` entry (a non-None
    value means the reset matched a row; None means it was fenced out). Pass
    ``execute_error`` to make EVERY execute raise — the per-org failure arms.
    """
    rows = list(reset_rows or [])
    conn = MagicMock()

    async def _execute(stmt: object, params: object = None) -> _ResetResult:
        if execute_error is not None:
            raise execute_error
        if stmt is wh._HANDBACK_RESET_SQL:
            return _ResetResult(rows.pop(0) if rows else None)
        return _ResetResult(None)

    conn.execute = AsyncMock(side_effect=_execute)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    conn.begin.return_value = begin_cm
    connect_cm = MagicMock()
    connect_cm.__aenter__ = AsyncMock(return_value=conn)
    connect_cm.__aexit__ = AsyncMock(return_value=False)
    engine = MagicMock()
    engine.connect.return_value = connect_cm
    return engine


def _observed_registry(run_id: uuid.UUID, org_id: uuid.UUID, token: str = "tok") -> InFlightRunRegistry:
    """A registry holding one entry whose task observed the shutdown cancel."""
    reg = InFlightRunRegistry()
    reg.register(run_id=run_id, org_id=org_id, claim_token=token)
    reg.mark_cancellation_observed(run_id, token)
    return reg


def _load_returning(row: object, graph: object) -> object:
    """A ``_load_handback_row`` replacement returning ``(row, graph)`` or None."""

    async def _load(conn: object, *, run_id: uuid.UUID, org_id: uuid.UUID) -> object:
        return None if row is None else (row, graph)

    return _load


async def test_handback_empty_registry_returns_zero_counts() -> None:
    engine = _handback_engine()
    result = await handback_inflight_runs(engine, registry=InFlightRunRegistry())
    engine.connect.assert_not_called()
    assert result == {"handed_back": 0, "terminalised": 0, "skipped": 0}


async def test_handback_releases_observed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine(reset_rows=[(str(run_id),)])
    monkeypatch.setattr(wh, "_load_handback_row", _load_returning(_row(), {"nodes": [{"id": "n1"}]}))
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    result = await handback_inflight_runs(engine, registry=reg)
    assert result == {"handed_back": 1, "terminalised": 0, "skipped": 0}
    # The registry is drained once the shutdown handback completes.
    assert len(reg) == 0


async def test_handback_skips_fenced_out_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine(reset_rows=[None])
    monkeypatch.setattr(wh, "_load_handback_row", _load_returning(_row(), {"nodes": [{"id": "n1"}]}))
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    result = await handback_inflight_runs(engine, registry=reg)
    assert result == {"handed_back": 0, "terminalised": 0, "skipped": 1}


async def test_handback_skips_run_not_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine()
    monkeypatch.setattr(wh, "_load_handback_row", _load_returning(None, None))
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    result = await handback_inflight_runs(engine, registry=reg)
    assert result == {"handed_back": 0, "terminalised": 0, "skipped": 1}


async def test_handback_terminalises_observed_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observed E2B work gets a durable terminal failure, not a silent re-claim."""
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine()
    monkeypatch.setattr(
        wh, "_load_handback_row", _load_returning(_row(observed_activity=True), {"nodes": [{"id": "n1"}]})
    )
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    fail = AsyncMock(return_value=True)
    monkeypatch.setattr(pipeline_execution, "fail_run_terminal", fail)
    book_cost = AsyncMock()
    monkeypatch.setattr(pipeline_execution, "record_worker_lost_derived_cost", book_cost)
    result = await handback_inflight_runs(engine, registry=reg)
    assert result == {"handed_back": 0, "terminalised": 1, "skipped": 0}
    fail.assert_awaited_once()
    # FAR-2077: a LANDED terminal write books the run's elapsed sandbox cost.
    book_cost.assert_awaited_once()
    cost_kwargs = book_cost.await_args.kwargs
    assert cost_kwargs["error_code"] == WORKER_LOST_ERROR_CODE
    assert book_cost.await_args.args[0] is engine
    assert book_cost.await_args.args[1] == str(run_id)


async def test_handback_terminal_fenced_out_books_no_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-2077: a fenced-out terminal write is another writer's row — book nothing."""
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine()
    monkeypatch.setattr(
        wh, "_load_handback_row", _load_returning(_row(), {"nodes": [{"id": "n1", "idempotent": False}]})
    )
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    monkeypatch.setattr(pipeline_execution, "fail_run_terminal", AsyncMock(return_value=False))
    book_cost = AsyncMock()
    monkeypatch.setattr(pipeline_execution, "record_worker_lost_derived_cost", book_cost)
    result = await handback_inflight_runs(engine, registry=reg)
    assert result == {"handed_back": 0, "terminalised": 0, "skipped": 1}
    book_cost.assert_not_awaited()


async def test_handback_terminal_write_fenced_out_counts_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine()
    monkeypatch.setattr(
        wh, "_load_handback_row", _load_returning(_row(), {"nodes": [{"id": "n1", "idempotent": False}]})
    )
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    monkeypatch.setattr(pipeline_execution, "fail_run_terminal", AsyncMock(return_value=False))
    result = await handback_inflight_runs(engine, registry=reg)
    assert result == {"handed_back": 0, "terminalised": 0, "skipped": 1}


async def test_handback_terminal_write_error_counts_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine()
    monkeypatch.setattr(
        wh, "_load_handback_row", _load_returning(_row(), {"nodes": [{"id": "n1", "idempotent": False}]})
    )
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    monkeypatch.setattr(pipeline_execution, "fail_run_terminal", AsyncMock(side_effect=RuntimeError("db down")))
    result = await handback_inflight_runs(engine, registry=reg)
    assert result == {"handed_back": 0, "terminalised": 0, "skipped": 1}


async def test_handback_terminal_cancelled_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine()
    monkeypatch.setattr(
        wh, "_load_handback_row", _load_returning(_row(), {"nodes": [{"id": "n1", "idempotent": False}]})
    )
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    monkeypatch.setattr(pipeline_execution, "fail_run_terminal", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await handback_inflight_runs(engine, registry=reg)


async def test_handback_org_row_lock_timeout_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine(execute_error=RuntimeError("55P03 lock_not_available"))
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    monkeypatch.setattr(wh, "is_row_lock_timeout", lambda _exc: True)
    result = await handback_inflight_runs(engine, registry=reg)
    assert result == {"handed_back": 0, "terminalised": 0, "skipped": 1}


async def test_handback_org_generic_failure_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine(execute_error=RuntimeError("boom"))
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    monkeypatch.setattr(wh, "is_row_lock_timeout", lambda _exc: False)
    result = await handback_inflight_runs(engine, registry=reg)
    assert result == {"handed_back": 0, "terminalised": 0, "skipped": 1}


async def test_handback_org_cancelled_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, org_id = uuid.uuid4(), uuid.uuid4()
    reg = _observed_registry(run_id, org_id)
    engine = _handback_engine(execute_error=asyncio.CancelledError)
    monkeypatch.setattr(row_lock, "set_mutation_row_lock_timeout", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await handback_inflight_runs(engine, registry=reg)


# ---------------------------------------------------------------------------
# ``_load_handback_row`` (the requeue-gate read)
# ---------------------------------------------------------------------------


def _row_load_result(mapping: object) -> MagicMock:
    result = MagicMock()
    mappings = MagicMock()
    mappings.one_or_none.return_value = mapping
    result.mappings.return_value = mappings
    return result


async def test_load_handback_row_returns_none_when_absent() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=_row_load_result(None))
    loaded = await wh._load_handback_row(conn, run_id=uuid.uuid4(), org_id=uuid.uuid4())
    assert loaded is None


async def test_load_handback_row_builds_row_and_graph() -> None:
    mapping = {
        "status": "running",
        "dispatch_phase": "first_node_dispatched",
        "claim_count": 2,
        "node_attempt_count": 1,
        "execution_attempt_count": 2,
        "node_token_usage": 5,
        "started_at": None,
        "dispatched_at": None,
        "sandbox_dispatch_state": None,
        "observed_activity": 1,
        "retry_policy": {},
        "graph_json": {"nodes": []},
        "outputs_absent": 0,
        "checkpoints_absent": 1,
    }
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=_row_load_result(mapping))
    row, graph = await wh._load_handback_row(conn, run_id=uuid.uuid4(), org_id=uuid.uuid4())
    assert row.status == "running"
    assert row.outputs_absent is False
    assert row.checkpoints_absent is True
    assert graph == {"nodes": []}


# ---------------------------------------------------------------------------
# ``_node_started_policy_allows`` budget / empty-``on`` arms
# ---------------------------------------------------------------------------


def test_node_started_policy_treats_empty_on_list_as_no_veto() -> None:
    """An explicit empty ``on`` is the GUI's inert no-op save — not a veto."""
    from modulo.core.worker_handoff import _node_started_policy_allows

    ok, reason = _node_started_policy_allows(_row(retry_policy={"on": []}))
    assert ok is True
    assert reason == ""


def test_node_started_policy_allows_failure_covered_within_budget() -> None:
    """A ``failure``-covering policy with budget left permits the re-run."""
    from modulo.core.worker_handoff import _node_started_policy_allows

    ok, reason = _node_started_policy_allows(_row(retry_policy={"on": ["failure"], "max_retries": 3}))
    assert ok is True
    assert reason == ""
