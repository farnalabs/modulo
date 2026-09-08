"""FAR-592 (D6 F2) prove-the-fix: the Local-tier refusal shortcut in
``PipelineExecutor.execute`` lands the run TERMINAL (``failed`` +
``sandbox.tier_refused``) and MUST skip the pipeline ``retry_policy`` gate —
a deterministic config refusal (profile opt-in cannot change mid-run) must
never be requeued, even when the policy lists ``"failure"``.

The test drives ``execute`` with every heavy dependency mocked at its boundary
so the only thing that runs for real is the ``except (NodeCancelledError,
SandboxNodeFailedError)`` handler and the post-stream retry-decision block.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.core.pipeline_engine.node_runner import SandboxTierRefusedError


def _mocked_executor() -> PipelineExecutor:
    executor = PipelineExecutor(MagicMock())

    run = MagicMock()
    run.variant_config_snapshot = None
    run.owner_team_id = None
    run.pipeline_id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()

    pipeline = MagicMock()
    pipeline.max_concurrent_runs = 5
    pipeline.retry_policy = {"on": ["failure"], "max_retries": 2}

    scalars = {
        "pipeline_id": run.pipeline_id,
        "max_concurrent": 5,
        "pipeline_retry_policy": {"on": ["failure"], "max_retries": 2},
        "guard": MagicMock(),
        "snapshot_id": run.snapshot_id,
        "thread_id": uuid.uuid4(),
        "is_correction_run": False,
        "run_number": 1,
    }

    capacity_run = MagicMock()
    capacity_run.status = "running"

    broker = MagicMock()

    # --- boundary mocks: everything EXCEPT the tier-refused raise ----------
    executor._load_execution_context = AsyncMock(return_value=(run, pipeline, MagicMock(), {}, {}))
    executor._capture_execution_scalars = MagicMock(return_value=scalars)
    executor._check_capacity = AsyncMock(return_value=capacity_run)
    executor._check_spend_ceiling_gate = AsyncMock(return_value=None)
    executor._init_run_environment = AsyncMock(return_value=(MagicMock(), MagicMock(), broker, False))
    executor._load_eval_defs_for_pipeline = AsyncMock(return_value=[])
    executor._build_eval_defs_by_node = MagicMock(return_value={})
    # The terminalization path is mocked so we can ASSERT what flows into it
    # without needing a database.
    executor._run_post_stream_and_teardown = AsyncMock(
        return_value=("failed", "sandbox.tier_refused", "local tier refused")
    )
    final_run = MagicMock()
    executor._finalize_run_after_stream = AsyncMock(return_value=final_run)
    # The objection of the test: the retry policy must NEVER be consulted for a
    # deterministic tier refusal.
    executor._maybe_retry_after_policy = AsyncMock()
    return executor


async def test_tier_refused_is_terminal_and_skips_retry_policy() -> None:
    """A SandboxTierRefusedError raised during streaming lands the run terminal
    (failed / sandbox.tier_refused) and the pipeline retry_policy (configured
    for ``failure``) is NEVER consulted — the run is not requeued."""
    executor = _mocked_executor()

    async def _raise(*_args, **_kwargs):
        raise SandboxTierRefusedError("Local provider tier refused runner bindings for node 'n1'")

    executor._prepare_and_stream = _raise

    result = await executor.execute(
        run_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        input_payload={"input": {}},
    )

    # The run-object returned by the (mocked) terminalization is what execute
    # yields — non-None proves we reached the terminal path.
    assert result is not None
    # The retry policy gate is the whole point: it must not be called.
    executor._maybe_retry_after_policy.assert_not_called()
    # And the terminal outcome written must be the typed refusal code.
    _, kwargs = executor._finalize_run_after_stream.call_args
    assert kwargs["final_status"] == "failed"
    assert kwargs["error_code"] == "sandbox.tier_refused"
