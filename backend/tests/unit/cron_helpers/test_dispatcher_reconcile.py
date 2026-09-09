"""Unit tests for dispatcher_reconcile (plan F3c) — predicate matrix, no-SAQ-
eviction re-dispatch, Redis-error fail-safe, re-enqueue gate-on-return,
discriminator, durable-dispatch recovery (B3) and safe terminalizers (B4/B5).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch

ORG = uuid.uuid4()
RUN_PENDING_UNDISPATCHED = uuid.uuid4()
RUN_PENDING_DISPATCHED = uuid.uuid4()
RUN_RUNNING = uuid.uuid4()
RUN_AWAITING = uuid.uuid4()
RUN_WITH_JOB = uuid.uuid4()
RUN_EVICTED = uuid.uuid4()


def _result_row(row: Any) -> MagicMock:
    """A DB result whose ``.first()`` returns *row* (None = no rows)."""
    result = MagicMock()
    result.first.return_value = row
    return result


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.terminalizer_rows: dict[str, list[uuid.UUID]] = {}
        self.executed: list[tuple[Any, Any]] = []
        self.begin_cm = _MockBegin()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _MockBegin:
        return self.begin_cm

    def get_bind(self) -> Any:
        return self._get_bind()

    async def get(self, model: Any, pk: Any) -> SimpleNamespace:
        return SimpleNamespace(max_concurrent_runs=5, status="running")

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        s = str(stmt)
        if "set_config" in s:
            return MagicMock()
        if "UPDATE runs SET" in s:
            # Dedicated org-scoped terminalizer UPDATEs (B4/B5/FAR-648) — zero
            # rows matched by default; individual tests configure
            # terminalizer_rows.
            ids = self.terminalizer_rows.get("executor_superseded", [])
            if "claim_cap_exhausted" in s:
                ids = self.terminalizer_rows.get("claim_cap_exhausted", [])
            if "hitl_claims" in s:
                # FAR-648 expired-HITL-gate terminalizer — the only
                # UPDATE-runs statement referencing hitl_claims (its error
                # code is a bound param, so it cannot be keyed by code).
                ids = self.terminalizer_rows.get("hitl_gate_expired", [])
            r = MagicMock()
            r.all.return_value = [(uid,) for uid in ids]
            r.rowcount = len(ids)
            return r
        if not self._results:
            return MagicMock()
        return self._results.pop(0)


def _org_result(org_ids: list[uuid.UUID]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = org_ids
    return r


def _rows_result(rows: list[Any]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _run_row(
    run_id: uuid.UUID,
    status: str,
    *,
    dispatched: bool = True,
    dispatched_minutes_ago: float | None = None,
    stale: bool = True,
    nodeless: bool = False,
    error_code: str | None = None,
    dispatcher: str | None = "saq",
    enqueue_failed_at: Any = None,
    claim_count: int = 1,
    retry_policy: Any = None,
) -> SimpleNamespace:
    heartbeat = datetime.now(UTC) - timedelta(minutes=30) if stale else datetime.now(UTC)
    if dispatched_minutes_ago is not None:
        dispatched_at: Any = datetime.now(UTC) - timedelta(minutes=dispatched_minutes_ago)
    else:
        dispatched_at = datetime.now(UTC) if dispatched else None
    return SimpleNamespace(
        id=run_id,
        pipeline_id=uuid.uuid4(),
        status=status,
        dispatched_at=dispatched_at,
        heartbeat_at=heartbeat,
        # Non-None by default (has finalised node output → NOT nodeless); a
        # nodeless zombie has never finalised any node.
        node_token_usage=None if nodeless else {},
        outputs_json=None if nodeless else {},
        started_at=datetime.now(UTC) - timedelta(minutes=60) if nodeless else datetime.now(UTC) - timedelta(minutes=1),
        error_code=error_code,
        dispatcher=dispatcher,
        enqueue_failed_at=enqueue_failed_at,
        claim_count=claim_count,
        retry_policy=retry_policy,
    )


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "saq_reenqueue_window": 600,
        "saq_job_heartbeat": 300,
        "saq_claimed_nodeless_minutes": 35,
        "saq_nodeless_redispatch_budget": 2,
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
        "saq_run_claim_cap": 20,
        "hitl_gate_cancel_grace_seconds": 3600,
        "modulo_telemetry_enabled": False,
        # FAR-746 knobs as REAL ints — the product code uses
        # _int_setting's coded default for anything that is not literally
        # an int (MagicMock attributes would otherwise coerce oddly), and
        # the deadline tests need to override the budget with a real value.
        "dispatcher_reconcile_budget_seconds": 95,
        "dispatcher_reconcile_terminalize_max_per_tick": 25,
        "dispatcher_reconcile_facts_max_per_tick": 25,
    }
    base.update(overrides)
    return MagicMock(**base)


def _make_queue(redis_client: MagicMock, *, job_result: Any = None) -> MagicMock:
    q = MagicMock()
    q.name = "runs"
    q.job_id.side_effect = lambda key: f"saq:job:runs:{key}"
    q.job = AsyncMock(return_value=job_result)
    return q


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


async def _run_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[Any],
    *,
    queue_job_result: Any = None,
    dispatch_result: tuple[str, str | None] = ("enqueued", "new-job-id"),
    capacity_free: bool = True,
    awaiting_committed: bool = True,
    terminalizer_ids: dict[str, list[uuid.UUID]] | None = None,
    terminalizer: AsyncMock | None = None,
    settings_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any, Any, Any, Any, _MockSession]:
    """Drive one ``dispatcher_reconcile`` tick against a fully mocked env.

    ``terminalizer`` optionally patches ``_terminalize_expired_hitl_gates``
    (FAR-648 wiring tests) — the caller keeps its own reference and asserts on
    it directly; ``settings_overrides`` feeds ``_settings`` so a test can
    prove a settings-derived value (e.g. the gate-expiry grace) reaches the
    reconciled code unchanged. Both default to the historical behaviour.
    """
    _patch_env(monkeypatch)
    session = _MockSession([_org_result([ORG]), _rows_result(rows)])
    if terminalizer_ids:
        session.terminalizer_rows = terminalizer_ids
    factory = MagicMock(return_value=session)
    redis_client = AsyncMock()
    q = _make_queue(redis_client, job_result=queue_job_result)
    redis_cls = MagicMock()
    redis_cls.from_url.return_value = redis_client

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(ch, "_open_system_factory", return_value=factory))
        stack.enter_context(patch.object(ch, "get_settings", return_value=_settings(**(settings_overrides or {}))))
        stack.enter_context(patch.object(ch, "AsyncRedis", redis_cls))
        stack.enter_context(patch.object(ch, "RedisQueue", MagicMock(return_value=q)))
        if terminalizer is not None:
            stack.enter_context(patch.object(ch, "_terminalize_expired_hitl_gates", terminalizer))
        reenqueue = stack.enter_context(
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=dispatch_result)
        )
        ingest = stack.enter_context(patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock))
        awaiting_guard = stack.enter_context(
            patch.object(
                ch,
                "_awaiting_human_has_committed_decision",
                new_callable=AsyncMock,
                return_value=awaiting_committed,
            )
        )
        record_facts = stack.enter_context(
            patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock)
        )
        if capacity_free is False:
            stack.enter_context(
                patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=5)
            )
        else:
            stack.enter_context(
                patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0)
            )
        summary = await ch.dispatcher_reconcile()

    session.record_facts = record_facts
    if terminalizer is not None:
        session.terminalizer = terminalizer
    return summary, reenqueue, ingest, redis_client, awaiting_guard, session


def _pipeline_capacity_marker_update(session: _MockSession, run_id: uuid.UUID) -> tuple[Any, Any] | None:
    """Return the recorded FAR-225 marking UPDATE for *run_id*, if any.

    The reconcile marks a pipeline-capacity-skipped orphan with
    ``error_code='pipeline_capacity'`` so the never_dispatched kill sweep
    excludes it and the capacity_marked_stale branch can rescue it. The
    statement carries its params via ``.bindparams()`` (not the execute
    ``params`` dict), so the bound values are read from the clause.
    """
    for stmt, params in session.executed:
        if (
            "UPDATE runs SET error_code" in str(stmt)
            and "IS DISTINCT FROM" in str(stmt)
            and stmt._bindparams["rid"].value == run_id
        ):
            return stmt, params
    return None


class TestReconcilePredicateMatrix:
    @pytest.mark.asyncio
    async def test_pending_undispatched_capacity_free_redispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, ingest, _, _, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_PENDING_UNDISPATCHED, "pending", dispatched=False)]
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pending_undispatched_capacity_full_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, _ingest, _, _, session = await _run_reconcile(
            monkeypatch, [_run_row(RUN_PENDING_UNDISPATCHED, "pending", dispatched=False)], capacity_free=False
        )
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        # FAR-225: the pipeline-capacity skip MARKs the run (error_code
        # 'pipeline_capacity') so it is rescued-not-killed — counted as
        # capacity_deferred, never a plain skipped/never_dispatched kill.
        assert summary["skipped"] == 0
        assert summary["capacity_deferred"] == 1
        assert _pipeline_capacity_marker_update(session, RUN_PENDING_UNDISPATCHED) is not None

    @pytest.mark.asyncio
    async def test_capacity_full_mark_is_idempotent_on_already_marked_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pending+undispatched run ALREADY carrying the pipeline_capacity
        marker is re-marked idempotently (the guard keeps the UPDATE a no-op
        on the marker) — never terminal-failed by the reconcile."""
        summary, reenqueue, _ingest, _, _, session = await _run_reconcile(
            monkeypatch,
            [
                _run_row(
                    RUN_PENDING_UNDISPATCHED,
                    "pending",
                    dispatched=False,
                    error_code="pipeline_capacity",
                )
            ],
            capacity_free=False,
        )
        assert summary["capacity_deferred"] == 1
        assert summary["skipped"] == 0
        reenqueue.assert_not_awaited()
        marker = _pipeline_capacity_marker_update(session, RUN_PENDING_UNDISPATCHED)
        assert marker is not None
        stmt, _params = marker
        # The idempotence guard travels with the statement.
        assert "IS DISTINCT FROM" in str(stmt)
        assert stmt._bindparams["code"].value == "pipeline_capacity"

    @pytest.mark.asyncio
    async def test_pending_dispatched_stale_redispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, _, _, _, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_PENDING_DISPATCHED, "pending", dispatched=True, stale=True, dispatcher=None)],
        )
        assert summary["repaired"] == 1
        assert reenqueue.await_args.args[3] == "execute_run"

    @pytest.mark.asyncio
    async def test_running_stale_redispatch_execute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, _, _, _, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_RUNNING, "running", stale=True)]
        )
        assert summary["repaired"] == 1
        assert reenqueue.await_args.args[3] == "execute_run"

    @pytest.mark.asyncio
    async def test_awaiting_human_committed_decision_stale_redispatched_as_resume_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F6a gated recovery WITH a committed gate decision: an awaiting_human
        run with a stale heartbeat, NO SAQ job in Redis (a half-resumed run
        whose resume_run job was lost), and a committed HITL decision IS
        re-dispatched as resume_run."""
        summary, reenqueue, ingest, _, awaiting_guard, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_AWAITING, "awaiting_human", stale=True)], awaiting_committed=True
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "resume_run"
        ingest.assert_not_awaited()
        awaiting_guard.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_awaiting_human_stale_no_committed_decision_not_redispatched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F6a auto-approve guard: an awaiting_human run with a stale heartbeat
        and NO SAQ job but NO committed gate decision (a genuinely-waiting run
        whose finished job hash expired + heartbeat froze) must NOT be
        re-dispatched — resume_run with empty resume_data would inject
        {"_hitl_decision": {}} and auto-approve the gate."""
        summary, reenqueue, ingest, _, awaiting_guard, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_AWAITING, "awaiting_human", stale=True)], awaiting_committed=False
        )
        assert summary["repaired"] == 0
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()
        awaiting_guard.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claimed_stale_no_job_redispatched_as_resume_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """F6a gated recovery WITH a committed decision: a claimed run with a
        stale heartbeat, NO SAQ job in Redis, and a committed HITL decision IS
        re-dispatched as resume_run (mid-resume crash recovery — decision
        committed + resume job lost; resume_data is reconstructed from the
        decision payload). FAR-541 removed the claimed exemption, so the
        decision guard now applies to claimed rows too."""
        summary, reenqueue, _, _, awaiting_guard, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_AWAITING, "claimed", stale=True)], awaiting_committed=True
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "resume_run"
        awaiting_guard.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reconcile_does_not_autoapprove_claimed_undecided_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-541 regression (observed live on app.modulo.run 2026-09-02
        11:36-11:38 UTC): a human claimed a fired HITL gate at 11:36:21 without
        deciding; the next reconcile tick re-dispatched the run as resume_run
        with an EMPTY decision; executor.resume injected {} as _hitl_decision;
        the gate node treated the empty dict as an approval and the run posted
        a formal GitHub approval. Now: a claimed row with NO committed decision
        (stale heartbeat, no Redis job) is SKIPPED — no resume_run enqueue."""
        summary, reenqueue, ingest, _, awaiting_guard, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_AWAITING, "claimed", stale=True)], awaiting_committed=False
        )
        assert summary["repaired"] == 0
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()
        awaiting_guard.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_capacity_deferred_redispatched_in_saq_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Capacity-deferred runs (pending, dispatched_at NULL, dispatcher NULL)
        must be reachable and re-dispatched when capacity frees (F3c)."""
        summary, reenqueue, ingest, _, _, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_PENDING_UNDISPATCHED, "pending", dispatched=False)],
            capacity_free=True,
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_job_still_exists_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, reenqueue, ingest, _, _, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_WITH_JOB, "running", stale=True)],
            queue_job_result=SimpleNamespace(id="saq:job:runs:run:x"),
        )
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_nodeless_fresh_heartbeat_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A claimed-but-nodeless zombie (running + FRESH heartbeat + zero node
        output after the nodeless window) is now RE-DISPATCHED (not terminal-failed):
        a nodeless zombie executed ZERO nodes, so re-dispatch is safe and recovers
        the run instead of permanently losing it. With no retry_policy, the
        configurable budget (SAQ_NODELESS_REDISPATCH_BUDGET, default 2) applies
        and claim_count == 1 is within it. dispatched_at is NULL (never
        dispatched) so the re-dispatch throttle does not suppress it."""
        summary, reenqueue, ingest, _, _, session = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_RUNNING, "running", stale=False, nodeless=True, dispatched=False)],
        )
        assert summary["nodeless_redispatched"] == 1
        assert summary["nodeless_failed"] == 0
        assert summary["repaired"] == 0
        reenqueue.assert_awaited_once()
        # A running nodeless zombie re-dispatches as execute_run.
        assert reenqueue.await_args.args[3] == "execute_run"
        ingest.assert_not_awaited()
        # A re-dispatched (non-terminal) run is NOT given a compensating fact.
        session.record_facts.assert_not_awaited()

    async def test_running_nodeless_budget_exhausted_terminal_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a retry_policy, re-dispatch is bounded by the configurable
        nodeless budget (SAQ_NODELESS_REDISPATCH_BUDGET, default 2). Once
        claim_count has advanced past the budget (already re-dispatched), the
        run is terminal-failed so it is never left dangling in a re-dispatch loop.
        FAR-714: the terminal-fail now also ingests the
        claimed-but-never-dispatched error event (alert surface)."""
        summary, reenqueue, ingest, _, _, session = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_RUNNING, "running", stale=False, nodeless=True, claim_count=3)],
        )
        assert summary["nodeless_failed"] == 1
        assert summary["nodeless_redispatched"] == 0
        assert summary["claimed_but_never_dispatched"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_awaited_once()
        assert "claimed-but-never-dispatched" in ingest.await_args.kwargs["message"]
        session.record_facts.assert_awaited_once_with(RUN_RUNNING, ORG)

    async def test_running_nodeless_retry_policy_stall_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """retry_policy with 'stall' in 'on' honors max_retries: a run within the
        retry budget (claim_count <= max_retries) is re-dispatched. dispatched_at
        is NULL so the re-dispatch throttle does not suppress it."""
        summary, reenqueue, _, _, _, _ = await _run_reconcile(
            monkeypatch,
            [
                _run_row(
                    RUN_RUNNING,
                    "running",
                    stale=False,
                    nodeless=True,
                    dispatched=False,
                    claim_count=2,
                    retry_policy={"on": ["stall"], "max_retries": 3},
                )
            ],
        )
        assert summary["nodeless_redispatched"] == 1
        assert summary["nodeless_failed"] == 0
        reenqueue.assert_awaited_once()

    async def test_running_nodeless_retry_policy_excludes_stall_terminal_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """retry_policy that does NOT include 'stall' in 'on' must NOT re-dispatch
        a nodeless zombie — it is terminal-failed."""
        summary, reenqueue, _, _, _, session = await _run_reconcile(
            monkeypatch,
            [
                _run_row(
                    RUN_RUNNING,
                    "running",
                    stale=False,
                    nodeless=True,
                    retry_policy={"on": ["timeout"], "max_retries": 3},
                )
            ],
        )
        assert summary["nodeless_failed"] == 1
        assert summary["nodeless_redispatched"] == 0
        reenqueue.assert_not_awaited()
        session.record_facts.assert_awaited_once_with(RUN_RUNNING, ORG)

    async def test_running_nodeless_redispatch_failure_terminal_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the re-dispatch itself raises (Redis unreachable / enqueue error),
        the run falls back to terminal-fail so it is never left dangling."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                _org_result([ORG]),
                _rows_result([_run_row(RUN_RUNNING, "running", stale=False, nodeless=True, dispatched=False)]),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client
        reenqueue = AsyncMock(side_effect=RuntimeError("redis down"))
        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", reenqueue),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
            patch.object(ch, "_awaiting_human_has_committed_decision", new_callable=AsyncMock, return_value=True),
            patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock) as record_facts,
            patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0),
        ):
            summary = await ch.dispatcher_reconcile()
        assert summary["nodeless_failed"] == 1
        assert summary["nodeless_redispatched"] == 0
        reenqueue.assert_awaited_once()
        ingest.assert_awaited()  # fallback alerts on the enqueue failure
        record_facts.assert_awaited_once_with(RUN_RUNNING, ORG)

    @pytest.mark.asyncio
    async def test_running_with_node_output_not_nodeless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run that finalised node output is NOT nodeless — a stale-heartbeat
        one still takes the worker-lost re-dispatch repair, never the fail."""
        summary, reenqueue, _, _, _, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_RUNNING, "running", stale=True, nodeless=False)],
        )
        assert summary["repaired"] == 1
        assert summary["nodeless_failed"] == 0
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"

    def test_nodeless_age_gate_requires_staleness(self) -> None:
        """Age-gate unit check: a nodeless-but-recently-started run is NOT
        matched (the predicate age gate protects a legitimate long first node)."""
        row = _run_row(RUN_RUNNING, "running", stale=False, nodeless=True)
        row.started_at = datetime.now(UTC) - timedelta(minutes=10)
        assert ch._is_nodeless_zombie_row(row, 45) is False

    @pytest.mark.asyncio
    async def test_nodeless_with_recent_start_falls_through_to_job_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A nodeless run that started recently (age gate not elapsed) is not
        failed by the nodeless branch; with a fresh heartbeat and no other
        branch matching, it is skipped (job exists) rather than failed."""
        row = _run_row(RUN_RUNNING, "running", stale=False, nodeless=True)
        row.started_at = datetime.now(UTC) - timedelta(minutes=10)
        summary, reenqueue, _, _, _, _ = await _run_reconcile(
            monkeypatch,
            [row],
            queue_job_result=SimpleNamespace(id="saq:job:runs:run:x"),
        )
        assert summary["nodeless_failed"] == 0
        assert summary["skipped"] == 1
        reenqueue.assert_not_awaited()


