"""FAR-505 — the resume path's compile must mirror the execute path's wiring.

The graph cache is keyed by ``(pipeline_id, snapshot_id, node_timeout,
struct_hash)``. ``execute()`` folds the pipeline retry policy into the struct
hash (``compute_retry_aware_topology_hash``) and threads
``pipeline_retry_policy`` + ``node_idempotency_key`` into the compile factory
(FAR-402 P5). ``resume()`` must do exactly the same — otherwise a cold compile
during resume produces an unwired graph AND a second, divergent cache entry for
the same snapshot whenever a retry policy exists.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.pipeline_engine import graph_cache as graph_cache_module
from modulo.core.pipeline_engine.eval_persist_order import EvalDefDTO
from modulo.core.pipeline_engine.executor import (
    _GATED_EVAL_MODES_KEY,
    PipelineExecutor,
    compute_retry_aware_topology_hash,
)

_RETRY_POLICY: dict[str, Any] = {"on": ["stall"], "max_retries": 2}
_GRAPH_JSON: dict[str, Any] = {"nodes": [{"id": "node-a", "role": None}], "edges": []}


def _make_run(
    *,
    run_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    snapshot_id: uuid.UUID | None = None,
    status: str = "pending",
) -> MagicMock:
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.pipeline_id = pipeline_id or uuid.uuid4()
    run.snapshot_id = snapshot_id or uuid.uuid4()
    run.langgraph_thread_id = str(uuid.uuid4())
    run.status = status
    run.run_number = 1
    return run


def _make_pipeline(retry_policy: dict[str, Any]) -> MagicMock:
    pipeline = MagicMock()
    pipeline.max_concurrent_runs = 5
    pipeline.lock_wait_timeout_seconds = 30
    pipeline.max_duration_seconds = 3600
    pipeline.max_steps = 100
    pipeline.token_budget = None
    pipeline.node_timeout_seconds = 300
    pipeline.retry_policy = retry_policy
    return pipeline


def _make_snapshot() -> MagicMock:
    snap = MagicMock()
    snap.graph_json = _GRAPH_JSON
    snap.run_context_defaults = {"context_key": "context_val"}
    return snap


def _make_session_factory(session: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def _ctx():
        yield session

    return MagicMock(side_effect=lambda: _ctx())


def _make_execute_session(snapshot: MagicMock, pipeline: MagicMock) -> AsyncMock:
    """Session mock whose execute() order matches execute()'s query sequence."""
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

    async def _execute(*_args: Any, **_kwargs: Any) -> Any:
        try:
            return next(execute_results)
        except StopIteration:
            return count_result

    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    session.add = MagicMock()
    session.execute = _execute
    return session


def _make_resume_session(
    snapshot: MagicMock,
    pipeline: MagicMock,
    *,
    eval_rows: list[Any] | None = None,
    stamp_configs: list[Any] | None = None,
) -> AsyncMock:
    """Session mock whose execute() order matches resume()'s query sequence."""
    pipeline_result = MagicMock()
    pipeline_result.scalar_one_or_none.return_value = pipeline

    snapshot_result = MagicMock()
    snapshot_result.scalar_one.return_value = snapshot

    graph_json_result = MagicMock()
    graph_json_result.scalar_one_or_none.return_value = snapshot.graph_json

    eval_result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = list(eval_rows or [])
    eval_result.scalars.return_value = scalars_mock
    # _load_eval_defs_for_pipeline reads the join rows via Result.all().
    eval_result.all.return_value = list(eval_rows or [])

    # FAR-939 M4: the fire-time evaluation-mode stamp read (one
    # gate_config_json select per fired claim) sits between the eval load and
    # the second session block.
    claim_stamp_result = MagicMock()
    claim_stamp_result.scalars.return_value = list(stamp_configs or [])

    count_result = MagicMock()
    count_result.scalar.return_value = 0

    # True resume() query order: sandbox-capacity graph_json probe, snapshot
    # select, eval-definitions load, claim evaluation-mode stamp read (first
    # session block), THEN the pipeline select (second session block).
    execute_results = iter(
        [graph_json_result, snapshot_result, eval_result, claim_stamp_result, pipeline_result, count_result]
    )

    async def _execute(*_args: Any, **_kwargs: Any) -> Any:
        try:
            return next(execute_results)
        except StopIteration:
            return count_result

    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = _execute
    return session


