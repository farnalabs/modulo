"""Unit tests for dispatcher_reconcile (plan F3c) — predicate matrix, no-SAQ-
eviction re-dispatch, Redis-error fail-safe, re-enqueue gate-on-return,
discriminator, durable-dispatch recovery (B3) and safe terminalizers (B4/B5).
"""

from __future__ import annotations

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
            # Dedicated org-scoped terminalizer UPDATEs (B4/B5) — zero rows
            # matched by default; individual tests configure terminalizer_rows.
            ids = self.terminalizer_rows.get("executor_superseded", [])
            if "claim_cap_exhausted" in s:
                ids = self.terminalizer_rows.get("claim_cap_exhausted", [])
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
        "modulo_telemetry_enabled": False,
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
) -> tuple[dict[str, Any], Any, Any, Any, Any, _MockSession]:
    _patch_env(monkeypatch)
    session = _MockSession([_org_result([ORG]), _rows_result(rows)])
    if terminalizer_ids:
        session.terminalizer_rows = terminalizer_ids
    factory = MagicMock(return_value=session)
    redis_client = AsyncMock()
    q = _make_queue(redis_client, job_result=queue_job_result)
    redis_cls = MagicMock()
    redis_cls.from_url.return_value = redis_client

    with (
        patch.object(ch, "_open_system_factory", return_value=factory),
        patch.object(ch, "get_settings", return_value=_settings()),
        patch.object(ch, "AsyncRedis", redis_cls),
        patch.object(ch, "RedisQueue", MagicMock(return_value=q)),
        patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock, return_value=dispatch_result) as reenqueue,
        patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        patch.object(
            ch,
            "_awaiting_human_has_committed_decision",
            new_callable=AsyncMock,
            return_value=awaiting_committed,
        ) as awaiting_guard,
        patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock) as record_facts,
    ):
        if capacity_free is False:
            with patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=5):
                summary = await ch.dispatcher_reconcile()
        else:
            with patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock, return_value=0):
                summary = await ch.dispatcher_reconcile()

    session.record_facts = record_facts
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
        """F6a gated recovery: a claimed run with a stale heartbeat and NO SAQ
        job in Redis IS re-dispatched as resume_run (a claim was already made —
        the guard does not apply)."""
        summary, reenqueue, _, _, awaiting_guard, _ = await _run_reconcile(
            monkeypatch, [_run_row(RUN_AWAITING, "claimed", stale=True)]
        )
        assert summary["repaired"] == 1
        reenqueue.assert_awaited_once()
        assert reenqueue.await_args.args[3] == "resume_run"
        awaiting_guard.assert_not_awaited()

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
        run is terminal-failed so it is never left dangling in a re-dispatch loop."""
        summary, reenqueue, ingest, _, _, session = await _run_reconcile(
            monkeypatch,
            [_run_row(RUN_RUNNING, "running", stale=False, nodeless=True, claim_count=3)],
        )
        assert summary["nodeless_failed"] == 1
        assert summary["nodeless_redispatched"] == 0
        reenqueue.assert_not_awaited()
        ingest.assert_not_awaited()
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


class TestNodelessRedispatchBudget:
    """FAR-509: the policy-less nodeless re-dispatch budget is configurable via
    SAQ_NODELESS_REDISPATCH_BUDGET (default 2). Direct unit checks of
    ``_should_redispatch_nodeless`` — the retry-policy taxonomy. FAR-525 qa
    gate: the decision keys on the POLICY's EVENT CONTENT (its ``on`` list),
    NEVER on dict non-emptiness — a policy whose ``on`` is empty/missing is
    treated the SAME as ``{}`` (budget-default repair), so the FAR-525 GUI's
    no-op panel save (``{on: [], max_retries: 0, backoff_schedule: {...}}``,
    always non-empty) cannot silently convert budget-default repair into
    terminal-fail."""

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
        """FAR-525 qa gate — the 4-row characterization, row 4 (THE FIX): the
        FAR-525 GUI's no-op panel save always stores a NON-EMPTY policy
        (``{on: [], max_retries: 0, backoff_schedule: {...}}``). Its ``on`` is
        EMPTY, so it must be treated the SAME as ``{}`` — budget-default
        repair, NOT the terminal-fail a non-emptiness key produced."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())
        noop_save = {"on": [], "max_retries": 0, "backoff_schedule": {"delay_seconds": 45, "multiplier": 2.0}}
        assert ch._should_redispatch_nodeless(self._row(2, retry_policy=noop_save)) is True
        assert ch._should_redispatch_nodeless(self._row(3, retry_policy=noop_save)) is False

    def test_missing_on_key_gets_budget_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-empty policy whose `on` key is MISSING is also event-empty:
        budget-default repair (event content, not dict shape, decides)."""
        monkeypatch.setattr(ch, "get_settings", lambda: _settings())
        assert ch._should_redispatch_nodeless(self._row(2, retry_policy={"max_retries": 3})) is True
        assert ch._should_redispatch_nodeless(self._row(3, retry_policy={"max_retries": 3})) is False


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
    """F6a auto-approve guard: the payload-requirement keys off the persisted
    ``decision_payload``'s ``action`` member — the ``hitl_claims.decision``
    column only ever holds approved/rejected/deliver_manual, so a column-keyed
    check would be dead code and could never protect a manual-output decision
    whose payload was lost."""

    def _mock_session(self, row: tuple[Any, Any] | None) -> AsyncMock:
        session = AsyncMock()
        result = MagicMock()
        result.first.return_value = row
        session.execute = AsyncMock(return_value=result)
        return session

    @pytest.mark.asyncio
    async def test_no_decision_row_returns_false(self) -> None:
        session = self._mock_session(None)
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_legacy_payload_less_approved_is_committed(self) -> None:
        """A legacy/pre-migration approved row with a NULL payload degrades to
        ``{"action": "approved"}`` — a plain approval needs no payload."""
        session = self._mock_session(("approved", None))
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True

    @pytest.mark.asyncio
    async def test_legacy_payload_less_rejected_is_committed(self) -> None:
        session = self._mock_session(("rejected", None))
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True

    @pytest.mark.asyncio
    async def test_plain_approve_with_payload_is_committed(self) -> None:
        session = self._mock_session(("approved", {"action": "approved"}))
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True

    @pytest.mark.asyncio
    async def test_manual_output_with_output_is_committed(self) -> None:
        session = self._mock_session(("approved", {"action": "manual_output", "output": {"answer": 42}}))
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True

    @pytest.mark.asyncio
    async def test_manual_output_without_output_is_not_committed(self) -> None:
        """A manual-output decision whose payload lost its output is NOT
        committed — a payload-less recovery would degrade to
        ``{"action": "approved"}`` and pass that dict to the manual node as its
        output instead of resuming with the human's data."""
        session = self._mock_session(("approved", {"action": "manual_output"}))
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_approved_with_modification_with_output_is_committed(self) -> None:
        session = self._mock_session(
            ("approved", {"action": "approved_with_modification", "modified_output": {"v": 1}})
        )
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True

    @pytest.mark.asyncio
    async def test_approved_with_modification_without_output_is_not_committed(self) -> None:
        """An approve-with-modification decision without its modified output is
        NOT committed — a payload-less recovery would drop the human's
        modification and resume as a plain approval."""
        session = self._mock_session(("approved", {"action": "approved_with_modification"}))
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is False

    @pytest.mark.asyncio
    async def test_deliver_manual_with_payload_is_committed(self) -> None:
        session = self._mock_session(("deliver_manual", {"action": "deliver_manual", "output": {"z": 3}}))
        assert await ch._awaiting_human_has_committed_decision(session, ORG, RUN_AWAITING) is True


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
        ``cron_helpers.dispatcher_reconcile`` must leave this test red �?" the
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