class TestHitlResumeOrSkipPredicateMatrix:
    """Direct predicate matrix for ``_resolve_hitl_resume_or_skip`` (FAR-541).

    A claimed row behaves exactly like an awaiting_human row — it resumes only
    from a committed decision the guard accepts. As of iteration 4 (FIX C) a
    claimed-UNDECIDED row always skips: under ``uq_hitl_claims_run_gate`` the
    old "claimed + same-gate committed decision" match was structurally dead
    (a claimed-undecided row and a DECIDED row for the same gate cannot
    coexist), and claimed-run crash recovery routes through the
    no-undecided-rows guard branch once the decision commits.
    """

    def _row(self, status: str) -> SimpleNamespace:
        return SimpleNamespace(id=RUN_AWAITING, status=status)

    async def _resolve(
        self,
        monkeypatch: pytest.MonkeyPatch,
        status: str,
        *,
        committed: bool,
        resume_data: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any] | None, dict[str, Any], AsyncMock, AsyncMock]:
        row = self._row(status)
        summary: dict[str, Any] = {"skipped": 0}
        guard = AsyncMock(return_value=committed)
        resume = AsyncMock(return_value=resume_data)
        monkeypatch.setattr(ch, "_awaiting_human_has_committed_decision", guard)
        monkeypatch.setattr(ch, "_committed_decision_resume_data", resume)
        skip, data = await ch._resolve_hitl_resume_or_skip(MagicMock(), ORG, row, summary)
        return skip, data, summary, guard, resume

    @pytest.mark.asyncio
    async def test_claimed_with_committed_decision_resumes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plumbing preserved: a claimed run whose guard passes (no
        claimed-undecided row — e.g. the claim expired/reset — plus a
        committed decision) resumes with resume_data reconstructed from the
        committed decision payload. The claimed-undecided + decided-same-gate
        resume was structurally dead (FIX C) and lives no more; claimed-run
        crash recovery routes through the no-undecided-rows guard branch."""
        payload = {"action": "rejected", "reason": "needs work"}
        skip, data, summary, guard, resume = await self._resolve(
            monkeypatch, "claimed", committed=True, resume_data=payload
        )
        assert skip is False
        assert data == payload
        assert summary["skipped"] == 0
        guard.assert_awaited_once()
        resume.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claimed_without_committed_decision_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """THE FIX (FAR-541): claimed + NO committed decision -> (True, None);
        the run is never re-dispatched with an empty decision."""
        skip, data, summary, guard, resume = await self._resolve(monkeypatch, "claimed", committed=False)
        assert skip is True
        assert data is None
        assert summary["skipped"] == 1
        guard.assert_awaited_once()
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_awaiting_human_with_committed_decision_resumes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unchanged: awaiting_human + committed decision -> resume."""
        payload = {"action": "approved", "notes": "looks good"}
        skip, data, summary, guard, resume = await self._resolve(
            monkeypatch, "awaiting_human", committed=True, resume_data=payload
        )
        assert skip is False
        assert data == payload
        assert summary["skipped"] == 0
        guard.assert_awaited_once()
        resume.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_awaiting_human_without_committed_decision_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unchanged: awaiting_human + no committed decision -> (True, None)."""
        skip, data, summary, guard, resume = await self._resolve(monkeypatch, "awaiting_human", committed=False)
        assert skip is True
        assert data is None
        assert summary["skipped"] == 1
        guard.assert_awaited_once()
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_parked_with_committed_decision_resumes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """qa F1 (park-vs-decide race): a ``hitl_parked`` run whose gate
        decision committed AFTER the park swept it is re-dispatched as
        ``resume_run`` through the SAME committed-decision guard — the run
        self-heals via the existing reconcile→resume path instead of being
        stranded parked forever."""
        payload = {"action": "approved", "gate_id": "hitl_gate_a_b"}
        skip, data, summary, guard, resume = await self._resolve(
            monkeypatch, "hitl_parked", committed=True, resume_data=payload
        )
        assert skip is False
        assert data == payload
        assert summary["skipped"] == 0
        guard.assert_awaited_once()
        resume.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_parked_without_committed_decision_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """qa F1: a genuinely-parked run (gate still undecided — the normal
        park state) is NEVER re-dispatched: the same auto-approve guard that
        protects awaiting_human/claimed protects hitl_parked."""
        skip, data, summary, guard, resume = await self._resolve(monkeypatch, "hitl_parked", committed=False)
        assert skip is True
        assert data is None
        assert summary["skipped"] == 1
        guard.assert_awaited_once()
        resume.assert_not_awaited()

    @pytest.mark.parametrize(
        ("status", "job_type"),
        [
            ("awaiting_human", "resume_run"),
            ("claimed", "resume_run"),
            ("hitl_parked", "resume_run"),
            ("pending", "execute_run"),
            ("running", "execute_run"),
        ],
    )
    def test_reconcile_job_type_covers_parked(self, status: str, job_type: str) -> None:
        """qa F1: the discriminator maps parked runs to ``resume_run`` so the
        F6a recovery enqueues the resume (checkpoint-continuing) variant."""
        assert ch._reconcile_job_type(status) == job_type

    @pytest.mark.asyncio
    async def test_other_status_passes_through_unguarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-HITL status (pending/running) is not touched by the guard."""
        summary: dict[str, Any] = {"skipped": 0}
        guard = AsyncMock()
        monkeypatch.setattr(ch, "_awaiting_human_has_committed_decision", guard)
        skip, data = await ch._resolve_hitl_resume_or_skip(MagicMock(), ORG, self._row("running"), summary)
        assert skip is False
        assert data is None
        assert summary["skipped"] == 0

    @pytest.mark.asyncio
    async def test_cross_gate_reconcile_skips_the_c1_incident(self) -> None:
        """THE C1 REGRESSION THROUGH THE RECONCILE (FAR-541 iteration 2): gate
        A decided/approved -> run proceeds -> gate B fires and is claimed ->
        run awaiting_human -> the reconcile must SKIP (it must not replay A's
        decision onto B). Real guard functions, DB-shaped mock session."""
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _result_row(("approved", {"action": "approved", "gate_id": "hitl_gate_a_b"}, "hitl_gate_a_b")),
                _result_row(("hitl_gate_c_d",)),  # gate B: claimed, undecided
            ]
        )
        summary: dict[str, Any] = {"skipped": 0}
        row = SimpleNamespace(id=RUN_AWAITING, status="claimed")
        skip, data = await ch._resolve_hitl_resume_or_skip(session, ORG, row, summary)
        assert skip is True
        assert data is None
        assert summary["skipped"] == 1

    @pytest.mark.asyncio
    async def test_cross_gate_matched_decision_resume_was_structurally_dead(self) -> None:
        """FAR-541 iteration 4 (FIX C): the old "claimed + same-gate committed
        decision -> resume" branch was STRUCTURALLY DEAD — under
        ``uq_hitl_claims_run_gate`` (UNIQUE (run_id, gate_id)) a
        claimed-UNDECIDED row and a DECIDED row for the same gate cannot
        coexist, so that test mocked a constraint-violating impossible state.
        The REAL claimed state a committed decision can coexist with is the
        C1 shape: a claimed-UNDECIDED row for gate B (the run's pending gate)
        + a committed decision for the EARLIER gate A -> SKIP (never replay A
        onto B). Guard-level: the skip comes straight from the claimed
        branch, before any identity comparison."""
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _result_row(("approved", {"action": "approved", "gate_id": "hitl_gate_a_b"}, "hitl_gate_a_b")),
                _result_row(("hitl_gate_c_d",)),  # gate B: claimed, undecided
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False


