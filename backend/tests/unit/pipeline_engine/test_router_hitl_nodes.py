"""Unit tests for FAR-402 P1 Router + HITL node ergonomics (FAR-415).

Covers: the shared JMESPath evaluator, ``make_router_node_fn`` (rule
evaluation, default, no-match -> RouterNoMatchError, classifier mode),
Router compile-time default-rule enforcement, HITL-node compile-equivalence
with the legacy edge-gate, and ``loop`` edge authorability.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.errors import RouterNoMatchError
from modulo.core.pipeline_engine.graph_cache import (
    RouterConfigError,
    _validate_router_config,
    build_graph_from_json,
)
from modulo.core.pipeline_engine.jmespath_eval import (
    compile_jmespath,
    evaluate_jmespath_condition,
)
from modulo.core.pipeline_engine.node_runner import make_router_node_fn

# ---------------------------------------------------------------------------
# Shared JMESPath evaluator
# ---------------------------------------------------------------------------


def test_evaluate_jmespath_condition_truthiness():
    state = {"foo": {"bar": 1}, "list": [1, 2]}
    assert evaluate_jmespath_condition(state, "foo.bar == `1`") is True
    assert evaluate_jmespath_condition(state, "foo.bar == `2`") is False
    # Empty/None expr is falsy (no guard).
    assert evaluate_jmespath_condition(state, "") is False
    assert evaluate_jmespath_condition(state, None) is False


def test_evaluate_jmespath_condition_invalid_raises():
    with pytest.raises(ValueError, match="Invalid JMESPath expression"):
        compile_jmespath("foo.++invalid")


def test_evaluate_jmespath_condition_list_bool():
    # bool(...) truthiness (mirrors prior inline sites).
    assert evaluate_jmespath_condition({"x": [1]}, "x") is True
    assert evaluate_jmespath_condition({"x": []}, "x") is False


# ---------------------------------------------------------------------------
# make_router_node_fn
# ---------------------------------------------------------------------------


def _router_fn(rules, mode=None):
    config = {"rules": rules}
    if mode is not None:
        config["mode"] = mode
    return make_router_node_fn(config, node_id="r1")


def test_router_first_match_wins():
    fn = _router_fn(
        [
            {"guard": "state.x == `1`", "target": "a"},
            {"guard": "state.x == `2`", "target": "b"},
            {"default": True, "target": "c"},
        ]
    )
    assert fn({"state": {"x": 2}}) == "b"
    assert fn({"state": {"x": 1}}) == "a"


def test_router_default_used_when_no_match():
    fn = _router_fn(
        [
            {"guard": "state.x == `1`", "target": "a"},
            {"default": True, "target": "c"},
        ]
    )
    assert fn({"state": {"x": 99}}) == "c"


def test_router_no_match_raises():
    # A Router with no default and no matching rule raises at routing time.
    fn = make_router_node_fn({"rules": [{"guard": "state.x == `1`", "target": "a"}]}, node_id="r2")
    with pytest.raises(RouterNoMatchError):
        fn({"state": {"x": 99}})


def test_router_classifier_mode_matches_label():
    fn = _router_fn(
        [
            {"guard": "state.x == `1`", "target": "a"},
            {"label": "go_b", "target": "b"},
            {"default": True, "target": "c"},
        ],
        mode="classifier",
    )
    assert fn({"state": {"x": 0}, "_llm_next_node": "go_b"}) == "b"


def test_router_no_label_key_falls_to_default():
    fn = make_router_node_fn(
        {"rules": [{"label": "go_b", "target": "b"}, {"default": True, "target": "c"}]},
        node_id="rc2",
    )
    assert fn({}) == "c"


# ---------------------------------------------------------------------------
# Compile-time default-rule enforcement
# ---------------------------------------------------------------------------


def test_validate_router_config_requires_default():
    with pytest.raises(RouterConfigError):
        _validate_router_config({"rules": [{"guard": "x", "target": "a"}]}, "n1")
    # classifier mode is exempt (label match or default).
    _validate_router_config({"mode": "classifier", "rules": [{"label": "l", "target": "a"}]}, "n2")
    # default present is fine.
    _validate_router_config({"rules": [{"default": True, "target": "a"}]}, "n3")


# ---------------------------------------------------------------------------
# HITL node compile-equivalence with legacy edge-gate
# ---------------------------------------------------------------------------


def _compiled_structure(graph_json):
    with (
        patch("modulo.core.pipeline_engine.graph_cache.make_node_fn", MagicMock()),
        patch("modulo.core.pipeline_engine.graph_cache.make_manual_node_fn", MagicMock()),
        patch("modulo.core.pipeline_engine.graph_cache.make_hitl_gate_fn", MagicMock()),
    ):
        compiled = build_graph_from_json(graph_json)
    g = compiled.get_graph()
    nodes = {n for n in g.nodes if n not in ("__start__", "__end__")}
    edges = {(s, t) for s, t, *_ in g.edges if s not in ("__start__", "__end__") and t not in ("__start__", "__end__")}
    gate_nodes = {n for n in nodes if "hitl_gate" in str(n)}
    return nodes, edges, gate_nodes


def test_hitl_node_compiles_like_edge_gate():
    hitl_config = {"required_team_id": "team-1", "human_only": True}
    # Legacy: an agent node A with an HITL-gated edge to B.
    legacy = {
        "nodes": [{"id": "A", "node_type": "agent"}, {"id": "B", "node_type": "agent"}],
        "edges": [{"source": "A", "target": "B", "hitl_gate_config": dict(hitl_config)}],
    }
    # New: an `hitl` node H (producing output) with a normal edge to B.
    new = {
        "nodes": [
            {"id": "H", "node_type": "hitl", "hitl_config": dict(hitl_config)},
            {"id": "B", "node_type": "agent"},
        ],
        "edges": [{"source": "H", "target": "B"}],
    }
    legacy_nodes, legacy_edges, legacy_gates = _compiled_structure(legacy)
    new_nodes, new_edges, new_gates = _compiled_structure(new)

    # Both insert exactly one synthetic HITL gate node and route
    # source -> gate -> target.
    assert len(legacy_gates) == 1
    assert len(new_gates) == 1
    assert len(legacy_nodes) == 3
    assert len(new_nodes) == 3
    # The gate sits between the source and B in both.
    assert "B" in legacy_nodes
    assert "B" in new_nodes
    assert all("B" in t for (_, t) in legacy_edges)
    assert all("B" in t for (_, t) in new_edges)
    # Same number of edges (source->gate, gate->target).
    assert len(legacy_edges) == len(new_edges)
    assert len(new_edges) == 2


# ---------------------------------------------------------------------------
# loop edge authorability
# ---------------------------------------------------------------------------


def test_loop_edge_authorable_in_valid_set():
    from modulo.core.workflow_import_export import VALID_EDGE_TYPES

    assert "loop" in VALID_EDGE_TYPES


def test_loop_edge_compiles():
    graph_json = {
        "nodes": [{"id": "A", "node_type": "agent"}, {"id": "B", "node_type": "agent"}],
        "edges": [
            {
                "source": "A",
                "target": "B",
                "type": "loop",
                "default_target": "B",
                "max_iterations": 3,
            }
        ],
    }
    nodes, _, _ = _compiled_structure(graph_json)
    assert "A" in nodes
    assert "B" in nodes
    # A loop counter synthetic node is inserted.
    assert any("loop_counter" in str(n) for n in nodes)


def test_router_node_compiles():
    graph_json = {
        "nodes": [
            {
                "id": "R",
                "node_type": "router",
                "router_config": {
                    "rules": [
                        {"guard": "state.x == `1`", "target": "A"},
                        {"default": True, "target": "B"},
                    ]
                },
            },
            {"id": "A", "node_type": "agent"},
            {"id": "B", "node_type": "agent"},
        ],
        "edges": [{"source": "R", "target": "A"}, {"source": "R", "target": "B"}],
    }
    nodes, _, _ = _compiled_structure(graph_json)
    assert "R" in nodes
    assert "A" in nodes
    assert "B" in nodes


def test_router_rule_targets_excluded_from_entry_point():
    # Regression guard for the wrong-entry-node bug: a Router's rule targets
    # must be registered as graph targets so the entry-point selection cannot
    # pick one as the pipeline entry. Here the nodes array puts a rule target
    # (A) first, the real entry (S) last, and the router (R) in between.
    graph_json = {
        "nodes": [
            {"id": "A", "node_type": "agent"},
            {"id": "B", "node_type": "agent"},
            {"id": "S", "node_type": "agent"},
            {
                "id": "R",
                "node_type": "router",
                "router_config": {
                    "rules": [
                        {"guard": "state.x == `1`", "target": "A"},
                        {"default": True, "target": "B"},
                    ]
                },
            },
        ],
        "edges": [{"source": "S", "target": "R"}],
    }
    with (
        patch("modulo.core.pipeline_engine.graph_cache.make_node_fn", MagicMock()),
        patch("modulo.core.pipeline_engine.graph_cache.make_manual_node_fn", MagicMock()),
        patch("modulo.core.pipeline_engine.graph_cache.make_hitl_gate_fn", MagicMock()),
    ):
        compiled = build_graph_from_json(graph_json)
    graph = compiled.get_graph()
    entry_nodes = [e.target for e in graph.edges if e.source == "__start__"]
    # Without the fix the entry becomes "A" (a router rule target) and the real
    # entry S + router R are dead.
    assert entry_nodes == ["S"]


# ---------------------------------------------------------------------------
# Executor terminalization: RouterNoMatchError -> router_no_match (FAR-415)
# ---------------------------------------------------------------------------

import uuid  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from typing import Any  # noqa: E402

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from modulo.core.pipeline_engine.executor import PipelineExecutor  # noqa: E402


def _make_run(*, claim_token: str | None = "tok-claim-abc") -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.pipeline_id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
    run.langgraph_thread_id = str(uuid.uuid4())
    run.status = "pending"
    run.claim_count = 0
    run.node_attempt_count = 0
    run.claim_token = claim_token
    return run


def _make_snapshot() -> MagicMock:
    snap = MagicMock()
    router_config = {"rules": [{"guard": "state.x == `1`", "target": "A"}]}
    snap.graph_json = {"nodes": [{"id": "R", "node_type": "router", "router_config": router_config}], "edges": []}
    snap.run_context_defaults = {}
    return snap


def _make_session(snapshot: MagicMock, statements: list[str] | None = None) -> AsyncMock:
    pipeline = MagicMock()
    pipeline.max_concurrent_runs = 5
    pipeline.lock_wait_timeout_seconds = 30
    pipeline.max_duration_seconds = 3600
    pipeline.max_steps = 100
    pipeline.token_budget = None

    pipeline_result = MagicMock()
    pipeline_result.scalar_one.return_value = pipeline
    snapshot_result = MagicMock()
    snapshot_result.scalar_one.return_value = snapshot
    eval_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    eval_result.scalars.return_value = scalars_mock
    count_result = MagicMock()
    count_result.scalar.return_value = 0

    execute_results = iter([pipeline_result, snapshot_result, eval_result, count_result])
    recorded = statements if statements is not None else []

    async def _execute(*_args: Any, **_kwargs: Any) -> Any:
        recorded.append(str(_args[0]) if _args else "")
        try:
            return next(execute_results)
        except StopIteration:
            return count_result

    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.execute = _execute
    return session


def _make_session_factory(session: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def _ctx():
        yield session

    return MagicMock(side_effect=lambda: _ctx())


def _mock_compiled_raising(exc: Exception) -> MagicMock:
    async def _astream(state: Any, config: Any, *, version: str = "v1") -> Any:
        raise exc
        yield  # pragma: no cover

    c = MagicMock()
    c.astream_events = _astream
    return c


def _mock_registry() -> MagicMock:
    broker = MagicMock()
    broker.publish = MagicMock()
    broker.is_closed = False
    registry = MagicMock()
    registry.get_or_create.return_value = broker
    registry.close = MagicMock()
    return registry


def _mock_graph_validator() -> MagicMock:
    validation = MagicMock()
    validation.is_valid = True
    mock_cls = MagicMock()
    mock_cls.return_value.validate_for_run = AsyncMock(return_value=validation)
    return mock_cls


async def _bypass_capacity(mock_self, **kwargs):
    run = MagicMock()
    run.status = "running"
    return run


async def test_execute_router_no_match_terminalizes_as_router_no_match():
    """Prove-the-fix (FAR-415): a Router node raising RouterNoMatchError must be
    caught by the executor's dedicated except and terminalize the run with the
    ``router_no_match`` status (error_code ``router.no_match``) — NOT bubble up
    as an unclassified ``failed``. This is the wiring that the unit-only
    ``test_router_no_match_raises`` test never exercised."""
    run = _make_run()
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(RouterNoMatchError(node_id="R"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        # Must NOT raise — the exception is terminalized, not propagated.
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    # The terminal status write carries the dedicated router_no_match status.
    mock_finalize.assert_awaited_once()
    call_kwargs = mock_finalize.await_args.kwargs
    assert call_kwargs["status"] == "router_no_match"
    assert call_kwargs["error_code"] == "router.no_match"
    assert call_kwargs["is_terminal"] is True
    # Cleanup ran so the broker is released.
    registry.close.assert_called_once_with(run.id)


async def test_execute_router_no_match_not_classed_as_failed():
    """Guard against a regression where a generic ``except Exception`` would
    shadow the RouterNoMatchError handler and re-terminalize the run as
    ``failed``. The run must end under ``router_no_match``, never ``failed``."""
    run = _make_run()
    snapshot = _make_snapshot()
    session = _make_session(snapshot)
    factory = _make_session_factory(session)
    compiled = _mock_compiled_raising(RouterNoMatchError(node_id="R"))
    registry = _mock_registry()
    settings = MagicMock(saq_run_retries=5)

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()) as mock_finalize,
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", return_value=compiled),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch("modulo.settings.get_settings", return_value=settings),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={}, claim_token="tok-claim-abc")

    assert mock_finalize.await_args.kwargs["status"] != "failed"
    assert mock_finalize.await_args.kwargs["status"] == "router_no_match"


# ---------------------------------------------------------------------------
# DB-layer acceptance: router_no_match must be accepted by the status CHECK
# constraint, the persistence whitelist, and the shared terminal set — otherwise
# the executor's terminal write (the path above mocks it) raises ValueError at
# runtime, the run is never recorded, and the regression is invisible to CI.
# Mirrors the establish pattern in test_executor_stalled_status.py.
# ---------------------------------------------------------------------------

from sqlalchemy import CheckConstraint, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from modulo.db.crud.run import RUN_STATUS_WHITELIST, update_run_status  # noqa: E402
from modulo.db.models.base import Base  # noqa: E402
from modulo.db.models.run import TERMINAL_STATUSES, Run  # noqa: E402


def test_run_model_check_constraint_allows_router_no_match():
    table_args = Run.__table_args__
    check_sql = " ".join(
        arg.sqltext.text for arg in table_args if isinstance(arg, CheckConstraint) and arg.name == "ck_runs_status"
    )
    assert "'router_no_match'" in check_sql


def test_run_status_whitelist_includes_router_no_match():
    assert "router_no_match" in RUN_STATUS_WHITELIST


def test_terminal_statuses_include_router_no_match():
    assert "router_no_match" in TERMINAL_STATUSES


@pytest.fixture
async def _sqlite_runs_engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[Run.__table__]))
    yield eng
    await eng.dispose()


async def test_update_run_status_persists_router_no_match_with_completed_at(_sqlite_runs_engine):
    """Persistence-layer coverage: update_run_status accepts 'router_no_match'
    and stamps completed_at on the real row — the end-to-end write a no-match
    run goes through in finalize_cost. Without the whitelist + completed_at
    wiring this raises ValueError or leaves completed_at NULL (the executor-only
    test above never reaches this layer)."""
    factory = async_sessionmaker(_sqlite_runs_engine, expire_on_commit=False)
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(
            Run(
                id=run_id,
                organisation_id=org_id,
                pipeline_id=uuid.uuid4(),
                snapshot_id=uuid.uuid4(),
                trigger_type="manual",
                status="running",
                run_number=1,
                input_hash="a" * 64,
                langgraph_thread_id=f"thread-{run_id}",
            )
        )
        await session.flush()

    async with factory() as session, session.begin():
        run = await update_run_status(session, run_id, "router_no_match")
        assert run is not None
        assert run.status == "router_no_match"
        assert run.completed_at is not None

    async with factory() as session:
        persisted = await session.execute(select(Run).where(Run.id == run_id))
        row = persisted.scalar_one_or_none()
    assert row is not None
    assert row.status == "router_no_match"
    assert row.completed_at is not None


async def test_update_run_status_router_no_match_in_transition_terminal_set(_sqlite_runs_engine):
    """Prove-the-fix (FAR-415): a router_no_match run is terminal and accepted by
    the fenced UPDATE / transition SQL terminal set, so the executor's terminal
    write is not rejected by the status guard. Exercises the real persistence
    path the executor funnels into (finalize_cost -> update_run_status)."""
    factory = async_sessionmaker(_sqlite_runs_engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(
            Run(
                id=run_id,
                organisation_id=uuid.uuid4(),
                pipeline_id=uuid.uuid4(),
                snapshot_id=uuid.uuid4(),
                trigger_type="manual",
                status="running",
                run_number=1,
                input_hash="a" * 64,
                langgraph_thread_id=f"thread-{run_id}",
            )
        )
        await session.flush()

    async with factory() as session, session.begin():
        # Idempotent re-terminalize must be accepted (terminal -> terminal).
        run = await update_run_status(session, run_id, "router_no_match")
        assert run.status == "router_no_match"
        run2 = await update_run_status(session, run_id, "router_no_match")
        assert run2.status == "router_no_match"
