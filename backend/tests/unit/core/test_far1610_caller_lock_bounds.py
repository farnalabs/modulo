"""FAR-1610 — caller-side lock bounds for the shared ``db/crud/run.py`` writers.

FAR-1601 groups A/B/C bounded the periodic sweeps + executor-path hot-``runs``
writers. Group C deliberately left the SHARED ``db/crud/run.py`` functions
(``update_run_status`` / ``unpark_parked_run`` / ``transition_run`` /
``request_cancellation``) unbounded at the CRUD layer: bounding them there
would let an ``except Exception`` caller swallow a 55P03 into a SILENT LOST
WRITE. The recipe is to issue the transaction-scoped bound in the CALLER's
transaction, before the first lock.

These tests pin, per caller:

1. **Wiring + ORDER** (fail-first): the bound is issued BEFORE the caller's
   first lock, in the caller's transaction. Without the source change no bound
   call exists and every ordering assertion fails.
2. **The 55P03 contract**, chosen per path: a REQUEST path fails visibly; a
   recovery/idempotent path skips + WARNs + re-processes.

The real-Postgres contention behaviour (a held row lock actually timing out)
is an integration concern; the unit seam here drives the same 55P03 the bound
produces.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from modulo.core.hitl_manager import GateNotFoundError, HITLManager
from modulo.core.pipeline_engine.executor import PipelineExecutor, RunNotFoundError


def _lock_timeout_error(statement: str = "UPDATE runs") -> OperationalError:
    """Simulated row-lock contention: asyncpg's real ``LockNotAvailableError``
    (SQLSTATE 55P03) wrapped exactly the way SQLAlchemy surfaces it."""
    from asyncpg import exceptions as asyncpg_exceptions

    driver_error = asyncpg_exceptions.LockNotAvailableError("canceling statement due to lock timeout")
    return OperationalError(statement, {}, driver_error)


@asynccontextmanager
async def _acm(obj: Any):
    yield obj


def _begin_session() -> AsyncMock:
    """Session double whose ``begin()`` is a proper async context manager."""
    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


# ---------------------------------------------------------------------------
# Executor — ``_check_spend_ceiling_gate`` (terminal write, fail-open + log)
# ---------------------------------------------------------------------------


def _make_session(org: Any) -> AsyncMock:
    session = AsyncMock()

    def _execute(stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=org if "organisations" in str(stmt).lower() else None)
        return result

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _fake_executor(session: AsyncMock) -> MagicMock:
    fake_self = MagicMock()
    fake_self._session_factory = MagicMock(side_effect=lambda: _acm(session))
    return fake_self


def _org(*, spend_ceiling_cents: int, org_cumulative_spend_cents: int = 0) -> MagicMock:
    org = MagicMock()
    org.id = uuid.uuid4()
    org.spend_ceiling_cents = spend_ceiling_cents
    org.org_cumulative_spend_cents = org_cumulative_spend_cents
    return org


async def test_spend_ceiling_gate_bounds_before_the_terminal_write() -> None:
    order: list[str] = []
    org = _org(spend_ceiling_cents=0)
    run = MagicMock()
    run.id = uuid.uuid4()
    fake_self = _fake_executor(_make_session(org))

    async def _bound(_session: Any) -> None:
        order.append("bound")

    async def _update(*_a: Any, **_k: Any) -> Any:
        order.append("update")
        return run

    with (
        patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new=_bound),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=_update),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=run)),
    ):
        halted = await PipelineExecutor._check_spend_ceiling_gate(
            fake_self, run_id=run.id, org_id=org.id, claim_token=None
        )

    assert order == ["bound", "update"]
    assert halted is run


async def test_spend_ceiling_gate_lock_timeout_is_fail_open_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 55P03 on the terminalizing write lands in the method's existing
    fail-open ``except Exception`` (WARNING + return None) — the ledger block
    remains the authoritative hard ceiling."""
    org = _org(spend_ceiling_cents=0)
    run = MagicMock()
    run.id = uuid.uuid4()
    fake_self = _fake_executor(_make_session(org))

    with (
        patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(side_effect=_lock_timeout_error()),
        ),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=run)),
        caplog.at_level("ERROR", logger="modulo.core.pipeline_engine.executor"),
    ):
        halted = await PipelineExecutor._check_spend_ceiling_gate(
            fake_self, run_id=run.id, org_id=org.id, claim_token=None
        )

    assert halted is None
    assert any("spend_ceiling_gate_failed" in message for message in caplog.messages)


