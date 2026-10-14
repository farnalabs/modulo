"""Composed end-to-end regression for the ADR-043 autonomy chain (FAR-1783).

Each link of the chain is already unit-tested in isolation:

* ``create_snapshot_from_live_graph`` freezes the pipeline's
  ``default_autonomy_level`` onto the run snapshot
  (``tests/unit/db/test_pipeline_snapshot.py``);
* ``_seed_state`` copies that snapshot value into
  ``run_context["_pipeline_default_autonomy"]``
  (``tests/unit/pipeline_engine/test_executor.py``);
* the HITL gate resolver reads that run-context key and resolves the effective
  level (``tests/unit/core/run_context/test_autonomy.py``).

Nothing exercised the four sites AS ONE CHAIN, so a change that keeps each link
green on its own fixtures but breaks the seam between them would ship unnoticed.
These tests drive the REAL chain — the real snapshot function, the real seed
function and the real gate resolver — end to end, and assert the pipeline's
non-default ``default_autonomy_level`` survives every hop to the resolved
effective autonomy (plus the override case: a ``run_context``
``autonomy_recommendation`` outranks the snapshot default).

The only stand-in is the DATABASE SESSION (the unit suite has no Postgres); the
chain links themselves are never mocked. Reuses the house DB-session result
stubs (the same tiny builders re-declared across sibling DB unit tests) rather
than importing a production double.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.core.pipeline_engine.executor import _seed_state
from modulo.core.pipeline_engine.node_runner import _hitl_review_autonomy_result
from modulo.core.run_context.autonomy import AutonomyLevel
from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
from modulo.db.models.pipeline_snapshot import PipelineSnapshot

# ---------------------------------------------------------------------------
# DB-session stubs (house pattern — the session, never a chain link, is the
# only thing stubbed; the four production sites run for real).
# ---------------------------------------------------------------------------


def _scalar_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _scalars_result(values: list[object]) -> MagicMock:
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = values
    scalars_mock.__iter__.return_value = iter(values)
    result.scalars.return_value = scalars_mock
    return result


def _allocation_lock_result() -> MagicMock:
    """The (ignored) result of the snapshot-version allocation row-lock SELECT."""
    return MagicMock()


def _bind_lock_connection(session: AsyncMock, attempt: MagicMock) -> Any:
    """Install the stubbed dedicated lock engine the snapshot fn resolves.

    Mirrors the sibling ``test_pipeline_snapshot`` helper: the lock connection is
    drawn from a dedicated NullPool engine, so unit tests replace the resolver
    instead of building a real engine.
    """
    lock_conn = AsyncMock()
    lock_conn.execute.side_effect = [attempt, MagicMock()]
    engine = MagicMock(spec=AsyncEngine)
    engine.connect = AsyncMock(return_value=lock_conn)
    session.bind = engine
    return patch("modulo.db.crud.pipeline_snapshot._dedicated_lock_engine", return_value=engine)


def _lock_attempt_result(acquired: bool) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = acquired
    return result


def _make_pipeline(level: str | None, max_level: str | None = None) -> MagicMock:
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    pipeline = MagicMock()
    pipeline.id = uuid.uuid4()
    pipeline.organisation_id = uuid.uuid4()
    pipeline.graph_nodes_json = [
        {"id": str(source_id), "agent_id": None, "connector_binding": None},
        {"id": str(target_id), "agent_id": None, "connector_binding": None},
    ]
    pipeline.run_context_defaults = {"branch": "main"}
    pipeline.default_autonomy_level = level
    pipeline.max_autonomy_level = max_level
    return pipeline


def _make_edge(pipeline: MagicMock) -> MagicMock:
    nodes = pipeline.graph_nodes_json
    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = uuid.UUID(nodes[0]["id"])
    edge.target_node_id = uuid.UUID(nodes[1]["id"])
    edge.edge_type = "normal"
    edge.hitl_review_config = None
    edge.condition_expression = None
    return edge


async def _snapshot_from_pipeline(pipeline: MagicMock) -> PipelineSnapshot:
    """Drive the REAL ``create_snapshot_from_live_graph`` for *pipeline*."""
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        _scalar_result(pipeline),  # _load_pipeline_and_edges -> Pipeline
        _scalars_result([_make_edge(pipeline)]),  # _load_pipeline_and_edges -> edges
        _allocation_lock_result(),  # FAR-1625 allocation row lock (ignored)
        _scalar_result(1),  # snapshot_version max
        _scalars_result([]),  # guardrail rows (none bound)
        _scalars_result([]),  # policy gate rows (none bound)
    ]
    with _bind_lock_connection(session, _lock_attempt_result(True)):
        return await create_snapshot_from_live_graph(session, pipeline_id=pipeline.id)


# ---------------------------------------------------------------------------
# The composed chain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["notify_on_complete", "fully_autonomous"])
async def test_pipeline_default_flows_through_snapshot_seed_and_resolver(level: str) -> None:
    """A non-default pipeline ``default_autonomy_level`` reaches the resolved
    effective autonomy through the three real hops.

    Fails if ANY link breaks: the snapshot stops copying the default (hop 1
    asserts it), the executor stops seeding the run-context key (hop 2 asserts
    it, and a stale resolver would then fall back to ``manual_approval`` —
    which is not *level*), or the resolver's precedence changes (hop 3).
    """
    pipeline = _make_pipeline(level)

    # Hop 1: live pipeline -> run snapshot.
    snapshot = await _snapshot_from_pipeline(pipeline)
    assert snapshot.default_autonomy_level == level

    # Hop 2: snapshot -> executor-seeded run_context.
    state = _seed_state(snapshot, {})
    assert state["run_context"]["_pipeline_default_autonomy"] == level

    # Hop 3: run_context -> gate resolver -> effective autonomy.
    resolution, _artifact = _hitl_review_autonomy_result("gate-1", state, human_only=False)
    assert resolution.effective == AutonomyLevel(level)
    assert resolution.clamped is False


async def test_snapshot_default_none_flows_to_manual_approval_fallback() -> None:
    """The legacy NULL pipeline default keeps snapshotting NULL (hop 1), the
    executor seeds no key (hop 2), and the resolver falls back to
    ``manual_approval`` (hop 3) — the safe default, not an escalation."""
    pipeline = _make_pipeline(None)

    snapshot = await _snapshot_from_pipeline(pipeline)
    assert snapshot.default_autonomy_level is None

    state = _seed_state(snapshot, {})
    assert "_pipeline_default_autonomy" not in state["run_context"]

    resolution, artifact = _hitl_review_autonomy_result("gate-1", state, human_only=False)
    assert resolution.effective == AutonomyLevel.MANUAL_APPROVAL
    assert artifact is None


async def test_run_context_recommendation_outranks_snapshot_default() -> None:
    """A ``run_context`` ``autonomy_recommendation`` outranks the snapshot
    default once a context-setter has written it.

    The chain's first two hops run for real (``fully_autonomous`` is frozen and
    seeded); a context-setter then lowers the gate to ``manual_approval``. The
    resolver must follow the recommendation, not the snapshot default — if the
    precedence inverted, the effective level would stay ``fully_autonomous``.
    """
    pipeline = _make_pipeline("fully_autonomous")

    snapshot = await _snapshot_from_pipeline(pipeline)
    assert snapshot.default_autonomy_level == "fully_autonomous"

    state = _seed_state(snapshot, {})
    assert state["run_context"]["_pipeline_default_autonomy"] == "fully_autonomous"

    # A context-setter node writes the recommendation into run_context (the
    # gate resolves at run time, after context-setters have run).
    state["run_context"]["autonomy_recommendation"] = "manual_approval"

    resolution, artifact = _hitl_review_autonomy_result("gate-1", state, human_only=False)
    assert resolution.effective == AutonomyLevel.MANUAL_APPROVAL
    assert resolution.requested == AutonomyLevel.MANUAL_APPROVAL
    assert resolution.clamped is False
    assert artifact is None