def _mock_graph_validator() -> MagicMock:
    validation = MagicMock()
    validation.is_valid = True
    mock_cls = MagicMock()
    mock_cls.return_value.validate_for_run = AsyncMock(return_value=validation)
    return mock_cls


def _mock_compiled() -> MagicMock:
    async def _astream(state: Any, config: Any, *, version: str = "v1") -> Any:
        yield {
            "event": "on_chain_end",
            "name": "node-a",
            "data": {"output": {"output": {"status": "completed", "cost_estimate_usd": 0.5}}},
        }

    c = MagicMock()
    c.astream_events = _astream
    c.aupdate_state = AsyncMock()
    return c


def _mock_registry() -> MagicMock:
    broker = MagicMock()
    broker.publish = MagicMock()
    broker.is_closed = False
    registry = MagicMock()
    registry.get_or_create.return_value = broker
    registry.close = MagicMock()
    return registry


async def _bypass_capacity(mock_self: Any, **kwargs: Any) -> MagicMock:
    run = MagicMock()
    run.status = "running"
    return run


def _recording_compile_mocks() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    MagicMock,
    MagicMock,
]:
    """Build mocks that record (a) get_or_compile kwargs and (b) the kwargs the
    compile factory passes to build_graph_from_json."""
    builds: list[dict[str, Any]] = []
    compiles: list[dict[str, Any]] = []

    def _fake_build(graph_json: dict[str, Any], **kwargs: Any) -> Any:
        builds.append({"graph_json": graph_json, **kwargs})
        return _mock_compiled()

    def _fake_get_or_compile(pipeline_id: Any, snapshot_id: Any, factory_fn: Any, **kwargs: Any) -> Any:
        compiles.append({"pipeline_id": pipeline_id, "snapshot_id": snapshot_id, **kwargs})
        return factory_fn()

    return builds, compiles, MagicMock(side_effect=_fake_build), MagicMock(side_effect=_fake_get_or_compile)