class TestReDispatchPredicateMatchesParked:
    """qa F1: the F6a gated-recovery branch of the re-dispatch predicate
    matches ``hitl_parked`` rows — a parked run whose gate decision committed
    after the sweep parked it is recoverable on the reconcile tick."""

    @staticmethod
    def _predicate_param_values() -> set[str]:
        predicate = ch._build_re_dispatch_predicate(
            reenqueue_window=120,
            stale_window=600,
            capacity_redispatch_seconds=120,
        )
        flat: set[str] = set()
        for value in predicate.compile().params.values():
            if isinstance(value, (list, tuple, set, frozenset)):
                flat.update(str(item) for item in value)
            else:
                flat.add(str(value))
        return flat

    def test_parked_status_in_the_gated_recovery_branch(self) -> None:
        values = self._predicate_param_values()
        assert "hitl_parked" in values
        assert "awaiting_human" in values
        assert "claimed" in values


class TestNodelessRedispatchBudget:
    """FAR-509: the policy-less nodeless re-dispatch budget is configurable via
    SAQ_NODELESS_REDISPATCH_BUDGET (default 2). Direct unit checks of
    ``_should_redispatch_nodeless`` — the retry-policy taxonomy. FAR-525 qa
    gate: the decision keys on the POLICY's EVENT CONTENT (its ``on`` list),
    NEVER on dict non-emptiness — the FAR-525 GUI's no-op panel save
    (``{on: [], max_retries: 0, backoff_schedule: {...}}``, always non-empty)
    cannot silently convert budget-default repair into terminal-fail.
    FAR-649: an ABSENT ``on`` (key missing or null) with a valid budget > 0 is
    now ALL-events coverage (stall included) — the POLICY budget applies, not
    the budget-default. The 6-row characterization matrix:

      1. ``{}``                                        -> budget-default
      2. ``{on: [], max_retries: N}``                  -> budget-default
      3. ``{max_retries: N}`` (absent on)              -> stall-covered / policy budget (FAR-649)
      4. non-empty ``on`` without "stall"              -> terminal-fail (False)
      5. ``{on: ["stall"], max_retries: N}``           -> stall-covered / policy budget
      6. ``{on: null, max_retries: N}``                -> stall-covered / policy budget (FAR-649)
    """

    @staticmethod
    def _row(claim_count: int, retry_policy: Any = None) -> SimpleNamespace:
        return _run_row(
            RUN_RUNNING, "running", stale=False, nodeless=True, claim_count=claim_count, retry_policy=retry_policy
        )

    def test_policy_less_first_claim_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """claim_count=1 (the initial claim, never re-dispatched) is within the
        default budget — re-dispatch."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())
        assert ch._should_redispatch_nodeless(self._row(1)) is True

    def test_policy_less_second_claim_within_default_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default budget 2: claim_count=2 is still re-dispatched (one
        re-dispatch after the original claim)."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())
        assert ch._should_redispatch_nodeless(self._row(2)) is True

    def test_policy_less_budget_exhausted_terminal_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default budget 2: claim_count=3 has exhausted the budget — the
        backstop terminal-fail applies."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())
        assert ch._should_redispatch_nodeless(self._row(3)) is False

    def test_policy_less_custom_budget_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With SAQ_NODELESS_REDISPATCH_BUDGET=1, claim_count=2 is already past
        the budget — terminal-fail (the pre-FAR-509 hardcoded behaviour)."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings(saq_nodeless_redispatch_budget=1))
        assert ch._should_redispatch_nodeless(self._row(2)) is False

    def test_stall_retry_policy_honors_max_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A policy covering 'stall' honors its OWN max_retries budget, not the
        global nodeless budget (claim_count <= max_retries)."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())
        assert ch._should_redispatch_nodeless(self._row(2, retry_policy={"on": ["stall"], "max_retries": 2})) is True
        assert ch._should_redispatch_nodeless(self._row(3, retry_policy={"on": ["stall"], "max_retries": 2})) is False

    def test_non_stall_policy_never_redispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A policy whose `on` NAMES events but WITHOUT 'stall' is terminal-failed
        regardless of claim_count or the configured budget."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings(saq_nodeless_redispatch_budget=10))
        assert (
            ch._should_redispatch_nodeless(self._row(1, retry_policy={"on": ["ci_failure"], "max_retries": 5})) is False
        )

    def test_empty_policy_treated_as_policy_less(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty dict policy (the column default) is treated as policy-less:
        the configurable budget applies."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())
        assert ch._should_redispatch_nodeless(self._row(2, retry_policy={})) is True
        assert ch._should_redispatch_nodeless(self._row(3, retry_policy={})) is False

    def test_noop_panel_save_policy_gets_budget_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-525 qa gate — the no-op panel save always stores a NON-EMPTY
        policy with an EXPLICIT empty ``on`` (``{on: [], max_retries: 0,
        backoff_schedule: {...}}``). An explicit empty ``on`` stays
        budget-default repair under FAR-649 too (row 2)."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())
        noop_save = {"on": [], "max_retries": 0, "backoff_schedule": {"delay_seconds": 45, "multiplier": 2.0}}
        assert ch._should_redispatch_nodeless(self._row(2, retry_policy=noop_save)) is True
        assert ch._should_redispatch_nodeless(self._row(3, retry_policy=noop_save)) is False

    def test_absent_on_policy_with_budget_honors_policy_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-649 — matrix row 3 (supersedes the FAR-525-era budget-default
        pin): an ABSENT `on` key with a valid budget > 0 is now ALL-events
        coverage (stall included), so the POLICY budget applies — terminal-fail
        once claim_count exceeds max_retries, NOT the budget-default."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings(saq_nodeless_redispatch_budget=10))
        assert ch._should_redispatch_nodeless(self._row(3, retry_policy={"max_retries": 3})) is True
        assert ch._should_redispatch_nodeless(self._row(4, retry_policy={"max_retries": 3})) is False

    def test_absent_on_policy_with_zero_budget_falls_to_budget_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An absent-`on` policy with a 0 (or malformed) budget is NOT
        re-classified as stall-covered — it falls through to the
        budget-default repair (the unusable-data treatment)."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())
        assert ch._should_redispatch_nodeless(self._row(2, retry_policy={"max_retries": 0})) is True
        assert ch._should_redispatch_nodeless(self._row(2, retry_policy={"max_retries": "lots"})) is True

    def test_null_on_policy_with_budget_honors_policy_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-649 — matrix row 6: an explicitly-NULL `on` with a valid budget
        > 0 behaves like an absent key — all-events coverage, POLICY budget."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings(saq_nodeless_redispatch_budget=10))
        assert ch._should_redispatch_nodeless(self._row(3, retry_policy={"on": None, "max_retries": 3})) is True
        assert ch._should_redispatch_nodeless(self._row(4, retry_policy={"on": None, "max_retries": 3})) is False


class TestNodelessRedispatchThrottle:
    """FAR-509 (qa-iterate): the nodeless re-dispatch is THROTTLED to at most
    one enqueue per ``SAQ_CLAIMED_NODELESS_MINUTES`` window per run — without
    the throttle, a budget-eligible zombie was re-enqueued on every 60s tick
    (the fresh key_suffix defeats SAQ dedupe). Three-way outcome at the repair
    branch: budget exhaustion terminal-fails (even when throttled — waiting
    cannot help a run that can no longer be re-dispatched); a run dispatched
    within the window is skipped silently (no enqueue, no terminal-fail); a run
    whose window elapsed is re-dispatched. Between the budget and the throttle,
    a never-re-claimable zombie is bounded by the mid-graph-wedge age backstop."""

    @staticmethod
    def _row(dispatched_minutes_ago: float | None, claim_count: int = 1) -> SimpleNamespace:
        return _run_row(
            RUN_RUNNING,
            "running",
            stale=False,
            nodeless=True,
            dispatched=False,
            dispatched_minutes_ago=dispatched_minutes_ago,
            claim_count=claim_count,
        )

    @pytest.mark.asyncio
    async def test_dispatched_at_null_budget_available_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A never-dispatched nodeless zombie (dispatched_at NULL) is never
        throttled: budget available → re-dispatched (enqueue called, no fail)."""
        summary, reenqueue, ingest, _, _, session = await _run_reconcile(
            monkeypatch, [self._row(dispatched_minutes_ago=None)]
        )
        assert summary["nodeless_redispatched"] == 1
        assert summary["nodeless_failed"] == 0
        assert summary["repaired"] == 0
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        ingest.assert_not_awaited()
        session.record_facts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recent_dispatch_throttled_no_enqueue_no_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run re-dispatched 10 minutes ago (within the 35-min nodeless
        window) is THROTTLED: no duplicate enqueue, no terminal-fail — the run
        is left untouched for a later tick (the throttle bounds the enqueue
        rate; the budget bounds the claim cycles)."""
        summary, reenqueue, ingest, _, _, session = await _run_reconcile(
            monkeypatch, [self._row(dispatched_minutes_ago=10)]
        )
        assert summary["nodeless_redispatched"] == 0
        assert summary["nodeless_failed"] == 0
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()
        # Throttle-skip is silent: no compensating fact, run untouched.
        session.record_facts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_window_elapsed_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run last dispatched 40 minutes ago (nodeless window elapsed) is
        re-dispatched again — at most one re-dispatch per window."""
        summary, reenqueue, _, _, _, _ = await _run_reconcile(monkeypatch, [self._row(dispatched_minutes_ago=40)])
        assert summary["nodeless_redispatched"] == 1
        assert summary["nodeless_failed"] == 0
        reenqueue.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_budget_exhausted_terminal_fails_even_when_throttled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ordering pinned: the budget check wins over the throttle. A run past
        its claim budget is terminal-failed even with a fresh dispatched_at —
        waiting cannot help a run that can no longer be re-dispatched."""
        summary, reenqueue, _, _, _, session = await _run_reconcile(
            monkeypatch, [self._row(dispatched_minutes_ago=10, claim_count=3)]
        )
        assert summary["nodeless_failed"] == 1
        assert summary["nodeless_redispatched"] == 0
        reenqueue.assert_not_awaited()
        session.record_facts.assert_awaited_once_with(RUN_RUNNING, ORG)

    def test_throttle_helper_boundaries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Direct boundary checks of ``_is_nodeless_redispatch_throttled``:
        NULL dispatched_at never throttles; a dispatch inside the window
        throttles; past the window it does not (5-min margins around the 35-min
        window — no exact-boundary flake)."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())
        assert ch._is_nodeless_redispatch_throttled(self._row(None), 35) is False
        assert ch._is_nodeless_redispatch_throttled(self._row(10), 35) is True
        assert ch._is_nodeless_redispatch_throttled(self._row(34), 35) is True
        assert ch._is_nodeless_redispatch_throttled(self._row(36), 35) is False


class TestNodelessRedispatchPerTickCap:
    """FAR-509 (qa-iterate): the nodeless re-dispatch is fleet-capped at
    ``NODELESS_REDISPATCH_MAX_PER_TICK`` enqueues per tick — after a fleet-wide
    worker wedge every aged nodeless zombie becomes throttle-eligible in the
    SAME tick, and the fresh key_suffix defeats SAQ dedupe, so without the cap
    the first recovery tick floods the queue. Mirrors the B3 enqueue-failed
    cap: capped rows are deferred (no enqueue, no terminal-fail — they stay
    running and become eligible again next tick; budget/throttle unaffected),
    counted ``nodeless_capped``, warning logged once per tick. The cap gates
    ONLY the re-dispatch outcome: budget-exhausted rows still terminal-fail
    when the cap is hit."""

    @staticmethod
    def _rows(count: int) -> list[SimpleNamespace]:
        """*count* distinct throttle-eligible, budget-available nodeless
        zombies (dispatched 40 min ago — the 35-min window elapsed)."""
        return [
            _run_row(
                uuid.uuid4(),
                "running",
                stale=False,
                nodeless=True,
                dispatched=False,
                dispatched_minutes_ago=40,
                claim_count=1,
            )
            for _ in range(count)
        ]

    @pytest.mark.asyncio
    async def test_burst_above_cap_enqueues_cap_and_defers_rest(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """4 eligible zombies with the cap patched to 2: exactly 2 enqueues,
        the other 2 untouched (no enqueue, no terminal-fail), nodeless_capped
        == 2, and the warning logs ONCE per tick (not per capped row)."""
        monkeypatch.setattr(ch, "NODELESS_REDISPATCH_MAX_PER_TICK", 2)
        rows = self._rows(4)
        with caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"):
            summary, reenqueue, ingest, _, _, session = await _run_reconcile(monkeypatch, rows)
        assert summary["nodeless_redispatched"] == 2
        assert summary["nodeless_capped"] == 2
        assert summary["nodeless_failed"] == 0
        assert reenqueue.await_count == 2
        ingest.assert_not_awaited()
        session.record_facts.assert_not_awaited()
        assert sum("nodeless re-dispatch cap hit" in r.message for r in caplog.records) == 1
        # Stats plumbing: the capped count reaches set_dispatcher_reconcile_stats
        # (the /healthz/ready dict) for this tick.
        assert ch._dispatcher_reconcile_stats["nodeless_capped"] == 2

    @pytest.mark.asyncio
    async def test_budget_exhausted_still_terminal_fails_when_cap_hit(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Ordering pinned: the cap gates only the re-dispatch outcome. The
        budget-exhausted zombie is placed AFTER the cap is already hit — it
        still terminal-fails (the budget check precedes the cap; terminal-fail
        reduces load and never enqueues)."""
        monkeypatch.setattr(ch, "NODELESS_REDISPATCH_MAX_PER_TICK", 2)
        exhausted = _run_row(
            uuid.uuid4(),
            "running",
            stale=False,
            nodeless=True,
            dispatched=False,
            dispatched_minutes_ago=40,
            claim_count=3,
        )
        rows = [*self._rows(3), exhausted]
        with caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"):
            summary, reenqueue, _, _, _, session = await _run_reconcile(monkeypatch, rows)
        assert summary["nodeless_redispatched"] == 2
        assert summary["nodeless_capped"] == 1
        assert summary["nodeless_failed"] == 1
        assert reenqueue.await_count == 2
        session.record_facts.assert_awaited_once_with(exhausted.id, ORG)

    @pytest.mark.asyncio
    async def test_burst_at_cap_enqueues_all_without_capping(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Exactly *cap* eligible zombies (boundary): all enqueued,
        nodeless_capped == 0, no cap warning."""
        monkeypatch.setattr(ch, "NODELESS_REDISPATCH_MAX_PER_TICK", 2)
        rows = self._rows(2)
        with caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"):
            summary, reenqueue, _, _, _, _ = await _run_reconcile(monkeypatch, rows)
        assert summary["nodeless_redispatched"] == 2
        assert summary["nodeless_capped"] == 0
        assert summary["nodeless_failed"] == 0
        assert reenqueue.await_count == 2
        assert not any("nodeless re-dispatch cap hit" in r.message for r in caplog.records)


class TestClaimedButNeverDispatchedCounter:
    """FAR-714: the claimed-but-never-dispatched detection surface. Every run
    the zombie repair TERMINAL-FAILS with the "Claimed by SAQ but dispatched no
    node" detail (the ~30/week executor_stalled class) must be counted in the
    tick summary's ``claimed_but_never_dispatched``, logged at ERROR level with
    the wasted age, and ingested as a ``source='saq'`` error event — while the
    non-terminal repair legs (re-dispatch, throttle, cap) must NOT bump it."""

    @pytest.mark.asyncio
    async def test_budget_exhausted_terminal_fail_bumps_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Prove-the-fix: the counter is bumped from the ACTUAL repair decision
        — a budget-exhausted nodeless zombie terminal-failed by
        ``_fail_nodeless_run`` — not from a fixture shortcut. The stable-message
        error event fires with the run id in context."""
        exhausted = _run_row(
            uuid.uuid4(),
            "running",
            stale=False,
            nodeless=True,
            dispatched=False,
            dispatched_minutes_ago=40,
            claim_count=3,
        )
        summary, reenqueue, ingest, _, _, session = await _run_reconcile(monkeypatch, [exhausted])
        assert summary["nodeless_failed"] == 1
        assert summary["claimed_but_never_dispatched"] == 1
        assert summary["nodeless_redispatched"] == 0
        reenqueue.assert_not_awaited()
        session.record_facts.assert_awaited_once_with(exhausted.id, ORG)
        ingest.assert_awaited_once()
        assert "claimed-but-never-dispatched" in ingest.await_args.kwargs["message"]
        assert ingest.await_args.kwargs["context"]["run_id"] == str(exhausted.id)

    @pytest.mark.asyncio
    async def test_redispatch_leg_does_not_bump_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A first-sighting zombie within its budget is RE-DISPATCHED (not
        terminal-failed): no counter bump, no error event."""
        fresh = _run_row(
            uuid.uuid4(),
            "running",
            stale=False,
            nodeless=True,
            dispatched=False,
            dispatched_minutes_ago=None,
            claim_count=1,
        )
        summary, reenqueue, ingest, _, _, _ = await _run_reconcile(monkeypatch, [fresh])
        assert summary["nodeless_redispatched"] == 1
        assert summary["claimed_but_never_dispatched"] == 0
        reenqueue.assert_awaited_once()
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_throttled_and_capped_legs_do_not_bump_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Throttle-skip and per-tick-cap defer rows WITHOUT terminal-failing:
        neither leg may bump the terminal-fail counter. Row order drives the
        outcomes: the first zombie exhausts the cap-1 budget, the second is
        capped, the third is throttled."""
        monkeypatch.setattr(ch, "NODELESS_REDISPATCH_MAX_PER_TICK", 1)
        redispatched = _run_row(
            uuid.uuid4(),
            "running",
            stale=False,
            nodeless=True,
            dispatched=False,
            dispatched_minutes_ago=40,
            claim_count=1,
        )
        capped = _run_row(
            uuid.uuid4(),
            "running",
            stale=False,
            nodeless=True,
            dispatched=False,
            dispatched_minutes_ago=40,
            claim_count=1,
        )
        throttled = _run_row(
            uuid.uuid4(),
            "running",
            stale=False,
            nodeless=True,
            dispatched=False,
            dispatched_minutes_ago=10,
            claim_count=1,
        )
        summary, reenqueue, ingest, _, _, _ = await _run_reconcile(monkeypatch, [redispatched, capped, throttled])
        assert summary["nodeless_redispatched"] == 1
        assert summary["nodeless_capped"] == 1
        assert summary["nodeless_failed"] == 0
        assert summary["claimed_but_never_dispatched"] == 0
        assert reenqueue.await_count == 1
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_terminal_fail_logs_alert_grade_at_error_level(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The repair logs at ERROR level (alert-grade, not the old WARNING)
        with the run id, claim_count and the wasted zombie age; the tick summary
        adds its own ERROR-level alert line."""
        exhausted = _run_row(
            uuid.uuid4(),
            "running",
            stale=False,
            nodeless=True,
            dispatched=False,
            dispatched_minutes_ago=40,
            claim_count=3,
        )
        with caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"):
            summary, _, _, _, _, _ = await _run_reconcile(monkeypatch, [exhausted])
        assert summary["claimed_but_never_dispatched"] == 1
        per_run = [r for r in caplog.records if "claimed_but_never_dispatched run=" in r.message]
        assert len(per_run) == 1
        assert str(exhausted.id) in per_run[0].getMessage()
        assert "zombie_age_minutes=" in per_run[0].getMessage()
        tick_summary = [r for r in caplog.records if "claimed_but_never_dispatched_summary" in r.message]
        assert len(tick_summary) == 1
        assert "1 run(s)" in tick_summary[0].getMessage()

    @pytest.mark.asyncio
    async def test_no_zombies_no_summary_alert(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A quiet tick emits no claimed-but-never-dispatched summary alert."""
        with caplog.at_level(logging.ERROR, logger="modulo.core.cron_helpers"):
            summary, _, _, _, _, _ = await _run_reconcile(monkeypatch, [_run_row(RUN_RUNNING, "running", stale=True)])
        assert summary["claimed_but_never_dispatched"] == 0
        assert not any("claimed_but_never_dispatched" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_counter_persisted_to_shared_stats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The new counter reaches the shared Redis stats payload the WEB
        process's /healthz/ready reads."""
        exhausted = _run_row(
            uuid.uuid4(),
            "running",
            stale=False,
            nodeless=True,
            dispatched=False,
            dispatched_minutes_ago=40,
            claim_count=3,
        )
        summary, _, _, redis_client, _, _ = await _run_reconcile(monkeypatch, [exhausted])
        stats_sets = [c for c in redis_client.set.await_args_list if c.args[0] == ch.DISPATCHER_RECONCILE_STATS_KEY]
        assert stats_sets, "dispatcher_reconcile must persist its outcome to the shared Redis stats key"
        payload = json.loads(stats_sets[0].args[1])
        assert payload["claimed_but_never_dispatched"] == 1
        assert summary["claimed_but_never_dispatched"] == 1
        # Module-dict plumbing (the /healthz in-worker view) reflects the tick.
        assert ch._dispatcher_reconcile_stats["claimed_but_never_dispatched"] == 1

    def test_stats_setter_copies_counter(self) -> None:
        """set_dispatcher_reconcile_stats carries the new key into the module
        dict (a missing copy line would silently zero /healthz/ready)."""
        ch.set_dispatcher_reconcile_stats({"claimed_but_never_dispatched": 4})
        assert ch._dispatcher_reconcile_stats["claimed_but_never_dispatched"] == 4
        ch.set_dispatcher_reconcile_stats({"claimed_but_never_dispatched": 0})
        assert ch._dispatcher_reconcile_stats["claimed_but_never_dispatched"] == 0

    @pytest.mark.asyncio
    async def test_fail_nodeless_run_bumps_only_when_transition_lands(self) -> None:
        """Direct unit check of the chokepoint: the counter bumps ONLY when the
        run row is still ``running`` (the actual repair); a missing row is a
        no-op for both the run and the counter."""
        summary = ch._dispatcher_summary()
        ingest = AsyncMock()
        with (
            patch.object(ch, "_ingest_saq_error", ingest),
            patch.object(ch, "get_settings", return_value=_settings()),
        ):
            await ch._fail_nodeless_run(_MockSession([_org_result([])]), uuid.uuid4(), ORG, summary)
        assert summary["claimed_but_never_dispatched"] == 1
        ingest.assert_awaited_once()
        # A missing/not-running row: the residual _MockSession.get returns None
        # → early return → no bump, no alert.
        summary2 = ch._dispatcher_summary()
        ingest2 = AsyncMock()
        with patch.object(ch, "_ingest_saq_error", ingest2):
            await ch._fail_nodeless_run(_NoRunSession(), uuid.uuid4(), ORG, summary2)
        assert summary2["claimed_but_never_dispatched"] == 0
        ingest2.assert_not_awaited()


class _NoRunSession:
    """Session double whose ``get`` never finds a row (the no-op repair path)."""

    begin_cm = _MockBegin()

    def begin(self) -> _MockBegin:
        return self.begin_cm

    async def get(self, model: Any, pk: Any) -> None:
        return None


class TestReconcileRedisFailSafe:
    @pytest.mark.asyncio
    async def test_redis_read_error_does_nothing_and_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_RUNNING, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        q.job = AsyncMock(side_effect=RuntimeError("redis read failed"))
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["redis_errors"] == 1
        reenqueue.assert_not_awaited()
        ingest.assert_awaited_once()
        # Fail-safe: NEVER act on an unreadable Redis — no DEL/ZREM/LREM issued.
        redis_client.delete.assert_not_called()
        redis_client.zrem.assert_not_called()
        redis_client.lrem.assert_not_called()


class TestNoSaqEvictionRedispatch:
    @pytest.mark.asyncio
    async def test_evicted_job_redispatched_without_saq_eviction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Worker stopped, job hash deleted -> reconcile re-dispatches WITHOUT
        touching SAQ internals (B2): no DEL/ZREM/LREM, no SAQ list reads. The
        re-dispatch uses a FRESH key_suffix so SAQ key dedupe never suppresses
        the recovery enqueue; the atomic claim UPDATE is the real dedupe."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_EVICTED, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client, job_result=None)  # queue.job returns None
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(
                ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "new-job")
            ) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["repaired"] == 1
        # NO SAQ-internal eviction — the whole point of B2.
        redis_client.delete.assert_not_called()
        redis_client.zrem.assert_not_called()
        redis_client.lrem.assert_not_called()
        # The original deterministic key was re-checked before enqueue.
        q.job.assert_awaited_with(f"run:{RUN_EVICTED}")
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[0] == "runs"
        assert reenqueue.await_args.args[1] == str(RUN_EVICTED)
        # A fresh key_suffix is passed so SAQ dedupe can't suppress the re-enqueue.
        assert reenqueue.await_args.kwargs["key_suffix"]
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_still_deduped_after_repair_alerts_no_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enqueue gate-on-return: a still-deduped result must not loop."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_EVICTED, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client, job_result=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("deduped", None)) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["repaired"] == 0
        assert summary["deduped"] == 1
        reenqueue.assert_awaited_once()  # gate-on-return: exactly one attempt
        ingest.assert_awaited_once()


class TestEnqueueFailedRecovery:
    """B3 durable dispatch: a run whose enqueue failed (pending + dispatched_at
    set + dispatcher NULL + enqueue_failed_at set) is re-dispatched on the
    bounded interval, terminal-failed ONLY past the TTL backstop when Redis is
    reachable, and capped per tick."""

    def _enqueue_failed_row(
        self, run_id: uuid.UUID, *, marker_minutes_ago: int = 1, stale: bool = True
    ) -> SimpleNamespace:
        return _run_row(
            run_id,
            "pending",
            dispatched=True,
            stale=stale,
            dispatcher=None,
            enqueue_failed_at=datetime.now(UTC) - timedelta(minutes=marker_minutes_ago),
        )

    @pytest.mark.asyncio
    async def test_enqueue_failed_stale_heartbeat_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pending run with the enqueue-failure marker and a stale heartbeat
        is re-dispatched as execute_run with a fresh key_suffix."""
        summary, reenqueue, ingest, _, _, _ = await _run_reconcile(monkeypatch, [self._enqueue_failed_row(RUN_EVICTED)])
        assert summary["repaired"] == 1
        assert summary["enqueue_failed_redispatched"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        assert reenqueue.await_args.kwargs["key_suffix"]
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enqueue_failed_ttl_backstop_terminal_fails_when_redis_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """enqueue_failed_at older than the backstop -> terminal-failed
        'dispatch_failed' ONLY when Redis is verifiably reachable."""
        summary, reenqueue, ingest, redis_client, _, session = await _run_reconcile(
            monkeypatch, [self._enqueue_failed_row(RUN_EVICTED, marker_minutes_ago=61)]
        )
        assert summary["dispatch_failed_terminalized"] == 1
        assert summary["enqueue_failed_ttl_terminalized"] == 1
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()
        redis_client.ping.assert_awaited_once()
        # FAR-162 (P6'): the dispatch_failed run gets a compensating daily fact.
        session.record_facts.assert_awaited_once_with(RUN_EVICTED, ORG)

    @pytest.mark.asyncio
    async def test_enqueue_failed_ttl_backstop_keeps_pending_when_redis_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redis down at the backstop check -> the run stays pending (no
        terminal-fail), deferred to a later tick."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [_org_result([ORG]), _rows_result([self._enqueue_failed_row(RUN_EVICTED, marker_minutes_ago=61)])]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_client.ping.side_effect = RuntimeError("redis down")
        q = _make_queue(redis_client, job_result=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["dispatch_failed_terminalized"] == 0
        assert summary["skipped"] == 1
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enqueue_failed_per_tick_cap_defer_remaining(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Redis-outage window that hit many webhooks must not flood the queue
        on recovery: re-dispatch is capped per tick and the overflow is deferred
        (logged, counted, never alerted as an error)."""
        monkeypatch.setattr(ch, "ENQUEUE_FAILED_REDISPATCH_MAX_PER_TICK", 2)
        summary, reenqueue, ingest, _, _, _ = await _run_reconcile(
            monkeypatch,
            [
                self._enqueue_failed_row(uuid.uuid4()),
                self._enqueue_failed_row(uuid.uuid4()),
                self._enqueue_failed_row(uuid.uuid4()),
            ],
        )
        assert summary["repaired"] == 2
        assert summary["enqueue_failed_capped"] == 1
        assert reenqueue.await_count == 2
        ingest.assert_not_awaited()


class TestMidGraphWedgeTerminalizer:
    """B4: a running SAQ run wedged mid-graph past the age bound is
    terminal-failed 'executor_superseded' via the dedicated org-scoped UPDATE —
    independent of the reconcile predicates (a fresh heartbeat does NOT protect
    it, which is exactly the wedge this closes)."""

    @pytest.mark.asyncio
    async def test_aged_running_run_terminalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = _run_row(RUN_RUNNING, "running", stale=False)  # FRESH heartbeat
        summary, reenqueue, ingest, _, _, session = await _run_reconcile(
            monkeypatch,
            [],
            terminalizer_ids={"executor_superseded": [row.id]},
        )
        assert summary["mid_graph_wedge_terminalized"] == 1
        assert summary["age_terminalized"] == 1
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()
        # FAR-162 (P6'): the terminalized run gets a compensating daily fact.
        session.record_facts.assert_awaited_once_with(row.id, ORG)

    @pytest.mark.asyncio
    async def test_claim_cap_exhausted_run_terminalized_records_facts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = _run_row(RUN_RUNNING, "running", stale=True)
        summary, reenqueue, ingest, _, _, session = await _run_reconcile(
            monkeypatch,
            [],
            terminalizer_ids={"claim_cap_exhausted": [row.id]},
        )
        assert summary["claim_cap_terminalized"] == 1
        assert summary["repaired"] == 0
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()
        session.record_facts.assert_awaited_once_with(row.id, ORG)


class TestCapacityMarkerExclusion:
    """Capacity-marked runs are NOT re-dispatched while their heartbeat is
    fresh (the executor's claim→demote cycle refreshed it — the org sandbox-cap
    churn loop must be throttled). The FAR-108 carve-out admits a pending
    capacity-marked run whose heartbeat is stale or NULL so the 60s reconcile —
    not the multi-minute stale-run sweep — recovers stranded capacity-blocked
    runs. These assertions fail if the FRESH-heartbeat exclusion is removed.

    A run demoted to ``pending`` with ``error_code`` in
    (``org_capacity_limited``, ``pipeline_capacity``) has a LIVE in-process
    retry accelerator (``_retry_pending``). If ``dispatcher_reconcile``
    re-enqueues it, a second worker spawns a SECOND retry loop that can
    double-execute the run. ``_reconcile_capacity_marker_exclusion()`` is the
    WHERE-clause guard for the fresh-heartbeat rows.
    """

    def _sql(self) -> str:
        return str(ch._reconcile_capacity_marker_exclusion(120).compile(compile_kwargs={"literal_binds": True}))

    def test_null_error_code_not_excluded(self) -> None:
        """error_code IS NULL (no failure) must be allowed through."""
        assert "IS NULL" in self._sql()

    def test_org_capacity_limited_marker_excluded(self) -> None:
        assert "org_capacity_limited" in self._sql()

    def test_pipeline_capacity_marker_excluded(self) -> None:
        assert "pipeline_capacity" in self._sql()

    def test_markers_rendered_in_not_in_clause(self) -> None:
        """Both markers live in a single NOT IN clause — a run carrying either
        marker fails the whole exclusion predicate and is never re-dispatched
        (unless the stale-heartbeat carve-out below admits it)."""
        sql = self._sql()
        assert "NOT IN ('org_capacity_limited', 'pipeline_capacity')" in sql

    def test_stale_heartbeat_capacity_marked_pending_admitted(self) -> None:
        """FAR-108 carve-out: a pending capacity-marked run whose heartbeat is
        stale passes the exclusion so the 60s reconcile can re-dispatch it."""
        sql = self._sql()
        assert "runs.status = 'pending'" in sql
        assert "runs.heartbeat_at IS NULL" in sql
        assert "now() - 120 * interval '1 second'" in sql

    def test_fresh_heartbeat_capacity_marked_pending_excluded(self) -> None:
        """The carve-out only admits a run whose heartbeat is NULL or older
        than the redispatch window — a freshly-demoted sandbox-cap run
        (heartbeat refreshed by the claim) fails both clauses and stays under
        the NOT IN exclusion, so the reconcile cannot hot-loop the executor
        claim/demote churn."""
        sql = self._sql()
        assert "heartbeat_at IS NULL" in sql
        assert "now() - 120 * interval '1 second'" in sql
        assert "NOT IN" in sql


class TestReconcileCapacityMarkedRedispatch:
    """FAR-108: stranded capacity-marked pending runs are re-dispatched by the
    60s dispatcher_reconcile once their heartbeat is stale — the fast recovery
    path that replaces the ~18-minute wait for the stale-run sweep."""

    def _sql(self) -> str:
        return str(
            ch._build_re_dispatch_predicate(
                reenqueue_window=600,
                stale_window=600,
                capacity_redispatch_seconds=120,
            ).compile(compile_kwargs={"literal_binds": True})
        )

    def test_capacity_marked_stale_branch_present(self) -> None:
        """The predicate carries a dedicated branch for pending capacity-marked
        runs with a stale or NULL heartbeat (the reconcile re-dispatch path)."""
        sql = self._sql()
        assert "org_capacity_limited" in sql
        assert "pipeline_capacity" in sql
        assert "heartbeat_at IS NULL" in sql
        assert "now() - 120 * interval '1 second'" in sql

    @pytest.mark.asyncio
    async def test_capacity_marked_pending_stale_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pending org-capacity-deferred run (dispatched_at NULL, marker set)
        with a stale heartbeat is re-dispatched as execute_run when the job is
        missing."""
        summary, reenqueue, ingest, _, _, _ = await _run_reconcile(
            monkeypatch,
            [
                _run_row(
                    RUN_PENDING_UNDISPATCHED,
                    "pending",
                    dispatched=False,
                    error_code="org_capacity_limited",
                )
            ],
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "execute_run"
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deferred_outcome_not_alerted_as_deduped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A re-enqueue that dispatch_run defers (still capacity-blocked) is
        counted ``capacity_deferred`` and never raises the deduped error_event
        — it is expected backoff, not a lost job."""
        summary, reenqueue, ingest, _, _, _ = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_PENDING_UNDISPATCHED, "pending", dispatched=False)],
            dispatch_result=("deferred", None),
        )
        assert summary["capacity_deferred"] == 1
        assert summary["repaired"] == 0
        assert summary["deduped"] == 0
        reenqueue.assert_awaited_once()
        ingest.assert_not_awaited()