# ---------------------------------------------------------------------------
# Executor — ``_check_capacity`` (recovery path: skip + WARN + re-read)
# ---------------------------------------------------------------------------


def _capacity_executor(session: AsyncMock) -> PipelineExecutor:
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = MagicMock(side_effect=lambda: _acm(session))
    executor._read_org_sandbox_cap_full = AsyncMock(return_value=(None, False, None))  # type: ignore[method-assign]
    executor._read_org_run_concurrency_limit = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return executor


def _capacity_run(status: str = "running") -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.cancellation_requested = False
    return run


async def test_check_capacity_bounds_before_the_claim_time_write() -> None:
    order: list[str] = []
    run = _capacity_run()
    executor = _capacity_executor(_begin_session())

    async def _bound(_session: Any) -> None:
        order.append("bound")

    async def _update(*_a: Any, **_k: Any) -> Any:
        order.append("update")
        return run

    with (
        patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new=_bound),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=run)),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", new=AsyncMock(return_value=1)),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=_update),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=1,
            graph_json=None,
        )

    # The demote path (max_concurrent 1, one active run) issues the bound first.
    assert order == ["bound", "update"]
    assert result is run


async def test_check_capacity_lock_timeout_skips_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 55P03 on the claim-time write is a RECOVERY-path contention: the
    transaction rolled back whole, no status change applied, the run is left
    exactly as it was and the caller proceeds. WARNING, never silent."""
    run = _capacity_run()
    executor = _capacity_executor(_begin_session())

    with (
        patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=run)),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", new=AsyncMock(return_value=1)),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(side_effect=_lock_timeout_error()),
        ),
        caplog.at_level("WARNING", logger="modulo.core.pipeline_engine.executor"),
    ):
        result = await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=1,
            graph_json=None,
        )

    assert result is run
    assert any("capacity_check_lock_timeout" in message for message in caplog.messages)


async def test_check_capacity_non_lock_failure_still_propagates() -> None:
    """Only 55P03 is handled; any other failure keeps today's visible path."""
    run = _capacity_run()
    executor = _capacity_executor(_begin_session())

    with (
        patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=run)),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", new=AsyncMock(return_value=1)),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(side_effect=RuntimeError("connection reset")),
        ),
        pytest.raises(RuntimeError, match="connection reset"),
    ):
        await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=1,
            graph_json=None,
        )


async def test_check_capacity_missing_run_raises_not_found() -> None:
    """A missing run row is a genuine not-found, not a capacity decline."""
    executor = _capacity_executor(_begin_session())

    with (
        patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=None)),
        pytest.raises(RunNotFoundError),
    ):
        await executor._check_capacity(
            run_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=1,
            graph_json=None,
        )


async def test_check_capacity_cancelled_run_disappears_raises_not_found() -> None:
    """A cancellation-requested run whose re-read vanished is a not-found."""
    run = _capacity_run()
    run.cancellation_requested = True
    executor = _capacity_executor(_begin_session())

    with (
        patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(side_effect=[run, None])),
        pytest.raises(RunNotFoundError),
    ):
        await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=1,
            graph_json=None,
        )


async def test_check_capacity_declined_run_disappears_raises_not_found() -> None:
    """A demoted run whose pending re-read vanished is a not-found."""
    run = _capacity_run()
    executor = _capacity_executor(_begin_session())

    with (
        patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.count_active_runs_for_pipeline", new=AsyncMock(return_value=1)),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(side_effect=[run, None])),
        pytest.raises(RunNotFoundError),
    ):
        await executor._check_capacity(
            run_id=run.id,
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=1,
            graph_json=None,
        )