async def test_resume_compile_wiring_matches_execute_path():
    """resume() must fold the retry policy into the struct hash and thread the
    retry policy + idempotency key into the compile factory, exactly as the
    execute path does (FAR-505)."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    pipeline = _make_pipeline(dict(_RETRY_POLICY))
    registry = _mock_registry()

    # --- execute leg (reference wiring) ---
    ex_builds, ex_compiles, ex_build_mock, ex_goc_mock = _recording_compile_mocks()
    with (
        patch(
            "modulo.core.pipeline_engine.executor.async_sessionmaker",
            return_value=_make_session_factory(_make_execute_session(snapshot, pipeline)),
        ),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", side_effect=ex_goc_mock),
        patch("modulo.core.pipeline_engine.executor.build_graph_from_json", side_effect=ex_build_mock),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
    ):
        executor = PipelineExecutor(MagicMock())
        await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

    assert len(ex_compiles) == 1
    assert len(ex_builds) == 1
    execute_compile, execute_build = ex_compiles[0], ex_builds[0]

    # --- resume leg ---
    rs_builds, rs_compiles, rs_build_mock, rs_goc_mock = _recording_compile_mocks()
    checkpointer_mock = MagicMock()
    checkpointer_mock.__aenter__ = AsyncMock(return_value=checkpointer_mock)
    checkpointer_mock.__aexit__ = AsyncMock(return_value=False)
    settings_mock = MagicMock()
    settings_mock.fernet_key = "test-fernet-key-not-for-production="
    with (
        patch(
            "modulo.core.pipeline_engine.executor.async_sessionmaker",
            return_value=_make_session_factory(_make_resume_session(snapshot, pipeline)),
        ),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", side_effect=rs_goc_mock),
        patch("modulo.core.pipeline_engine.executor.build_graph_from_json", side_effect=rs_build_mock),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope", return_value=checkpointer_mock),
        patch("modulo.settings.get_settings", return_value=settings_mock),
        patch("modulo.core.pipeline_engine.executor.RunawayGuard", return_value=MagicMock()),
    ):
        executor = PipelineExecutor(MagicMock())
        executor._checkpointer_conn_string = "sqlite:///test.db"
        await executor.resume(run_id=run.id, org_id=uuid.uuid4(), resume_data={"action": "approved"})

    assert len(rs_compiles) == 1
    assert len(rs_builds) == 1
    resume_compile, resume_build = rs_compiles[0], rs_builds[0]

    # Both paths use the retry-aware struct hash (retry policy folded in).
    expected_hash = compute_retry_aware_topology_hash(_GRAPH_JSON, _RETRY_POLICY)
    assert execute_compile["graph_struct_hash"] == expected_hash
    assert resume_compile["graph_struct_hash"] == expected_hash

    # Both factories wire the retry policy and a callable idempotency key.
    for build in (execute_build, resume_build):
        assert build["pipeline_retry_policy"] == _RETRY_POLICY
        assert callable(build["node_idempotency_key"])

    # The same stable run identity (<pipeline_id>:<run_number>) means the
    # execute path and the resume path derive the SAME node idempotency key.
    execute_key = execute_build["node_idempotency_key"]("node-a", {})
    resume_key = resume_build["node_idempotency_key"]("node-a", {})
    assert isinstance(execute_key, str)
    assert execute_key
    assert execute_key == resume_key


async def test_execute_then_resume_hits_same_cache_entry_with_retry_policy():
    """With a retry policy, execute-then-resume on the same snapshot must reuse
    ONE cache entry — the resume's retry-aware struct hash matches the execute
    path's, so the compile factory never runs a second time (FAR-505)."""
    graph_cache_module._CACHE.clear()
    try:
        run = _make_run()
        final_run = _make_run(run_id=run.id, status="complete")
        snapshot = _make_snapshot()
        pipeline = _make_pipeline(dict(_RETRY_POLICY))
        registry = _mock_registry()
        build_mock = MagicMock(side_effect=lambda *_a, **_kw: _mock_compiled())

        # --- execute leg (REAL get_or_compile; only the factory is patched) ---
        with (
            patch(
                "modulo.core.pipeline_engine.executor.async_sessionmaker",
                return_value=_make_session_factory(_make_execute_session(snapshot, pipeline)),
            ),
            patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
            patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
            patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.executor.set_rls_org"),
            patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
            patch("modulo.core.pipeline_engine.executor.build_graph_from_json", build_mock),
            patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
            patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
            patch.object(PipelineExecutor, "_check_capacity", _bypass_capacity),
        ):
            executor = PipelineExecutor(MagicMock())
            await executor.execute(run_id=run.id, org_id=uuid.uuid4(), input_payload={})

        assert build_mock.call_count == 1

        # --- resume leg on the SAME pipeline/snapshot with a retry policy ---
        checkpointer_mock = MagicMock()
        checkpointer_mock.__aenter__ = AsyncMock(return_value=checkpointer_mock)
        checkpointer_mock.__aexit__ = AsyncMock(return_value=False)
        settings_mock = MagicMock()
        settings_mock.fernet_key = "test-fernet-key-not-for-production="
        with (
            patch(
                "modulo.core.pipeline_engine.executor.async_sessionmaker",
                return_value=_make_session_factory(_make_resume_session(snapshot, pipeline)),
            ),
            patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
            patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
            patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.executor.set_rls_org"),
            patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
            patch("modulo.core.pipeline_engine.executor.build_graph_from_json", build_mock),
            patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
            patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
            patch("modulo.core.pipeline_engine.executor._checkpointer_scope", return_value=checkpointer_mock),
            patch("modulo.settings.get_settings", return_value=settings_mock),
            patch("modulo.core.pipeline_engine.executor.RunawayGuard", return_value=MagicMock()),
        ):
            executor = PipelineExecutor(MagicMock())
            executor._checkpointer_conn_string = "sqlite:///test.db"
            await executor.resume(run_id=run.id, org_id=uuid.uuid4(), resume_data={"action": "approved"})

        # The resume hit the entry the execute leg compiled — no second factory
        # run, no divergent cache key.
        assert build_mock.call_count == 1
    finally:
        graph_cache_module._CACHE.clear()