class TestReconcilePersistsSharedStats:
    """The cron must persist its outcome to the shared Redis key so the WEB
    process's /healthz/ready can observe it (the in-process dict is worker-local
    and invisible to the health check)."""

    @pytest.mark.asyncio
    async def test_reconcile_persists_stats_to_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        summary, _reenqueue, _ingest, redis_client, _, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_RUNNING, "running", stale=True)]
        )
        assert summary["repaired"] == 1
        stats_sets = [c for c in redis_client.set.await_args_list if c.args[0] == ch.DISPATCHER_RECONCILE_STATS_KEY]
        assert stats_sets, "dispatcher_reconcile must persist its outcome to the shared Redis stats key"
        payload = json.loads(stats_sets[0].args[1])
        assert payload["last_run_at"]
        assert payload["scanned"] == 1
        assert payload["repaired"] == 1

    @pytest.mark.asyncio
    async def test_empty_org_path_still_persists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client
        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
        ):
            summary = await ch.dispatcher_reconcile()
        assert summary["scanned"] == 0
        redis_client.set.assert_awaited_once()
        assert redis_client.set.await_args.args[0] == ch.DISPATCHER_RECONCILE_STATS_KEY


class TestReconcilePrefixAware:
    @pytest.mark.asyncio
    async def test_staging_queue_redispatched_without_saq_list_touches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same reconcile against staging-runs: the re-dispatch targets the
        staging queue and NEVER reads/writes SAQ-internal lists (B2)."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([_run_row(RUN_RUNNING, "running", stale=True)])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = MagicMock()
        q.name = "staging-runs"
        q.job_id.side_effect = lambda key: f"saq:job:staging-runs:{key}"
        q.job = AsyncMock(return_value=None)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings(saq_runs_queue="staging-runs")),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "job")) as reenqueue,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
        ):
            await ch.dispatcher_reconcile()

        # NO SAQ-internal list writes — the queue name is only used for the
        # re-dispatch itself, never for eviction keys.
        redis_client.zrem.assert_not_awaited()
        redis_client.lrem.assert_not_awaited()
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[0] == "staging-runs"
        assert reenqueue.await_args.kwargs["key_suffix"]