async def test_check_capacity_cancellation_propagates() -> None:
    """``CancelledError`` must never be swallowed by the 55P03 recovery arm."""
    executor = _capacity_executor(_begin_session())

    with (
        patch(
            "modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        pytest.raises(asyncio.CancelledError),
    ):
        await executor._check_capacity(
            run_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            max_concurrent=1,
            graph_json=None,
        )


# ---------------------------------------------------------------------------
# Executor — ``resume`` and the execute() policy-gate-pin transaction
# ---------------------------------------------------------------------------


async def test_resume_bounds_before_the_claim_write() -> None:
    order: list[str] = []
    fake_self = MagicMock()
    fake_self._session_factory = MagicMock(side_effect=lambda: _acm(_begin_session()))

    async def _bound(_session: Any) -> None:
        order.append("bound")

    async def _get_run(*_a: Any, **_k: Any) -> Any:
        order.append("get_run")
        return None

    from modulo.core.pipeline_engine.executor import RunNotFoundError

    with (
        patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new=_bound),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=_get_run),
        pytest.raises(RunNotFoundError),
    ):
        await PipelineExecutor.resume(
            fake_self,
            run_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            resume_data={},
        )

    assert order == ["bound", "get_run"]


async def test_execute_bounds_the_policy_gate_pin_transaction() -> None:
    order: list[str] = []
    fake_self = MagicMock()
    fake_self._session_factory = MagicMock(side_effect=lambda: _acm(_begin_session()))
    halted = MagicMock()

    async def _bound(_session: Any) -> None:
        order.append("bound")

    async def _pin(*_a: Any, **_k: Any) -> Any:
        order.append("pin")
        return halted

    fake_self._load_execution_context = AsyncMock(
        return_value=(MagicMock(), MagicMock(), MagicMock(), {}, {}),
    )
    fake_self._check_policy_gate_pin = AsyncMock(side_effect=_pin)

    with (
        patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new=_bound),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
    ):
        result = await PipelineExecutor.execute(
            fake_self,
            run_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            input_payload={},
        )

    assert order == ["bound", "pin"]
    assert result is halted


# ---------------------------------------------------------------------------
# HITL manager — the decide path and the claim path
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Minimal session double recording the ORDER of the bound vs statements.

    ``execute`` always returns an empty result so the decision UPDATE / gate
    read find no row and the method raises early (after the bound fired)."""

    def __init__(self, order: list[str]) -> None:
        self._order = order

    def get_bind(self) -> Any:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def execute(self, _stmt: Any, _params: dict[str, Any] | None = None) -> Any:
        self._order.append("execute")
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    async def get(self, *_a: Any, **_k: Any) -> Any:
        return None

    async def flush(self) -> None:
        return None


async def test_decide_bounds_before_the_decision_update() -> None:
    order: list[str] = []
    session = _RecordingSession(order)
    mgr = HITLManager()

    async def _bound(_session: Any) -> None:
        order.append("bound")

    with (
        patch("modulo.core.hitl_manager.set_mutation_row_lock_timeout", new=_bound),
        pytest.raises(GateNotFoundError),
    ):
        await mgr.approve(
            session,  # type: ignore[arg-type]
            run_id=uuid.uuid4(),
            review_id="gate-1",
            org_id=uuid.uuid4(),
            claim_token="opaque-token",
        )

    assert order[0] == "bound"
    assert "execute" in order


async def test_claim_bounds_before_the_first_lock() -> None:
    order: list[str] = []
    session = _RecordingSession(order)
    mgr = HITLManager()

    async def _bound(_session: Any) -> None:
        order.append("bound")

    with (
        patch("modulo.core.hitl_manager.set_mutation_row_lock_timeout", new=_bound),
        pytest.raises(GateNotFoundError),
    ):
        await mgr.claim(
            session,  # type: ignore[arg-type]
            run_id=uuid.uuid4(),
            review_id="gate-1",
            org_id=uuid.uuid4(),
            claimant_id=uuid.uuid4(),
        )

    assert order[0] == "bound"
    assert "execute" in order


# ---------------------------------------------------------------------------
# cost_controller.finalize — already bounded at the finalize_cost entry point
# ---------------------------------------------------------------------------


def test_finalize_cost_entry_point_issues_the_bound_first() -> None:
    """``finalize_cost`` already bounds its transaction at entry (FAR-1313 /
    FAR-1592), covering ``_write_finalized_run`` / ``_write_empty_terminal`` /
    ``_fallback_write`` / the ledger block; ``_reduced_escape`` bounds its own
    FRESH transaction. FAR-1610 confirms — no change needed."""
    import inspect

    from modulo.core.cost_controller import finalize

    source = inspect.getsource(finalize.finalize_cost)
    assert "set_mutation_row_lock_timeout(session)" in source
    reduced = inspect.getsource(finalize._reduced_escape)
    assert "set_mutation_row_lock_timeout(fresh)" in reduced