# ---------------------------------------------------------------------------
# FAR-939 chunk 12 (M4) — run-frozen evaluation modes on the resume compile
# ---------------------------------------------------------------------------


def _mode_dto(
    node_id: str,
    *,
    mode: str | None,
    governed: bool = True,
    gate_id: uuid.UUID | None = None,
    eval_id: uuid.UUID | None = None,
) -> EvalDefDTO:
    return EvalDefDTO(
        id=eval_id or uuid.uuid4(),
        org_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        node_id=node_id,
        name="mode-dto",
        eval_type="regex",
        config={},
        failure_behaviour="block",
        policy_gate_id=(gate_id or uuid.uuid4()) if governed else None,
        policy_gate_version=1,
        policy_gate_node_id=uuid.UUID(node_id) if governed else None,
        policy_gate_evaluation_mode=mode,
    )


def test_frozen_capture_records_only_governed_evals_of_the_fired_source() -> None:
    """The fire-time stamp carries the source node's GOVERNED evals and their
    modes (§3.3 run-frozen) — ungoverned evals have no mode to freeze."""
    node = str(uuid.uuid4())
    target = str(uuid.uuid4())
    human_dto = _mode_dto(node, mode="human")
    automatic_dto = _mode_dto(node, mode="automatic")
    ungoverned = _mode_dto(node, mode=None, governed=False)
    by_node = {node: [human_dto, automatic_dto, ungoverned]}

    frozen = PipelineExecutor._frozen_gated_eval_modes(by_node, f"hitl_review_{node}_{target}")

    assert frozen == [
        {
            "eval_id": str(human_dto.id),
            "policy_gate_id": str(human_dto.policy_gate_id),
            "evaluation_mode": "human",
        },
        {
            "eval_id": str(automatic_dto.id),
            "policy_gate_id": str(automatic_dto.policy_gate_id),
            "evaluation_mode": "automatic",
        },
    ]


def test_frozen_capture_is_empty_for_an_unparseable_gate_id() -> None:
    """Manual-node ids (and anything else that is not
    ``hitl_review_<source>_<target>``) have no claim row to stamp."""
    node = str(uuid.uuid4())
    assert not PipelineExecutor._frozen_gated_eval_modes({node: [_mode_dto(node, mode="human")]}, "manual_step_1")
    assert not PipelineExecutor._frozen_gated_eval_modes(None, f"hitl_review_{node}_{uuid.uuid4()!s}")


def test_overlay_restores_the_run_start_mode_after_a_mid_run_flip() -> None:
    """THE M4 defect: the resume build reads the LIVE gate, so a human→
    automatic flip between pause and resume changed the resumed run's DTO
    universe (the Evidence grounding keyed on the mode stopped matching).
    The fire-time stamp puts the run-start mode back."""
    node = str(uuid.uuid4())
    live_dto = _mode_dto(node, mode="automatic")  # built from the FLIPPED live row
    by_node = {node: [live_dto]}
    stamp = [
        {
            "eval_id": str(live_dto.id),
            "policy_gate_id": str(live_dto.policy_gate_id),
            "evaluation_mode": "human",
        }
    ]

    applied = PipelineExecutor._overlay_frozen_gated_eval_modes(by_node, [{_GATED_EVAL_MODES_KEY: stamp}])

    assert applied == 1
    assert live_dto.policy_gate_evaluation_mode == "human"