class TestTerminalizerSyntheticErrorDetail:
    """P7': the genuinely detail-less failure writers stamp a synthetic
    error_detail (from the ERROR_CODE_REGISTRY guidance) so the runs list /
    detail view always has something to show for these failures."""

    @pytest.mark.asyncio
    async def test_mid_graph_wedge_writes_synthetic_detail(self) -> None:
        session = _MockSession([])
        await ch._terminalize_mid_graph_wedges(session, ORG, max_age_minutes=135)
        stmt, params = session.executed[-1]
        assert "error_detail" in str(stmt)
        assert params["detail"] == ch._EXECUTOR_SUPERSEDED_ERROR_DETAIL

    @pytest.mark.asyncio
    async def test_claim_cap_exhausted_writes_synthetic_detail(self) -> None:
        session = _MockSession([])
        await ch._terminalize_claim_cap_exhausted(session, ORG, claim_cap=20, stale_seconds=600)
        stmt, params = session.executed[-1]
        assert "error_detail" in str(stmt)
        assert params["detail"] == ch._CLAIM_CAP_EXHAUSTED_ERROR_DETAIL

    @pytest.mark.asyncio
    async def test_dispatch_failed_writes_synthetic_detail(self) -> None:
        session = _MockSession([])
        await ch._fail_run_dispatch_failed(session, RUN_EVICTED, ORG)
        stmt, params = session.executed[-1]
        assert "error_detail" in str(stmt)
        assert params["detail"] == ch._DISPATCH_FAILED_ERROR_DETAIL


