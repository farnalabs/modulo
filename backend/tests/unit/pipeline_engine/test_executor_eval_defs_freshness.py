"""FAR-502: eval definitions must be fresh on replay/resume.

``get_or_compile`` serves cached compiled graphs to replays / variant runs /
resumes (the only paths that reuse a snapshot_id across runs). The compiled
graph bakes eval definitions into HITL gate closures (eval-before-interrupt),
so a changed eval definition must (a) change the cache key — folded into
``graph_struct_hash`` — and (b) reach the compile factory; otherwise the
replay/resume silently runs the FIRST run's eval definitions. These tests pin
the executor wiring at both call sites: ``execute()`` (replay/variant path)
and ``resume()``.

Without the FAR-502 fix both tests fail: the struct hash is identical across
the eval change (cached closures reused / cold resume compiles gates with no
evals at all).
"""

import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine import EvalType
from modulo.core.pipeline_engine.executor import PipelineExecutor


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
    snap.graph_json = {
        "nodes": [
            {"id": "R", "node_type": "router", "router_config": {"rules": [{"guard": "state.x == `1`", "target": "A"}]}}
        ],
        "edges": [],
    }
    snap.run_context_defaults = {}
    return snap


def _make_session(snapshot: MagicMock, result_order: str = "execute") -> tuple[AsyncMock, Callable[[], None]]:
    """Session mock returning scripted query results, resettable per executor call.

    Returns ``(session, reset)`` — call ``reset()`` before each executor
    invocation so both runs see the same scripted sequence (otherwise the
    second run reads raw MagicMocks and hashes a phantom empty graph).

    ``result_order="execute"``: pipeline load first, then snapshot.
    ``result_order="resume"``: snapshot load first, then pipeline.
    Third-and-later queries return a scalars-shaped result (scalar()=0,
    scalars().all()=[]).
    """
    pipeline = MagicMock()
    pipeline.max_concurrent_runs = 5
    pipeline.lock_wait_timeout_seconds = 30
    pipeline.max_duration_seconds = 3600
    pipeline.max_steps = 100
    pipeline.token_budget = None

    pipeline_result = MagicMock()
    pipeline_result.scalar_one.return_value = pipeline
    pipeline_result.scalar.return_value = 0
    snapshot_result = MagicMock()
    snapshot_result.scalar_one.return_value = snapshot
    snapshot_result.scalar.return_value = 0
    scalars_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    scalars_result.scalars.return_value = scalars_mock
    scalars_result.scalar.return_value = 0

    first, second = (
        (pipeline_result, snapshot_result) if result_order == "execute" else (snapshot_result, pipeline_result)
    )
    state = {"index": 0, "fresh": True}

    async def _execute(*_args: Any, **_kwargs: Any) -> Any:
        if state["fresh"]:
            state["index"] = 0
            state["fresh"] = False
        try:
            result = (first, second, scalars_result)[state["index"]]
        except IndexError:
            result = scalars_result
        state["index"] += 1
        return result

    def _reset() -> None:
        state["fresh"] = True

    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.execute = _execute
    return session, _reset