def test_overlay_restores_automatic_when_the_flip_went_the_other_way() -> None:
    """automatic→human mid-run must NOT make a run that started automatic
    ground a human Evidence row (provenance lie) — the stamp freezes it back
    to automatic."""
    node = str(uuid.uuid4())
    live_dto = _mode_dto(node, mode="human")
    by_node = {node: [live_dto]}
    config = {
        _GATED_EVAL_MODES_KEY: [
            {
                "eval_id": str(live_dto.id),
                "policy_gate_id": str(live_dto.policy_gate_id),
                "evaluation_mode": "automatic",
            }
        ]
    }

    applied = PipelineExecutor._overlay_frozen_gated_eval_modes(by_node, [config])

    assert applied == 1
    assert live_dto.policy_gate_evaluation_mode == "automatic"


def test_overlay_skips_when_the_governing_gate_changed() -> None:
    """A stamp whose ``policy_gate_id`` no longer matches the live DTO's
    governing gate (disabled / soft-deleted / replaced) must not override
    operator control — the live-built DTO stands."""
    node = str(uuid.uuid4())
    live_dto = _mode_dto(node, mode="human", gate_id=uuid.uuid4())
    replaced_gate_id = uuid.uuid4()
    assert replaced_gate_id != live_dto.policy_gate_id
    by_node = {node: [live_dto]}
    config = {
        _GATED_EVAL_MODES_KEY: [
            {
                "eval_id": str(live_dto.id),
                "policy_gate_id": str(replaced_gate_id),
                "evaluation_mode": "automatic",
            }
        ]
    }

    applied = PipelineExecutor._overlay_frozen_gated_eval_modes(by_node, [config])

    assert applied == 0
    assert live_dto.policy_gate_evaluation_mode == "human"


def test_overlay_is_a_noop_without_a_stamp() -> None:
    """No fired gate (or a legacy claim row without the stamp) ⇒ no
    behaviour change — the resume build stands as-is."""
    node = str(uuid.uuid4())
    live_dto = _mode_dto(node, mode="human")
    by_node = {node: [live_dto]}

    assert PipelineExecutor._overlay_frozen_gated_eval_modes(by_node, None) == 0
    assert PipelineExecutor._overlay_frozen_gated_eval_modes(by_node, [None]) == 0
    assert PipelineExecutor._overlay_frozen_gated_eval_modes(by_node, [{}]) == 0
    assert live_dto.policy_gate_evaluation_mode == "human"


def test_overlay_skips_malformed_and_unmatched_stamp_entries() -> None:
    """Every defensive arm of the M4 overlay: a non-dict entry, an entry
    with no ``eval_id``, an entry with an out-of-vocabulary mode, an entry
    whose eval is no longer in the universe, and an entry whose eval lost its
    governing gate are ALL skipped. Only a well-formed entry naming the SAME
    governing gate is applied — so ``applied`` stays 0 and no mode moves."""
    node = str(uuid.uuid4())
    live_dto = _mode_dto(node, mode="human")
    ungoverned_dto = _mode_dto(node, mode="human", governed=False)
    by_node = {node: [live_dto, ungoverned_dto]}
    config = {
        _GATED_EVAL_MODES_KEY: [
            "not-a-dict",
            {"eval_id": "", "evaluation_mode": "human"},
            {"eval_id": str(live_dto.id), "evaluation_mode": "maybe"},
            {"eval_id": str(uuid.uuid4()), "evaluation_mode": "human"},
            {"eval_id": str(ungoverned_dto.id), "evaluation_mode": "human"},
        ]
    }

    applied = PipelineExecutor._overlay_frozen_gated_eval_modes(by_node, [config])

    assert applied == 0
    assert live_dto.policy_gate_evaluation_mode == "human"
    assert ungoverned_dto.policy_gate_evaluation_mode == "human"