class TestAwaitingHumanHasCommittedDecision:
    """F6a auto-approve guard + FAR-541 gate scoping: the payload-requirement
    keys off the persisted ``decision_payload``'s ``action`` member — the
    ``hitl_claims.decision`` column only ever holds
    approved/rejected/deliver_manual, so a column-keyed check would be dead
    code and could never protect a manual-output decision whose payload was
    lost. FAR-541 iteration 2 adds gate SCOPING: the decision must resolve the
    identity the run is currently waiting at (claimed-undecided rows skip
    unconditionally as of iteration 4 / FIX C; the no-undecided-rows branch
    accepts only identity-consumable actions)."""

    _LATEST_SQL = "SELECT decision, decision_payload, gate_id FROM hitl_claims"
    _CLAIMED_SQL = "SELECT gate_id FROM hitl_claims"
    _UNDECIDED_SQL = "SELECT 1 FROM hitl_claims"

    def _mock_session(self, results: list[Any]) -> AsyncMock:
        """Session whose ``execute`` pops one result per call (the guard runs
        1-3 queries: latest decision -> claimed-undecided row -> any-undecided
        row)."""
        session = AsyncMock()
        queued = list(results)
        result_mocks: list[MagicMock] = []
        for row in queued:
            result = MagicMock()
            result.first.return_value = row
            result_mocks.append(result)
        session.execute = AsyncMock(side_effect=result_mocks)
        return session

    def _assert_query_order(self, session: AsyncMock) -> None:
        """The guard's queries must arrive in dependency order: latest decision
        first, then the pending-gate discovery."""
        calls = [str(c.args[0]) for c in session.execute.await_args_list]
        assert self._LATEST_SQL in calls[0]

    @pytest.mark.asyncio
    async def test_no_decision_row_returns_false(self) -> None:
        session = self._mock_session([None])
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False
        self._assert_query_order(session)

    @pytest.mark.asyncio
    async def test_legacy_payload_less_approved_is_committed(self) -> None:
        """FAR-541 iteration 4 (FIX C): a legacy/pre-migration approved row
        with a NULL payload is STRANDED — the claimed-branch identity match
        that used to accept it via the decision ROW's gate id is structurally
        dead under ``uq_hitl_claims_run_gate`` (a claimed-undecided row and a
        DECIDED row for the same gate cannot coexist), and the
        no-undecided-rows crash-recovery branch requires a stamp. Accepted
        residue (see the guard's docstring)."""
        session = self._mock_session(
            [
                ("approved", None, "gate-b"),
                None,  # no claimed-undecided row
                None,  # no undecided rows at all
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_legacy_payload_less_approved_different_gate_skipped(self) -> None:
        """FAR-541 scoping: a legacy payload-less approval committed for gate A
        does NOT resume a run waiting at claimed gate B (the C1 incident)."""
        session = self._mock_session(
            [
                ("approved", None, "gate-a"),
                ("gate-b",),
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_legacy_payload_less_rejected_is_committed(self) -> None:
        """FAR-541 iteration 4 (FIX C): a legacy payload-less REJECTED row is
        equally stranded in the no-undecided-rows crash-recovery branch — a
        stamp is required to verify the identity (accepted residue)."""
        session = self._mock_session(
            [
                ("rejected", None, "gate-b"),
                None,
                None,
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_plain_approve_with_payload_is_committed(self) -> None:
        """A stamped plain approval (no extra members) resumes in the
        no-undecided-rows crash-recovery branch (the run is parked at the
        decided gate's interrupt with its resume job lost)."""
        session = self._mock_session(
            [
                ("approved", {"action": "approved", "gate_id": "hitl_gate_b"}, "hitl_gate_b"),
                None,  # no claimed-undecided row
                None,  # no undecided rows at all
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True

    @pytest.mark.asyncio
    async def test_stamped_decision_for_different_gate_is_skipped(self) -> None:
        """THE C1 REGRESSION (FAR-541 iteration 2): gate A's stamped decision
        replayed onto a run waiting at claimed gate B -> SKIP."""
        session = self._mock_session(
            [
                ("approved", {"action": "approved", "gate_id": "gate-a"}, "gate-a"),
                ("gate-b",),
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_claimed_no_decision_skipped(self) -> None:
        """No committed decision at all -> never resume."""
        session = self._mock_session([None])
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_claimed_no_committed_decision_but_pending_claimed_row_skipped(self) -> None:
        """A claimed-but-undecided gate with NO committed decision anywhere ->
        SKIP (the FAR-541 original bug: empty resume auto-approved the gate)."""
        session = self._mock_session([None])
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_unclaimed_undecided_row_skips_resume(self) -> None:
        """An unclaimed undecided row (awaiting_human nobody claimed) makes the
        reconcile SKIP even when a decision exists — conservative-correct: the
        pending gate is undecided and no human has engaged with it."""
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _result_row(("approved", {"action": "approved", "gate_id": "gate-a"}, "gate-a")),
                _result_row(None),  # no claimed-undecided row
                _result_row(("x",)),  # an undecided row EXISTS
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_stamped_decision_no_undecided_rows_resume_lost_still_resumes(self) -> None:
        """Mid-resume crash recovery / the MCP resume path: the decided gate's
        claim row is decided (no undecided rows) and the decision carries its
        stamp -> resume."""
        session = self._mock_session(
            [
                ("approved", {"action": "approved", "gate_id": "gate-b"}, "gate-b"),
                None,  # no claimed-undecided row
                None,  # no undecided rows at all
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True

    @pytest.mark.asyncio
    async def test_unstamped_decision_no_undecided_rows_skipped(self) -> None:
        """A legacy unstamped decision with no undecided rows cannot be
        verified against the pending gate -> conservative SKIP."""
        session = self._mock_session(
            [
                ("approved", {"action": "approved"}, "gate-b"),
                None,
                None,
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_manual_output_with_output_is_committed(self) -> None:
        """FAR-541 iteration 4 (FIX A): a committed ``manual_output`` decision
        at a MANUAL-NODE park (identity = the node id, no undecided rows — the
        resume job was lost) RESUMES: the manual consumer completes the node
        on any payload stamped with its node id, so skipping would wedge the
        run forever (re-decide 409s, recover-node bounces)."""
        session = self._mock_session(
            [
                ("approved", {"action": "manual_output", "gate_id": "node-1", "output": {"answer": 42}}, "node-1"),
                None,  # no claimed-undecided row
                None,  # no undecided rows at all
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True

    @pytest.mark.asyncio
    async def test_manual_output_without_output_is_not_committed(self) -> None:
        """A manual-output decision whose payload lost its output is NOT
        committed — a payload-less recovery would degrade to
        ``{"action": "approved"}`` and pass that dict to the manual node as its
        output instead of resuming with the human's data."""
        session = self._mock_session([("approved", {"action": "manual_output"}, "node-1")])
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_manual_output_foreign_stamp_is_skipped(self) -> None:
        """M1: a manual_output decision stamped for its node does not resume a
        run waiting at a DIFFERENT claimed gate — the guard itself skips, so
        no re-dispatch loop."""
        session = self._mock_session(
            [
                ("approved", {"action": "manual_output", "gate_id": "node-9", "output": {"a": 1}}, "node-9"),
                ("gate-b",),
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_approve_with_modification_writer_payload_is_committed(self) -> None:
        """The approve-with-modification API (routes/hitl.py) submits
        ``approved`` plus a ``modified_output`` member (FAR-541 iteration 3:
        the retired ``approved_with_modification`` action was never produced by
        any writer and its special-case branch is pruned) — it resumes in the
        no-undecided-rows crash-recovery branch."""
        session = self._mock_session(
            [
                (
                    "approved",
                    {"action": "approved", "gate_id": "hitl_gate_b", "modified_output": {"v": 1}},
                    "hitl_gate_b",
                ),
                None,  # no claimed-undecided row
                None,  # no undecided rows at all
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True

    @pytest.mark.asyncio
    async def test_retired_approved_with_modification_action_is_not_gate_consumable(self) -> None:
        """FAR-541 iteration 3 (FIX 1 + 6a): the retired
        ``approved_with_modification`` action has no writer and no special case
        anymore — it is simply not a gate-consumable action, so in the
        no-undecided-rows crash-recovery branch it SKIPs (never re-dispatched
        into the gate's fail-closed action check, which would bounce-loop)."""
        session = self._mock_session(
            [
                (
                    "approved",
                    {"action": "approved_with_modification", "gate_id": "gate-b", "modified_output": {"v": 1}},
                    "gate-b",
                ),
                None,  # no claimed-undecided row
                None,  # no undecided rows at all
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_manual_output_no_undecided_rows_skipped(self) -> None:
        """FAR-541 (FIX A, iteration 4): a ``manual_output`` decision parked at
        a GATE identity is not a verdict the gate consumer accepts — in the
        no-undecided-rows branch it must SKIP, else resuming it would bounce
        off the gate consumer's fail-closed action check and re-dispatch-loop.
        (Manual-node parks DO resume — see
        ``test_manual_output_with_output_is_committed``.)"""
        session = self._mock_session(
            [
                ("approved", {"action": "manual_output", "gate_id": "hitl_gate_b", "output": {"a": 1}}, "hitl_gate_b"),
                None,  # no claimed-undecided row
                None,  # no undecided rows at all
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_manual_output_guardrail_park_no_undecided_rows_skipped(self) -> None:
        """FAR-541 (FIX A, iteration 4): a ``manual_output`` decision parked at
        a GUARDRAIL conformance identity is equally skipped — the conformance
        consumer's override allowlist (approved/deliver_manual/skip/replay)
        does not include ``manual_output``, so resuming would re-interrupt and
        re-dispatch-loop."""
        session = self._mock_session(
            [
                (
                    "approved",
                    {"action": "manual_output", "gate_id": "guardrail_conformance_g1", "output": {"a": 1}},
                    "guardrail_conformance_g1",
                ),
                None,  # no claimed-undecided row
                None,  # no undecided rows at all
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_legacy_empty_string_stamp_no_undecided_rows_skipped(self) -> None:
        """FAR-541 (F-5, iteration 4): a legacy ``""``-stamped payload is
        FALSY — it must skip here (cannot be verified) instead of passing the
        stamp check and bouncing off the consumer once."""
        session = self._mock_session(
            [
                ("approved", {"action": "approved", "gate_id": ""}, ""),
                None,  # no claimed-undecided row
                None,  # no undecided rows at all
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_corrupted_nondict_payload_is_skipped(self) -> None:
        """FAR-541 iteration 3 (FIX 6b): the guard and the resume-data
        reconstruction agree — a corrupted non-dict payload reconstructs to
        None, so the guard SKIPs instead of re-dispatching a resume with
        resume_data=None (an empty decision)."""
        session = self._mock_session(
            [
                ("approved", ["not", "a", "dict"], "gate-b"),
                ("gate-b",),
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_latest_decision_query_orders_deterministically(self) -> None:
        """FAR-541 iteration 3 (FIX 6c): the shared latest-decision query is
        fully deterministic — decision_at DESC NULLS LAST, then claimed_at DESC
        NULLS LAST, then id DESC — so the guard and the reconstruction (separate
        calls) can never disagree on WHICH decision wins on a timestamp tie."""
        session = self._mock_session([None])
        await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING)
        sql = str(session.execute.await_args_list[0].args[0])
        assert "ORDER BY decision_at DESC NULLS LAST, claimed_at DESC NULLS LAST, id DESC LIMIT 1" in sql

    @pytest.mark.asyncio
    async def test_pending_claimed_gate_query_orders_deterministically(self) -> None:
        """FAR-541 iteration 4 (FIX B): the claimed-undecided pending-gate
        query is deterministic too — claimed_at DESC NULLS LAST, tiebroken on
        id DESC (mirroring the latest-decision query) — so the picked row is
        stable when two claimed-undecided rows share a claim timestamp."""
        session = self._mock_session(
            [
                ("approved", {"action": "approved", "gate_id": "hitl_gate_b"}, "hitl_gate_b"),
                ("hitl_gate_b",),  # a claimed-undecided row exists -> 2nd query fires
            ]
        )
        await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING)
        claimed_sql = str(session.execute.await_args_list[1].args[0])
        assert "ORDER BY claimed_at DESC NULLS LAST, id DESC LIMIT 1" in claimed_sql

    @pytest.mark.asyncio
    async def test_deliver_manual_with_payload_is_committed(self) -> None:
        """A stamped ``deliver_manual`` decision resumes in the
        no-undecided-rows crash-recovery branch (a gate-consumable verdict
        action). The old claimed-branch coexistence this test mocked (decided
        row + claimed-undecided row, same gate) is constraint-forbidden —
        FIX C removed that dead branch."""
        session = self._mock_session(
            [
                (
                    "deliver_manual",
                    {"action": "deliver_manual", "gate_id": "hitl_gate_b", "output": {"z": 3}},
                    "hitl_gate_b",
                ),
                None,  # no claimed-undecided row
                None,  # no undecided rows at all
            ]
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True


class TestCommittedDecisionResumeData:
    """FAR-541: the reconstructed resume payload always carries the decision
    row's ``gate_id`` so the consumer's identity check passes for rows the
    reconcile is allowed to resume."""

    def _mock_session(self, row: tuple[Any, ...] | None) -> AsyncMock:
        session = AsyncMock()
        result = MagicMock()
        result.first.return_value = row
        session.execute = AsyncMock(return_value=result)
        return session

    @pytest.mark.asyncio
    async def test_no_decision_returns_none(self) -> None:
        session = self._mock_session(None)
        assert await ch._committed_decision_resume_data(session, ORG, RUN_AWAITING) is None

    @pytest.mark.asyncio
    async def test_legacy_payload_less_row_gets_row_gate_id(self) -> None:
        session = self._mock_session(("approved", None, "gate-b"))
        data = await ch._committed_decision_resume_data(session, ORG, RUN_AWAITING)
        assert data == {"action": "approved", "gate_id": "gate-b"}

    @pytest.mark.asyncio
    async def test_stamped_payload_round_trips_verbatim(self) -> None:
        payload = {"action": "rejected", "gate_id": "gate-b", "reason": "no"}
        session = self._mock_session(("rejected", payload, "gate-b"))
        data = await ch._committed_decision_resume_data(session, ORG, RUN_AWAITING)
        assert data == payload

    @pytest.mark.asyncio
    async def test_pre_stamping_payload_gets_row_gate_id_added(self) -> None:
        payload = {"action": "approved", "notes": "ok"}
        session = self._mock_session(("approved", payload, "gate-b"))
        data = await ch._committed_decision_resume_data(session, ORG, RUN_AWAITING)
        assert data == {"action": "approved", "notes": "ok", "gate_id": "gate-b"}

    @pytest.mark.asyncio
    async def test_json_string_payload_is_parsed(self) -> None:
        session = self._mock_session(("approved", '{"action": "approved", "gate_id": "gate-b"}', "gate-b"))
        data = await ch._committed_decision_resume_data(session, ORG, RUN_AWAITING)
        assert data == {"action": "approved", "gate_id": "gate-b"}


class TestRunApiKeySweepWiring:
    """FAR-296 Phase 3b-2: the compensating per-run API-key revocation sweep is
    wired into the dispatcher_reconcile periodic tick (the FAR-189 lesson: an
    unwired sweep is dead code, so the wiring is regression-tested here)."""

    def _patches(self, monkeypatch: pytest.MonkeyPatch, api_key_module: Any, sweep_mock: Any) -> tuple[Any, list[Any]]:
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client
        return factory, [
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "run_classification_reconcile", new=AsyncMock(return_value={})),
            patch.object(ch, "enforce_no_delivery_streaks", new=AsyncMock(return_value={})),
            patch.object(api_key_module, "revoke_run_api_key_sweep", new=sweep_mock),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "new-job-id")),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
            patch.object(
                ch,
                "_awaiting_human_has_committed_decision",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock),
            patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0),
        ]

    @pytest.mark.asyncio
    async def test_dispatcher_reconcile_invokes_run_api_key_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-189 wiring regression test: the real ``dispatcher_reconcile``
        must invoke the per-run API-key revocation sweep and fold its counters
        into the summary.

        Deleting the ``await revoke_run_api_key_sweep(...)`` line from
        ``cron_helpers.dispatcher_reconcile`` must leave this test red — the
        sweep mock is asserted awaited once AND the folded summary keys would be
        missing from the summary dict.
        """
        from modulo.auth import api_key as api_key_module

        sweep_mock = AsyncMock(return_value={"scanned": 3, "revoked": 2, "errors": 0})
        with contextlib.ExitStack() as stack:
            factory, patches = self._patches(monkeypatch, api_key_module, sweep_mock)
            for p in patches:
                stack.enter_context(p)
            summary = await ch.dispatcher_reconcile()

        sweep_mock.assert_awaited_once_with(factory)
        assert summary["run_api_key_scanned"] == 3
        assert summary["run_api_key_revoked"] == 2
        assert summary["run_api_key_errors"] == 0

    @pytest.mark.asyncio
    async def test_run_api_key_sweep_failure_does_not_fail_reconcile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sweep exception is caught and logged (never raised through the
        tick); the reconcile survives and folds the counters to zero."""
        from modulo.auth import api_key as api_key_module

        sweep_mock = AsyncMock(side_effect=RuntimeError("boom"))
        with contextlib.ExitStack() as stack:
            _factory, patches = self._patches(monkeypatch, api_key_module, sweep_mock)
            for p in patches:
                stack.enter_context(p)
            summary = await ch.dispatcher_reconcile()

        assert summary["run_api_key_scanned"] == 0
        assert summary["run_api_key_revoked"] == 0
        assert summary["run_api_key_errors"] == 0
        # The tick completed its bookkeeping despite the sweep failure.
        assert summary["repaired"] == 0
        assert summary["scanned"] == 0


class TestRollbackThresholdsWiring:
    """FAR-296 Phase 5b: the rollback threshold evaluator is wired into the
    dispatcher_reconcile periodic tick (the FAR-189 lesson: an unwired sweep
    is dead code, so the wiring is regression-tested here)."""

    @pytest.mark.asyncio
    async def test_dispatcher_reconcile_invokes_rollback_thresholds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-189 wiring regression test: the real ``dispatcher_reconcile``
        must invoke the rollback threshold evaluator and fold its counters
        into the summary.

        Deleting the ``await evaluate_rollback_thresholds(...)`` line from
        ``cron_helpers._run_reconcile_sweeps`` must leave this test red --
        the mock is asserted awaited once AND the folded summary keys would be
        missing from the summary dict.
        """
        from modulo.core import rollback_thresholds as rt_module

        threshold_mock = AsyncMock(return_value={"orgs_checked": 2, "anomalies_found": 1, "flagged_orgs": ["org-1"]})
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "run_classification_reconcile", new=AsyncMock(return_value={})),
            patch.object(ch, "enforce_no_delivery_streaks", new=AsyncMock(return_value={})),
            patch.object(rt_module, "evaluate_rollback_thresholds", new=threshold_mock),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "new-job-id")),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
            patch.object(
                ch,
                "_awaiting_human_has_committed_decision",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock),
            patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0),
        ):
            summary = await ch.dispatcher_reconcile()

        threshold_mock.assert_awaited_once_with(factory)
        assert summary["rollback_thresholds_checked"] == 2
        assert summary["rollback_thresholds_flagged"] == 1

    @pytest.mark.asyncio
    async def test_rollback_thresholds_failure_does_not_fail_reconcile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A threshold evaluation exception is caught and logged (never raised
        through the tick); the reconcile survives and folds the counters to zero."""
        from modulo.core import rollback_thresholds as rt_module

        threshold_mock = AsyncMock(side_effect=RuntimeError("boom"))
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG]), _rows_result([])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_open_system_factory", return_value=factory),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
            patch.object(ch, "run_classification_reconcile", new=AsyncMock(return_value={})),
            patch.object(ch, "enforce_no_delivery_streaks", new=AsyncMock(return_value={})),
            patch.object(rt_module, "evaluate_rollback_thresholds", new=threshold_mock),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=("enqueued", "new-job-id")),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
            patch.object(
                ch,
                "_awaiting_human_has_committed_decision",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock),
            patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0),
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["rollback_thresholds_checked"] == 0
        assert summary["rollback_thresholds_flagged"] == 0
        # The tick completed its bookkeeping despite the sweep failure.
        assert summary["repaired"] == 0
        assert summary["scanned"] == 0


class TestTerminalizeExpiredHitlGates:
    """FAR-648: the expired-HITL-gate terminalizer's SQL contract.

    The sweep is a SINGLE guarded UPDATE — there is no separate select, so the
    TOCTOU re-validation (still awaiting_human, gates still unclaimed +
    undecided + past expiry) happens by construction inside the org
    transaction: the predicate is re-checked at execution time and a gate
    claimed mid-tick no longer matches (rowcount 0). These tests pin the
    predicate shape; the behavioral matrix runs against real Postgres in
    tests/integration/test_org_sandbox_capacity.py."""

    async def _run(self, terminalized: list[uuid.UUID] | None = None) -> tuple[list[uuid.UUID], _MockSession]:
        session = _MockSession([])
        if terminalized is not None:
            session.terminalizer_rows["hitl_gate_expired"] = terminalized
        returned = await ch._terminalize_expired_hitl_gates(session, ORG, grace_seconds=3600)
        return returned, session

    @pytest.mark.asyncio
    async def test_writes_cancelled_with_hitl_gate_expired_code_and_detail(self) -> None:
        """P7' contract: the terminalizer writes status='cancelled' with the
        new ``hitl_gate_expired`` code and its synthetic error_detail."""
        _returned, session = await self._run()
        stmt, params = session.executed[-1]
        sql = str(stmt)
        assert "status='cancelled'" in sql
        assert "error_code=:code" in sql
        assert params["code"] == ch._HITL_GATE_EXPIRED_ERROR_CODE
        assert ch._HITL_GATE_EXPIRED_ERROR_CODE == "hitl_gate_expired"
        assert params["detail"] == ch._HITL_GATE_EXPIRED_ERROR_DETAIL

    @pytest.mark.asyncio
    async def test_source_status_bound_to_awaiting_human_constant(self) -> None:
        """qa F15: the ``awaiting_human`` status literal is a bound param named
        from the shared model constant — never a raw SQL literal."""
        _returned, session = await self._run()
        stmt, params = session.executed[-1]
        assert "status=:awaiting_status" in str(stmt)
        assert params["awaiting_status"] == ch.AWAITING_HUMAN_STATUS
        assert ch.AWAITING_HUMAN_STATUS == "awaiting_human"

    @pytest.mark.asyncio
    async def test_predicate_keeps_cancel_wins_precedence(self) -> None:
        """A cancellation-requested run is owned by the cancel path — the
        terminalizer must never write ``cancelled`` over it."""
        _returned, session = await self._run()
        assert "cancellation_requested=false" in str(session.executed[-1][0])

    @pytest.mark.asyncio
    async def test_predicate_revalidates_gate_state_in_the_update(self) -> None:
        """TOCTOU safety: the single UPDATE carries the full gate predicate —
        an EXISTS clause for an expired unclaimed undecided gate AND a
        NOT EXISTS clause excluding any claimed or in-grace undecided gate —
        so a gate claimed between the tick's read and this write no longer
        matches."""
        _returned, session = await self._run()
        sql = str(session.executed[-1][0])
        assert "NOT EXISTS" in sql
        assert "hc.decision IS NULL" in sql
        assert "hc.account_id IS NULL" in sql
        assert "hc.expires_at < now() - (:grace_seconds * interval '1 second')" in sql
        assert "hc2.account_id IS NOT NULL" in sql
        assert "hc2.expires_at >= now() - (:grace_seconds * interval '1 second')" in sql

    @pytest.mark.asyncio
    async def test_returns_terminalized_ids_and_warns_per_run(self, caplog: pytest.LogCaptureFixture) -> None:
        expired = [uuid.uuid4(), uuid.uuid4()]
        with caplog.at_level(logging.WARNING, logger="modulo.core.cron_helpers"):
            returned, _session = await self._run(expired)
        assert returned == expired
        assert sum("expired-HITL-gate zombie terminalized" in r.message for r in caplog.records) == len(expired)

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self) -> None:
        returned, _session = await self._run()
        assert not returned


class TestHitlGateExpiryTerminalizerWiring:
    """FAR-648 wiring: the reconcile tick must invoke the expired-HITL-gate
    terminalizer with the settings grace and fold its results into the summary
    stats key + the post-commit compensating daily fact (P6', FAR-162)."""

    def test_stats_key_declared_in_both_vocabularies(self) -> None:
        assert "hitl_gate_expired_terminalized" in ch._dispatcher_reconcile_stats
        assert "hitl_gate_expired_terminalized" in ch._dispatcher_summary()

    @pytest.mark.asyncio
    async def test_reconcile_invokes_terminalizer_with_settings_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expired_run = uuid.uuid4()
        terminalizer = AsyncMock(return_value=[expired_run])
        summary, _reenqueue, _ingest, _redis, _awaiting, session = await _run_reconcile(
            monkeypatch, [], terminalizer=terminalizer
        )

        terminalizer.assert_awaited_once()
        assert terminalizer.await_args.kwargs["grace_seconds"] == 3600
        assert summary["hitl_gate_expired_terminalized"] == 1
        session.record_facts.assert_awaited_once_with(expired_run, ORG)

    @pytest.mark.asyncio
    async def test_grace_is_settings_derived_not_hardcoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The knob is read from settings every tick — an operator override
        reaches the terminalizer unchanged."""
        terminalizer = AsyncMock(return_value=[])
        await _run_reconcile(
            monkeypatch,
            [],
            terminalizer=terminalizer,
            settings_overrides={"hitl_gate_cancel_grace_seconds": 180},
        )

        assert terminalizer.await_args.kwargs["grace_seconds"] == 180


class TestRecordFactForTerminalizedRun:
    """FAR-648 phantom-fact guard: the compensating daily-fact recorder (P6',
    FAR-162) re-selects the run AFTER the per-org transactions commit. The
    terminalized ids are collected BEFORE that commit — if an org transaction
    rolled back after its ids were collected, the run is NOT terminal and the
    fact would be a lie. The recorder must record for a terminal run and skip
    a non-terminal one."""

    async def _invoke(self, monkeypatch: pytest.MonkeyPatch, *, status: str) -> AsyncMock:
        _patch_env(monkeypatch)
        session = _MockSession([])
        factory = MagicMock(return_value=session)
        record = AsyncMock()
        run_id = uuid.uuid4()
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.db.crud.run.get_run",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(id=run_id, status=status),
            ),
            patch("modulo.core.analytics.record_fact_for_terminal_failed_run", record),
        ):
            await ch._record_fact_for_terminalized_run(run_id, ORG)
        return record

    @pytest.mark.asyncio
    async def test_records_for_terminal_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run re-selected in a terminal status (``cancelled`` — the
        terminalizer's own write) gets its compensating daily fact."""
        record = await self._invoke(monkeypatch, status="cancelled")
        record.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_non_terminal_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run still ``awaiting_human`` at fact time means the terminalizer's
        org transaction rolled back after the id was collected — no fact may
        be written (it would describe a terminal transition that never
        happened)."""
        record = await self._invoke(monkeypatch, status="awaiting_human")
        record.assert_not_awaited()


# ---------------------------------------------------------------------------
# FAR-746 resilience tests
# ---------------------------------------------------------------------------


class TestDispatcherReconcileInnerDeadline:
    """FAR-746 inner deadline: the sweep manages its own time budget INSIDE
    the SAQ job.  On deadline expiry the current per-org transaction rolls
    back, failure stats are persisted (status='timeout'), and the function
    returns gracefully — the SAQ outer timeout must never fire."""

    @pytest.mark.asyncio
    async def test_inner_deadline_persists_timeout_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When asyncio.timeout fires, the stats blob must carry
        status='timeout' and a fresh last_run_at — not a stale blob."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        async def _slow_body(*_a: Any, **_kw: Any) -> None:
            await asyncio.sleep(10)  # simulate long-running tick

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(ch, "_open_system_factory", return_value=factory))
            stack.enter_context(
                patch.object(ch, "get_settings", return_value=_settings(dispatcher_reconcile_budget_seconds=0))
            )
            stack.enter_context(patch.object(ch, "AsyncRedis", redis_cls))
            stack.enter_context(patch.object(ch, "RedisQueue", MagicMock(return_value=q)))
            stack.enter_context(patch.object(ch, "_dispatcher_reconcile_body", _slow_body))
            summary = await ch.dispatcher_reconcile()

        assert summary["status"] == "timeout"
        assert summary["last_error"] is not None
        assert "TimeoutError" in summary["last_error"]
        # The in-process mirror (worker-process view) carries the failure too.
        assert ch._dispatcher_reconcile_stats["status"] == "timeout"
        # Stats were persisted to Redis (the write was called).
        redis_client.set.assert_awaited()

    @pytest.mark.asyncio
    async def test_inner_deadline_does_not_leak_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the inner deadline fires, no session factory should be
        called after the timeout — the session was closed by the context
        manager before the timeout propagated."""
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        async def _slow_body(*_a: Any, **_kw: Any) -> None:
            # Simulate a slow tick that exceeds the budget.
            await asyncio.sleep(10)

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(ch, "_open_system_factory", return_value=factory))
            stack.enter_context(
                patch.object(ch, "get_settings", return_value=_settings(dispatcher_reconcile_budget_seconds=0))
            )
            stack.enter_context(patch.object(ch, "AsyncRedis", redis_cls))
            stack.enter_context(patch.object(ch, "RedisQueue", MagicMock(return_value=q)))
            stack.enter_context(patch.object(ch, "_dispatcher_reconcile_body", _slow_body))
            await ch.dispatcher_reconcile()

        # The session factory was only called once (by _open_system_factory)
        # for the empty-org path — no leaked session.
        assert factory.call_count <= 1


class TestDispatcherReconcileUnexpectedException:
    """FAR-746 failure heartbeat: an unexpected exception persists
    status='failed' + last_error and re-raises."""

    @pytest.mark.asyncio
    async def test_unexpected_exception_persists_failed_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession([_org_result([ORG])])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client

        async def _exploding_body(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("boom")

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(ch, "_open_system_factory", return_value=factory))
            stack.enter_context(patch.object(ch, "get_settings", return_value=_settings()))
            stack.enter_context(patch.object(ch, "AsyncRedis", redis_cls))
            stack.enter_context(patch.object(ch, "RedisQueue", MagicMock(return_value=q)))
            stack.enter_context(patch.object(ch, "_dispatcher_reconcile_body", _exploding_body))
            with pytest.raises(RuntimeError, match="boom"):
                await ch.dispatcher_reconcile()

        # The failure heartbeat was persisted to Redis.
        redis_client.set.assert_awaited()
        # The failure reached the in-process mirror too.
        assert ch._dispatcher_reconcile_stats["status"] == "failed"
        assert "RuntimeError" in (ch._dispatcher_reconcile_stats["last_error"] or "")


class TestDispatcherReconcileFactsBatchCap:
    """FAR-746 batch cap: the compensating daily-fact writes are bounded by
    dispatcher_reconcile_facts_max_per_tick.  Overflow is counted in
    facts_deferred and drains on subsequent ticks."""

    @pytest.mark.asyncio
    async def test_facts_deferred_when_over_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The REAL body applies the cap: 5 terminalized ids with facts_max=2
        means exactly 2 facts are written and 3 are deferred."""
        _patch_env(monkeypatch)
        session = _MockSession([])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client
        facts_org = uuid.uuid4()

        async def _five_terminalized(
            _factory: Any,
            _q: Any,
            _rc: Any,
            org_id: uuid.UUID,
            _pred: Any,
            _nw: Any,
            _mam: Any,
            _cc: Any,
            _sw: Any,
            _crs: Any,
            _efr: Any,
            _summary: Any,
            terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
            _grace: Any,
            **_kw: Any,
        ) -> int:
            terminalized_run_ids.extend((uuid.uuid4(), org_id) for _ in range(5))
            return 0

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(ch, "_open_system_factory", return_value=factory))
            stack.enter_context(
                patch.object(
                    ch,
                    "get_settings",
                    return_value=_settings(dispatcher_reconcile_facts_max_per_tick=2),
                )
            )
            stack.enter_context(patch.object(ch, "AsyncRedis", redis_cls))
            stack.enter_context(patch.object(ch, "RedisQueue", MagicMock(return_value=q)))
            stack.enter_context(patch.object(ch, "_collect_org_ids", new_callable=AsyncMock, return_value=[facts_org]))
            stack.enter_context(patch.object(ch, "_reconcile_org", side_effect=_five_terminalized))
            stack.enter_context(patch.object(ch, "_run_reconcile_sweeps", new_callable=AsyncMock))
            stack.enter_context(patch.object(ch, "_overlay_dual_write_counters", new_callable=AsyncMock))
            stack.enter_context(patch.object(ch, "_update_reconcile_telemetry", new_callable=AsyncMock))
            record_facts = stack.enter_context(
                patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock)
            )
            summary = await ch.dispatcher_reconcile()

        # Only 2 facts were written (the cap); 3 were deferred.
        assert record_facts.await_count == 2
        assert summary["facts_deferred"] == 3
        # The success path keeps the ok status (never a failure heartbeat).
        assert summary["status"] == "ok"
        # The tick's outcome was persisted (success path).
        redis_client.set.assert_awaited()

    @pytest.mark.asyncio
    async def test_facts_all_written_under_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Under the cap every terminalized run gets its fact — the cap must
        not silently drop writes the budget could have covered."""
        _patch_env(monkeypatch)
        session = _MockSession([])
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        q = _make_queue(redis_client)
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client
        facts_org = uuid.uuid4()

        async def _two_terminalized(
            _factory: Any,
            _q: Any,
            _rc: Any,
            org_id: uuid.UUID,
            _pred: Any,
            _nw: Any,
            _mam: Any,
            _cc: Any,
            _sw: Any,
            _crs: Any,
            _efr: Any,
            _summary: Any,
            terminalized_run_ids: list[tuple[uuid.UUID, uuid.UUID]],
            _grace: Any,
            **_kw: Any,
        ) -> int:
            terminalized_run_ids.extend((uuid.uuid4(), org_id) for _ in range(2))
            return 0

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(ch, "_open_system_factory", return_value=factory))
            stack.enter_context(patch.object(ch, "get_settings", return_value=_settings()))
            stack.enter_context(patch.object(ch, "AsyncRedis", redis_cls))
            stack.enter_context(patch.object(ch, "RedisQueue", MagicMock(return_value=q)))
            stack.enter_context(patch.object(ch, "_collect_org_ids", new_callable=AsyncMock, return_value=[facts_org]))
            stack.enter_context(patch.object(ch, "_reconcile_org", side_effect=_two_terminalized))
            stack.enter_context(patch.object(ch, "_run_reconcile_sweeps", new_callable=AsyncMock))
            stack.enter_context(patch.object(ch, "_overlay_dual_write_counters", new_callable=AsyncMock))
            stack.enter_context(patch.object(ch, "_update_reconcile_telemetry", new_callable=AsyncMock))
            record_facts = stack.enter_context(
                patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock)
            )
            summary = await ch.dispatcher_reconcile()

        assert record_facts.await_count == 2
        assert summary["facts_deferred"] == 0
        redis_client.set.assert_awaited()


class TestTerminalizeBatchCap:
    """FAR-746 batch caps: the per-tick terminalizer row caps bound the
    heavy per-org UPDATEs so a big zombie backlog drains across 60s ticks."""

    @pytest.mark.asyncio
    async def test_mid_graph_wedge_update_carries_limit_param(self) -> None:
        session = _MockSession([])
        await ch._terminalize_mid_graph_wedges(session, ORG, max_age_minutes=135, max_rows=7)
        stmt, params = session.executed[-1]
        assert "LIMIT :max_rows" in str(stmt)
        assert params["max_rows"] == 7

    @pytest.mark.asyncio
    async def test_terminalizer_updates_uncapped_by_default(self) -> None:
        """Direct calls without max_rows stay uncapped (the LIMIT sentinel
        never truncates a realistic backlog)."""
        session = _MockSession([])
        await ch._terminalize_mid_graph_wedges(session, ORG, max_age_minutes=135)
        _stmt, params = session.executed[-1]
        assert params["max_rows"] == ch._TERMINALIZE_UNLIMITED_ROWS

    def test_terminalize_max_rows_coercion(self) -> None:
        assert ch._terminalize_max_rows(None) == ch._TERMINALIZE_UNLIMITED_ROWS
        assert ch._terminalize_max_rows(0) == ch._TERMINALIZE_UNLIMITED_ROWS
        assert ch._terminalize_max_rows(-5) == ch._TERMINALIZE_UNLIMITED_ROWS
        assert ch._terminalize_max_rows("25") == 25
        assert ch._terminalize_max_rows(object()) == ch._TERMINALIZE_UNLIMITED_ROWS
        assert ch._terminalize_max_rows(25) == 25

    @pytest.mark.asyncio
    async def test_claim_cap_and_hitl_updates_carry_limit_param(self) -> None:
        session = _MockSession([])
        await ch._terminalize_claim_cap_exhausted(session, ORG, claim_cap=20, stale_seconds=600, max_rows=9)
        await ch._terminalize_expired_hitl_gates(session, ORG, grace_seconds=3600, max_rows=11)
        claim_stmt, claim_params = session.executed[-2]
        hitl_stmt, hitl_params = session.executed[-1]
        assert "LIMIT :max_rows" in str(claim_stmt)
        assert claim_params["max_rows"] == 9
        assert "LIMIT :max_rows" in str(hitl_stmt)
        assert hitl_params["max_rows"] == 11

    @pytest.mark.asyncio
    async def test_terminalize_capped_counter_fires_at_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wedge terminalizer returning exactly terminalize_max rows counts
        the terminalize_capped overflow signal — the backlog did not fully
        drain this tick."""
        summary, _, _, _, _, _ = await _run_reconcile(
            monkeypatch,
            [],
            terminalizer_ids={"executor_superseded": [uuid.uuid4() for _ in range(2)]},
            settings_overrides={"dispatcher_reconcile_terminalize_max_per_tick": 2},
        )
        assert summary["mid_graph_wedge_terminalized"] == 2
        assert summary["terminalize_capped"] == 1

    @pytest.mark.asyncio
    async def test_terminalize_capped_counter_silent_under_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wedge terminalizer returning fewer rows than the cap does NOT
        count an overflow."""
        summary, _, _, _, _, _ = await _run_reconcile(
            monkeypatch,
            [],
            terminalizer_ids={"executor_superseded": [uuid.uuid4()]},
            settings_overrides={"dispatcher_reconcile_terminalize_max_per_tick": 25},
        )
        assert summary["mid_graph_wedge_terminalized"] == 1
        assert summary["terminalize_capped"] == 0