def _make_session_factory(session: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def _ctx():
        yield session

    return MagicMock(side_effect=lambda: _ctx())


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


def _mock_compiled_empty_stream() -> MagicMock:
    """Compiled-graph stand-in: empty event stream + resume-capable aupdate_state."""

    async def _astream(*_args: Any, **_kwargs: Any) -> Any:
        return
        yield  # pragma: no cover

    c = MagicMock()
    c.astream_events = _astream
    c.aupdate_state = AsyncMock()
    return c


async def _bypass_capacity(mock_self, **kwargs):
    run = MagicMock()
    run.status = "running"
    return run


def _eval_row(config: dict[str, Any], eval_id: uuid.UUID | None = None) -> SimpleNamespace:
    """ORM-shaped eval-definition row (what ``_load_eval_defs_for_pipeline`` returns)."""
    return SimpleNamespace(
        id=eval_id or uuid.uuid4(),
        node_id="A",
        pipeline_id=uuid.uuid4(),
        name="gate-eval",
        eval_type=EvalType.REGEX,
        config_json=config,
        failure_behaviour="warn",
        pass_threshold=None,
        suite_id=None,
        version=1,
    )


def _canonical(defs: dict[str, list[Any]]) -> dict[str, list[dict[str, Any]]]:
    """created_at is stamped per DTO construction — compare without it."""
    return {node: [d.model_dump(mode="json", exclude={"created_at"}) for d in lst] for node, lst in defs.items()}


def _assert_fresh_eval_defs_reach_compile(
    struct_hashes: list[str],
    build_eval_defs: list[Any],
    defs_e1: dict[str, list[Any]],
    defs_e2: dict[str, list[Any]],
) -> None:
    assert len(struct_hashes) == 2
    # THE FIX: changed eval definitions must change the compile-cache key, so a
    # replay/resume misses the cache instead of reusing the first run's gate
    # closures (which captured the old eval definitions).
    assert struct_hashes[0] != struct_hashes[1]
    # The freshly loaded definitions reach the compile factory on every compile.
    assert _canonical(build_eval_defs[0]) == _canonical(defs_e1)
    assert _canonical(build_eval_defs[1]) == _canonical(defs_e2)


async def test_execute_replay_recompiles_with_fresh_eval_defs():
    """Replaying the same snapshot with CHANGED eval definitions recompiles the
    graph with the new definitions (FAR-502 execute/replay path)."""
    run = _make_run()
    snapshot = _make_snapshot()
    session, reset_script = _make_session(snapshot, result_order="execute")
    factory = _make_session_factory(session)

    org_id = uuid.uuid4()
    row1 = _eval_row({"pattern": "v1"})
    row2 = _eval_row({"pattern": "v2"})
    # Expected compiled-in defs = what the real builder produces from the rows.
    defs_e1 = PipelineExecutor._build_eval_defs_by_node([row1], org_id, run.pipeline_id)
    defs_e2 = PipelineExecutor._build_eval_defs_by_node([row2], org_id, run.pipeline_id)

    struct_hashes: list[str] = []
    build_eval_defs: list[Any] = []

    def _spy_get_or_compile(pipeline_id, snapshot_id, compile_factory, **kwargs):
        struct_hashes.append(kwargs["graph_struct_hash"])
        return compile_factory()

    def _capture_build(graph_json, **kwargs):
        build_eval_defs.append(kwargs.get("eval_definitions_by_node"))
        return _mock_compiled_empty_stream()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", side_effect=_spy_get_or_compile),
        patch("modulo.core.pipeline_engine.executor.build_graph_from_json", side_effect=_capture_build),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=_mock_registry()),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        patch.object(
            PipelineExecutor,
            "_load_eval_defs_for_pipeline",
            AsyncMock(side_effect=[[row1], [row2]]),
        ),
        patch("modulo.settings.get_settings", return_value=MagicMock(saq_run_retries=5)),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=org_id, input_payload={}, claim_token="tok-claim-abc")
        reset_script()
        await executor.execute(run_id=run.id, org_id=org_id, input_payload={}, claim_token="tok-claim-abc")

    _assert_fresh_eval_defs_reach_compile(struct_hashes, build_eval_defs, defs_e1, defs_e2)


async def test_resume_recompiles_with_fresh_eval_defs():
    """Resuming the same snapshot with CHANGED eval definitions recompiles the
    graph with the new definitions (FAR-502 resume path — the factory must also
    receive the definitions; previously it passed none at all)."""
    run = _make_run()
    snapshot = _make_snapshot()
    session, reset_script = _make_session(snapshot, result_order="resume")
    factory = _make_session_factory(session)

    org_id = uuid.uuid4()
    row1 = _eval_row({"pattern": "v1"})
    row2 = _eval_row({"pattern": "v2"})
    defs_e1 = PipelineExecutor._build_eval_defs_by_node([row1], org_id, run.pipeline_id)
    defs_e2 = PipelineExecutor._build_eval_defs_by_node([row2], org_id, run.pipeline_id)

    struct_hashes: list[str] = []
    build_eval_defs: list[Any] = []

    def _spy_get_or_compile(pipeline_id, snapshot_id, compile_factory, **kwargs):
        struct_hashes.append(kwargs["graph_struct_hash"])
        return compile_factory()

    def _capture_build(graph_json, **kwargs):
        build_eval_defs.append(kwargs.get("eval_definitions_by_node"))
        return _mock_compiled_empty_stream()

    @asynccontextmanager
    async def _fake_checkpointer_scope(_conn_string: str, **_kwargs: Any):
        yield MagicMock()

    with (
        patch("modulo.core.pipeline_engine.executor.async_sessionmaker", return_value=factory),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope", _fake_checkpointer_scope),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", side_effect=_spy_get_or_compile),
        patch("modulo.core.pipeline_engine.executor.build_graph_from_json", side_effect=_capture_build),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=_mock_registry()),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_enforce_resume_sandbox_capacity", AsyncMock()),
        patch.object(
            PipelineExecutor,
            "_load_eval_defs_for_pipeline",
            AsyncMock(side_effect=[[row1], [row2]]),
        ),
        patch("modulo.settings.get_settings", return_value=MagicMock(saq_run_retries=5)),
    ):
        executor = PipelineExecutor(MagicMock(), checkpointer_conn_string="postgresql://fake-checkpointer")
        await executor.resume(run_id=run.id, org_id=org_id, resume_data={"action": "approved"}, claim_token="tok")
        reset_script()
        await executor.resume(run_id=run.id, org_id=org_id, resume_data={"action": "approved"}, claim_token="tok")

    _assert_fresh_eval_defs_reach_compile(struct_hashes, build_eval_defs, defs_e1, defs_e2)