async def test_resume_reapplies_the_fire_time_mode_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FAR-939 M4 end-to-end on the resume path: the live gate flipped
    human→automatic mid-run; the fired claim's stamp re-freezes the resumed
    run's DTO universe back to the run-start ``human`` mode, and the
    reapplication is logged with its structured event (so an operator can
    see the freeze took effect)."""
    run = _make_run()
    final_run = _make_run(run_id=run.id, status="complete")
    snapshot = _make_snapshot()
    pipeline = _make_pipeline(dict(_RETRY_POLICY))
    registry = _mock_registry()

    eval_id = uuid.uuid4()
    gate_id = uuid.uuid4()
    eval_row = MagicMock()
    eval_row.id = eval_id
    # A node the mocked stream never completes — post-node evals stay out of
    # this test's path (the compile is mocked; only the DTO universe matters).
    eval_row.node_id = "node-z"
    eval_row.pipeline_id = run.pipeline_id
    eval_row.name = "human-gate-eval"
    eval_row.eval_type = "regex"
    eval_row.config_json = {}
    eval_row.pass_threshold = None
    eval_row.suite_id = None
    eval_row.version = 1
    gate = MagicMock()
    gate.id = gate_id
    gate.enabled = True
    gate.action = "block"
    gate.version = 1
    gate.node_id = uuid.uuid4()
    gate.evaluation_mode = "automatic"  # the mid-run flip
    stamp = [
        {
            "eval_id": str(eval_id),
            "policy_gate_id": str(gate_id),
            "evaluation_mode": "human",
        }
    ]

    rs_builds: list[dict[str, Any]] = []

    def _fake_build(graph_json: dict[str, Any], **kwargs: Any) -> Any:
        rs_builds.append({"graph_json": graph_json, **kwargs})
        return _mock_compiled()

    def _fake_get_or_compile(pipeline_id: Any, snapshot_id: Any, factory_fn: Any, **kwargs: Any) -> Any:
        return factory_fn()

    session = _make_resume_session(
        snapshot,
        pipeline,
        eval_rows=[(eval_row, gate)],
        stamp_configs=[{_GATED_EVAL_MODES_KEY: stamp}],
    )
    checkpointer_mock = MagicMock()
    checkpointer_mock.__aenter__ = AsyncMock(return_value=checkpointer_mock)
    checkpointer_mock.__aexit__ = AsyncMock(return_value=False)
    settings_mock = MagicMock()
    settings_mock.fernet_key = "test-fernet-key-not-for-production="
    with (
        caplog.at_level(logging.INFO, logger="modulo.core.pipeline_engine.executor"),
        patch(
            "modulo.core.pipeline_engine.executor.async_sessionmaker",
            return_value=_make_session_factory(session),
        ),
        patch("modulo.core.pipeline_engine.executor.get_run", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.update_run_status", return_value=final_run),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_or_compile", side_effect=_fake_get_or_compile),
        patch("modulo.core.pipeline_engine.executor.build_graph_from_json", side_effect=_fake_build),
        patch("modulo.core.pipeline_engine.executor.get_registry", return_value=registry),
        patch("modulo.core.pipeline_engine.executor.GraphValidator", new=_mock_graph_validator()),
        patch("modulo.core.pipeline_engine.executor._checkpointer_scope", return_value=checkpointer_mock),
        patch("modulo.settings.get_settings", return_value=settings_mock),
        patch("modulo.core.pipeline_engine.executor.RunawayGuard", return_value=MagicMock()),
    ):
        executor = PipelineExecutor(MagicMock())
        executor._checkpointer_conn_string = "sqlite:///test.db"
        await executor.resume(run_id=run.id, org_id=uuid.uuid4(), resume_data={"action": "approved"})

    reapplied = [r for r in caplog.records if r.getMessage() == "eval_defs.frozen_modes_reapplied"]
    assert len(reapplied) == 1
    assert reapplied[0].entries == 1

    frozen_dto = rs_builds[0]["eval_definitions_by_node"]["node-z"][0]
    assert frozen_dto.id == eval_id
    assert frozen_dto.policy_gate_id == gate_id
    assert frozen_dto.policy_gate_evaluation_mode == "human"
