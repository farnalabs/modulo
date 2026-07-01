"""PipelineExecutor — orchestrates a single run end-to-end.

Responsibilities:
  - Seed initial LangGraph state from snapshot.run_context_defaults + input_payload
  - Obtain/compile the StateGraph (via graph_cache)
  - Enforce per-pipeline max_concurrent_runs via SELECT FOR UPDATE on pipeline row
  - Consume astream_events() and publish to the per-run RunEventBroker
  - Set up AsyncPostgresSaver as LangGraph checkpointer
  - Stream graph execution, updating Run status on transitions
  - Mark run complete/failed/cancelled/awaiting_human/eval_failed in DB

Handles NodeInterrupt by transitioning the run to awaiting_human.
Does NOT handle WebSocket fan-out, HITL claim/approve/reject, or webhook triggers (phases 3+).
"""

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from langgraph.errors import NodeInterrupt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.eval_engine import (
    EvalBlockedError,
    EvalSuiteBlockedError,
    SuiteEvalResult,
    evaluate_suite,
)
from modulo.core.eval_engine import (
    EvalDefinition as EvalDefDTO,
)
from modulo.core.eval_engine import (
    EvalResult as EngineEvalResult,
)
from modulo.core.graph_validator import GraphValidator
from modulo.core.pipeline_engine.decorator import (
    RunCancelledError,
    set_cancellation_check,
)
from modulo.core.pipeline_engine.event_broker import RunEventBroker, get_registry
from modulo.core.pipeline_engine.graph_cache import build_graph_from_json, get_or_compile
from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver
from modulo.core.pipeline_engine.output_filter import OutputRejectedError
from modulo.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError
from modulo.core.trigger_engine.agent_signal import fire_agent_signal
from modulo.db.crud.run import (
    count_active_runs_for_pipeline,
    get_run,
    update_run_status,
)
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org
from modulo.otel_bridge import LangGraphOtelBridge

_log = logging.getLogger(__name__)


class RunNotFoundError(KeyError):
    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(str(run_id))
        self.run_id = run_id


class GraphValidationError(ValueError):
    """Raised when pre-run graph validation fails with blocking errors."""

    def __init__(
        self,
        issues: list[Any],
        run_id: uuid.UUID,
    ) -> None:
        messages = [f"[{i.code}] {i.message}" for i in issues]
        super().__init__(f"Graph validation failed for run {run_id}: {'; '.join(messages)}")
        self.run_id = run_id
        self.issues = issues


def _graph_json_hash(graph_json: dict[str, Any]) -> str:
    serialised = json.dumps(graph_json, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialised.encode()).hexdigest()


def _seed_state(snapshot: PipelineSnapshot, input_payload: dict[str, Any]) -> dict[str, Any]:
    """Build the initial LangGraph state for a run.

    If *input_payload* contains a ``_feedback_correction`` key, it is
    promoted to the top-level ``run_context`` as ``feedback_correction``
    and removed from the input dict so the pipeline agents never see it
    as part of their normal input.
    """
    run_context: dict[str, Any] = {
        **snapshot.run_context_defaults,
        "cancelled": False,
        "input": input_payload,
    }
    # Promote feedback_correction from input_payload to run_context
    # so the entire graph can access rejection metadata.
    feedback_correction = input_payload.pop("_feedback_correction", None)
    if feedback_correction:
        run_context["feedback_correction"] = feedback_correction
    # Seed autonomy from snapshot-level default so gate nodes can resolve it.
    if snapshot.default_autonomy_level:
        run_context["_pipeline_default_autonomy"] = snapshot.default_autonomy_level
    return {
        "run_context": run_context,
        "artifacts": [],
    }


def _map_lg_event(
    lg_event: dict[str, Any],
    run_id: uuid.UUID,
    node_ids: set[str],
) -> tuple[str, dict[str, Any]] | None:
    """Map a LangGraph astream_events event to (event_type, payload) or None."""
    event_kind = lg_event.get("event", "")
    name = lg_event.get("name", "")

    if name not in node_ids:
        return None

    if event_kind == "on_chain_start":
        return "node_started", {"node_id": name}
    if event_kind == "on_chain_end":
        return "node_completed", {"node_id": name}
    if event_kind == "on_chain_error":
        error = lg_event.get("data", {}).get("error", "")
        return "node_failed", {"node_id": name, "error": str(error)}
    return None


def _strip_asyncpg(url: str) -> str:
    """Convert an asyncpg SQLAlchemy URL to a psycopg-compatible URL."""
    return url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")


@asynccontextmanager
async def _checkpointer_scope(
    conn_string: str,
    organisation_id: uuid.UUID,
    fernet_key: str | None = None,
) -> AsyncIterator[ModuloPostgresSaver]:
    """Create a ModuloPostgresSaver for the duration of a single run execution."""
    async with ModuloPostgresSaver.from_conn_string(
        conn_string,
        organisation_id=organisation_id,
        fernet_key=fernet_key,
    ) as saver:
        yield saver


class PipelineExecutor:
    """Execute a single pipeline run synchronously (sequential, HITL-aware).

    Args:
        db_engine: SQLAlchemy async engine for run CRUD operations.
        checkpointer_conn_string: psycopg-compatible connection string for
            LangGraph's AsyncPostgresSaver. If None, no checkpointer is used
            (runs will not persist checkpoints and HITL interrupts will not work).
    """

    def __init__(
        self,
        db_engine: AsyncEngine,
        *,
        checkpointer_conn_string: str | None = None,
    ) -> None:
        self._engine = db_engine
        self._session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        self._checkpointer_conn_string = checkpointer_conn_string
        self._otel_bridge = LangGraphOtelBridge()

    # Override in tests to avoid real delays.
    _capacity_poll_interval: float = 15.0

    async def _wait_for_capacity_or_fail(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        max_concurrent: int,
        lock_wait_seconds: int,
    ) -> Run:
        """Poll until capacity is available or timeout/cancelled.

        Serialises capacity checks via SELECT FOR UPDATE on the pipeline row
        to prevent TOCTOU races between the count check and status update.
        """
        deadline = datetime.now(UTC).timestamp() + lock_wait_seconds
        poll_interval = self._capacity_poll_interval

        while datetime.now(UTC).timestamp() < deadline:
            async with self._session_factory() as session:
                async with session.begin():
                    await set_rls_org(session, org_id)
                    # Serialise on the pipeline row — only one executor at a time
                    # passes this check for a given pipeline.
                    await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id).with_for_update())
                    run = await get_run(session, run_id)
                    if run is None:
                        raise RunNotFoundError(run_id)
                    if run.cancellation_requested:
                        await update_run_status(session, run_id, "cancelled")
                        cancelled_run = await get_run(session, run_id)
                        if cancelled_run is None:
                            raise RunNotFoundError(run_id)
                        return cancelled_run

                    active_count = await count_active_runs_for_pipeline(session, pipeline_id)
                    if active_count < max_concurrent:
                        await update_run_status(session, run_id, "running")
                        running_run = await get_run(session, run_id)
                        if running_run is None:
                            raise RunNotFoundError(run_id)
                        return running_run

            await asyncio.sleep(poll_interval)

        async with self._session_factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)
                await update_run_status(session, run_id, "failed", error_code="lock_timeout")
                run = await get_run(session, run_id)
                if run is None:
                    raise RunNotFoundError(run_id)
                return run

    async def resume(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        resume_data: dict[str, Any],
    ) -> Run:
        """Resume a run that was interrupted for HITL review.

        Loads the checkpointed graph state, injects *resume_data* as
        ``_hitl_decision``, and streams the graph until completion or the
        next interrupt.
        """
        async with self._session_factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)
                run = await get_run(session, run_id)
                if run is None:
                    raise RunNotFoundError(run_id)
                await update_run_status(session, run_id, "running")

                snapshot_result = await session.execute(
                    select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id)
                )
                snapshot = snapshot_result.scalar_one()
                graph_json: dict[str, Any] = snapshot.graph_json

                # Re-validate the snapshot before resuming — the pipeline
                # config may have changed since the original run started.
                validation = await GraphValidator().validate_for_run(snapshot, {}, session)
                if not validation.is_valid:
                    raise GraphValidationError(validation.issues, run_id)

        pipeline_id = run.pipeline_id
        snapshot_id = run.snapshot_id
        thread_id = run.langgraph_thread_id

        async with self._session_factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)

                # Load pipeline for runaway protection limits.
                pipeline_result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
                pipeline = pipeline_result.scalar_one_or_none()
                if pipeline is None:
                    raise RunNotFoundError(run_id)

                guard = RunawayGuard(
                    max_duration_seconds=pipeline.max_duration_seconds,
                    max_steps=pipeline.max_steps,
                    token_budget=pipeline.token_budget,
                )

                # Load eval definitions for eval-before-interrupt (resume path).
                eval_stmt = select(EvalDefinition).where(
                    EvalDefinition.pipeline_id == pipeline_id,
                    EvalDefinition.node_id.isnot(None),
                )
                eval_rows = (await session.execute(eval_stmt)).scalars().all()
                resume_eval_defs_by_node: dict[str, list[EvalDefDTO]] = {}
        for e in eval_rows:
            node_key = str(e.node_id) if e.node_id else ""
            if node_key:
                resume_eval_defs_by_node.setdefault(node_key, []).append(
                    EvalDefDTO(
                        id=e.id,
                        org_id=org_id,
                        pipeline_id=e.pipeline_id,
                        node_id=node_key,
                        name=e.name,
                        eval_type=e.eval_type,
                        config=e.config_json,
                        failure_behaviour=e.failure_behaviour,
                        pass_threshold=e.pass_threshold,
                        suite_id=e.suite_id,
                    )
                )

        compiled = get_or_compile(
            pipeline_id,
            snapshot_id,
            lambda: build_graph_from_json(
                graph_json,
                eval_definitions_by_node=resume_eval_defs_by_node,
            ),
        )

        config = {"configurable": {"thread_id": thread_id}}
        node_ids = {str(n["id"]) for n in graph_json.get("nodes", [])}
        node_token_budgets: dict[str, int] = {
            str(n["id"]): n["token_budget"]
            for n in graph_json.get("nodes", [])
            if n.get("token_budget") is not None
        }

        if not self._checkpointer_conn_string:
            raise RuntimeError("Cannot resume without a checkpointer configured")

        final_status: str = "failed"
        error_code: str | None = None
        node_token_usage: dict[str, Any] | None = None
        broker = get_registry().get_or_create(run_id)
        try:
            from modulo.settings import get_settings

            _settings = get_settings()
            async with _checkpointer_scope(
                self._checkpointer_conn_string,
                organisation_id=org_id,
                fernet_key=_settings.fernet_key,
            ) as saver:
                compiled.checkpointer = saver
                await compiled.aupdate_state(config, {"_hitl_decision": resume_data})
                final_status, error_code, _, node_token_usage = await self._stream_graph(
                    compiled,
                    None,
                    config,
                    node_ids,
                    broker,
                    run_id,
                    guard=guard,
                    node_token_budgets=node_token_budgets,
                )
        except Exception as exc:
            _log.exception("pipeline.resume_error", extra={"run_id": str(run_id)})
            final_status = "failed"
            error_code = type(exc).__name__
        finally:
            if final_status != "awaiting_human":
                get_registry().close(run_id)

        total_tokens: int | None = None
        if node_token_usage:
            total_tokens = sum(n["total_tokens"] for n in node_token_usage.values())
            input_rate = Decimal("0.00001")
            output_rate = Decimal("0.00003")
            total_cost = Decimal("0")
            for n_data in node_token_usage.values():
                n_cost = Decimal(str(n_data.get("input_tokens", 0))) * input_rate
                n_cost += Decimal(str(n_data.get("output_tokens", 0))) * output_rate
                n_data["cost_usd"] = float(n_cost)
                total_cost += n_cost

        async with self._session_factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)
                final_run = await update_run_status(
                    session,
                    run_id,
                    final_status,
                    error_code=error_code,
                    total_tokens=total_tokens,
                    total_cost_usd=total_cost if node_token_usage else None,
                    node_token_usage=node_token_usage,
                )

        if final_run is None:
            raise RunNotFoundError(run_id)
        return final_run

    async def execute(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        input_payload: dict[str, Any],
    ) -> Run:
        """Execute the run to completion. Returns the final Run row."""
        # Load run + pipeline + snapshot in one short-lived transaction.
        # Query directly to avoid SQLAlchemy async lazy-load (MissingGreenlet).
        async with self._session_factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)
                run = await get_run(session, run_id)
                if run is None:
                    raise RunNotFoundError(run_id)
                pipeline_result = await session.execute(select(Pipeline).where(Pipeline.id == run.pipeline_id))
                pipeline = pipeline_result.scalar_one()
                snapshot_result = await session.execute(
                    select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id)
                )
                snapshot = snapshot_result.scalar_one()
                graph_json: dict[str, Any] = snapshot.graph_json

                # Pre-run validation — blocks execution on errors.
                validation = await GraphValidator().validate_for_run(snapshot, input_payload, session)
                if not validation.is_valid:
                    raise GraphValidationError(validation.issues, run_id)

        # Capture scalar attributes before the session closes.
        pipeline_id = run.pipeline_id
        max_concurrent = pipeline.max_concurrent_runs
        lock_wait_seconds = pipeline.lock_wait_timeout_seconds
        guard = RunawayGuard(
            max_duration_seconds=pipeline.max_duration_seconds,
            max_steps=pipeline.max_steps,
            token_budget=pipeline.token_budget,
        )

        snapshot_id = run.snapshot_id
        thread_id = run.langgraph_thread_id

        # Load eval definitions for conditional HITL gating (eval-before-interrupt).
        async with self._session_factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)
                eval_stmt = select(EvalDefinition).where(
                    EvalDefinition.pipeline_id == pipeline_id,
                    EvalDefinition.node_id.isnot(None),
                )
                eval_rows = (await session.execute(eval_stmt)).scalars().all()
        eval_defs_by_node: dict[str, list[EvalDefDTO]] = {}
        for e in eval_rows:
            node_key = str(e.node_id) if e.node_id else ""
            if node_key:
                eval_defs_by_node.setdefault(node_key, []).append(
                    EvalDefDTO(
                        id=e.id,
                        org_id=org_id,
                        pipeline_id=e.pipeline_id,
                        node_id=node_key,
                        name=e.name,
                        eval_type=e.eval_type,
                        config=e.config_json,
                        failure_behaviour=e.failure_behaviour,
                        pass_threshold=e.pass_threshold,
                        suite_id=e.suite_id,
                    )
                )

        # Wait for capacity slot (or return cancelled/timed out).
        capacity_run = await self._wait_for_capacity_or_fail(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            max_concurrent=max_concurrent,
            lock_wait_seconds=lock_wait_seconds,
        )
        if capacity_run.status != "running":
            return capacity_run

        # Build DB-backed cancellation check closure for this run.
        async def _check_db_cancellation() -> bool:
            async with self._session_factory() as session:
                await set_rls_org(session, org_id)
                run = await get_run(session, run_id)
                return run is not None and run.cancellation_requested

        final_status: str = "failed"
        error_code: str | None = None
        error_detail: str | None = None
        node_token_usage: dict[str, Any] | None = None
        completed_node_outputs: dict[str, Any] = {}
        broker = get_registry().get_or_create(run_id)
        set_cancellation_check(_check_db_cancellation)
        try:
            # Compile (or retrieve from cache) the StateGraph.
            compiled = get_or_compile(
                pipeline_id,
                snapshot_id,
                lambda: build_graph_from_json(
                    graph_json,
                    eval_definitions_by_node=eval_defs_by_node,
                ),
            )

            initial_state = _seed_state(snapshot, input_payload)
            config = {"configurable": {"thread_id": thread_id}}
            node_ids = {str(n["id"]) for n in graph_json.get("nodes", [])}
            node_token_budgets: dict[str, int] = {
                str(n["id"]): n["token_budget"]
                for n in graph_json.get("nodes", [])
                if n.get("token_budget") is not None
            }

            if self._checkpointer_conn_string:
                from modulo.settings import get_settings

                _settings = get_settings()
                async with _checkpointer_scope(
                    self._checkpointer_conn_string,
                    organisation_id=org_id,
                    fernet_key=_settings.fernet_key,
                ) as saver:
                    compiled.checkpointer = saver
                    final_status, error_code, error_detail, node_token_usage = await self._stream_graph(
                        compiled, initial_state, config, node_ids, broker, run_id,
                        completed_node_outputs=completed_node_outputs,
                        guard=guard,
                        node_token_budgets=node_token_budgets,
                    )
            else:
                final_status, error_code, error_detail, node_token_usage = await self._stream_graph(
                    compiled, initial_state, config, node_ids, broker, run_id,
                    completed_node_outputs=completed_node_outputs,
                    guard=guard,
                    node_token_budgets=node_token_budgets,
                )
        except Exception as exc:
            _log.exception("pipeline.execution_error", extra={"run_id": str(run_id)})
            final_status = "failed"
            error_code = type(exc).__name__
        finally:
            set_cancellation_check(None)
            if final_status != "awaiting_human":
                get_registry().close(run_id)

        # If the run completed, check for eval suite thresholds.
        if final_status == "complete":
            async with self._session_factory() as session:
                async with session.begin():
                    await set_rls_org(session, org_id)
                    try:
                        suite_results = await self._check_eval_suites(session, run_id, pipeline_id)
                        for sr in suite_results:
                            if not sr.passed:
                                final_status = "failed"
                                error_code = "eval_suite_blocked"
                                _log.warning(
                                    "eval.suite_blocked",
                                    extra={
                                        "run_id": str(run_id),
                                        "suite_id": sr.suite_id,
                                        "score": sr.aggregate_score,
                                    },
                                )
                                break
                    except EvalSuiteBlockedError as exc:
                        final_status = "failed"
                        error_code = "eval_suite_blocked"
                        error_detail = str(exc)
                        _log.warning(
                            "eval.suite_blocked",
                            extra={
                                "run_id": str(run_id),
                                "suite_id": exc.suite_id,
                                "score": exc.score,
                            },
                        )

            # Fire agent_signal triggers for each completed node.
            if final_status == "complete" and completed_node_outputs:
                async with self._session_factory() as session:
                    async with session.begin():
                        await set_rls_org(session, org_id)
                        for node_id, node_output in completed_node_outputs.items():
                            signal_results = await fire_agent_signal(
                                session,
                                org_id=org_id,
                                source_run_id=run_id,
                                source_pipeline_id=pipeline_id,
                                completed_node_id=node_id,
                                node_output=node_output,
                            )
                            for sr in signal_results:
                                _log.info(
                                    "agent_signal.%s trigger=%s run=%s",
                                    sr["status"],
                                    sr.get("trigger_id", "?"),
                                    sr.get("run_id", "?"),
                                )

        # Compute aggregate token/cost data from per-node usage.
        total_tokens: int | None = None
        total_cost_usd_val: Decimal | None = None
        if node_token_usage:
            total_tokens = sum(n["total_tokens"] for n in node_token_usage.values())

            input_rate = Decimal("0.00001")
            output_rate = Decimal("0.00003")
            total_cost = Decimal("0")
            for n_data in node_token_usage.values():
                n_cost = Decimal(str(n_data.get("input_tokens", 0))) * input_rate
                n_cost += Decimal(str(n_data.get("output_tokens", 0))) * output_rate
                n_data["cost_usd"] = float(n_cost)
                total_cost += n_cost
            total_cost_usd_val = total_cost

        # Mark complete/failed/cancelled/awaiting_human.
        async with self._session_factory() as session:
            async with session.begin():
                await set_rls_org(session, org_id)
                final_run = await update_run_status(
                    session,
                    run_id,
                    final_status,
                    error_code=error_code,
                    error_detail=error_detail,
                    total_tokens=total_tokens,
                    total_cost_usd=total_cost_usd_val,
                    node_token_usage=node_token_usage,
                )

        if final_run is None:
            raise RunNotFoundError(run_id)
        return final_run

    async def _check_eval_suites(
        self,
        session: Any,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> list[SuiteEvalResult]:
        """Check all eval suites with pass_threshold for a completed run.

        Queries eval definitions for the pipeline that belong to a suite
        with a pass_threshold, aggregates their results, and returns
        SuiteEvalResult for each suite.
        """
        stmt = (
            select(EvalDefinition)
            .where(
                EvalDefinition.pipeline_id == pipeline_id,
                EvalDefinition.suite_id.isnot(None),
                EvalDefinition.pass_threshold.isnot(None),
            )
            .distinct(EvalDefinition.suite_id)
        )
        result = await session.execute(stmt)
        suite_defs = result.scalars().all()

        if not suite_defs:
            return []

        suite_ids = list({d.suite_id for d in suite_defs if d.suite_id})
        results: list[SuiteEvalResult] = []
        for suite_id in suite_ids:
            eval_stmt = select(EvalDefinition).where(
                EvalDefinition.suite_id == suite_id,
                EvalDefinition.pipeline_id == pipeline_id,
            )
            eval_result = await session.execute(eval_stmt)
            defs_in_suite = eval_result.scalars().all()
            eval_ids = [d.id for d in defs_in_suite]

            result_stmt = select(EvalResult).where(
                EvalResult.run_id == run_id,
                EvalResult.eval_id.in_(eval_ids),
            )
            result_result = await session.execute(result_stmt)
            eval_results = result_result.scalars().all()

            threshold = next(
                (d.pass_threshold for d in defs_in_suite if d.pass_threshold is not None),
                None,
            )

            suite_result = evaluate_suite(
                eval_results=[
                    EngineEvalResult(
                        id=r.id,
                        run_id=r.run_id,
                        node_id=str(r.node_id) if r.node_id else "",
                        eval_id=r.eval_id,
                        passed=r.passed,
                        score=r.score,
                        detail=r.detail or "",
                        evaluated_at=r.evaluated_at,
                    )
                    for r in eval_results
                ],
                suite_id=suite_id,
                pass_threshold=threshold,
            )

            suite_result = SuiteEvalResult(
                suite_id=suite_id,
                total_evals=len(eval_results),
                passed_evals=sum(1 for r in eval_results if r.passed),
                aggregate_score=suite_result.aggregate_score,
                passed=suite_result.passed,
                blocking_failures=suite_result.blocking_failures,
            )
            if threshold is not None and not suite_result.passed:
                raise EvalSuiteBlockedError(suite_id, suite_result.aggregate_score, threshold)
            results.append(suite_result)

        return results

    async def _stream_graph(
        self,
        compiled: Any,
        initial_state: dict[str, Any] | None,
        config: dict[str, Any],
        node_ids: set[str],
        broker: RunEventBroker,
        run_id: uuid.UUID,
        completed_node_outputs: dict[str, Any] | None = None,
        guard: RunawayGuard | None = None,
        node_token_budgets: dict[str, int] | None = None,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Stream graph execution, mapping events to broker publishes.

        If *completed_node_outputs* is provided (a mutable dict), it will be
        populated with ``{node_id: output_data}`` for each completed node.

        If *guard* is provided, runaway run protection checks are enforced
        before each event and on node completion / token usage.

        If *node_token_budgets* is provided (``{node_id: token_budget}``),
        per-node token budgets are enforced after each LLM call — if a node's
        cumulative tokens exceed its budget a ``RunawayRunError`` is raised.

        Returns (final_status, error_code, error_detail, node_token_usage).
        """
        node_token_usage: dict[str, dict[str, int]] = {}
        lg_config = {**config, "callbacks": [self._otel_bridge]}
        try:
            async for lg_event in compiled.astream_events(initial_state, lg_config, version="v2"):
                if guard is not None:
                    guard.check_duration()

                mapped = _map_lg_event(lg_event, run_id, node_ids)
                if mapped is not None:
                    event_type, payload = mapped
                    broker.publish(event_type, payload)

                event_kind = lg_event.get("event", "")
                # Capture node output for agent_signal trigger firing.
                if event_kind == "on_chain_end":
                    name = lg_event.get("name", "")
                    if name in node_ids:
                        if guard is not None:
                            guard.record_step()
                        if completed_node_outputs is not None:
                            data = lg_event.get("data", {})
                            output = data.get("output") if isinstance(data, dict) else None
                            if output is not None:
                                completed_node_outputs[name] = output

                if event_kind in ("on_chat_model_end", "on_llm_end"):
                    metadata = lg_event.get("metadata") or {}
                    node_name = metadata.get("langgraph_node")
                    if node_name:
                        data = lg_event.get("data", {})
                        output = data.get("output", {}) if isinstance(data, dict) else {}
                        llm_output = output.get("llm_output", {}) if isinstance(output, dict) else {}
                        token_usage = llm_output.get("token_usage", {}) if isinstance(llm_output, dict) else {}
                        if isinstance(token_usage, dict):
                            node_data = node_token_usage.setdefault(
                                node_name, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                            )
                            pt = token_usage.get("prompt_tokens", 0) or 0
                            ct = token_usage.get("completion_tokens", 0) or 0
                            tt = token_usage.get("total_tokens", 0) or 0
                            node_data["input_tokens"] += pt
                            node_data["output_tokens"] += ct
                            node_data["total_tokens"] += tt
                            if guard is not None:
                                guard.record_tokens(tt)
                            # Per-node token budget check
                            if node_token_budgets is not None:
                                node_budget = node_token_budgets.get(node_name)
                                if node_budget is not None and node_data["total_tokens"] > node_budget:
                                    raise RunawayRunError(
                                        "token_budget", node_data["total_tokens"], node_budget,
                                    )

            broker.publish("run_completed", {})
            return "complete", None, None, node_token_usage or None
        except NodeInterrupt as exc:
            interrupts = exc.args[0] if exc.args else []
            gate_payload = interrupts[0].value if interrupts else {}
            broker.publish("hitl_awaiting", {"gate_payload": gate_payload})
            return "awaiting_human", None, None, None
        except EvalBlockedError as exc:
            broker.publish("run_failed", {"error": "eval_blocked", "detail": str(exc)})
            return "eval_failed", "eval_blocked", str(exc), None
        except OutputRejectedError as exc:
            broker.publish("run_failed", {"error": "output_rejected", "detail": str(exc)})
            return "output_rejected", "output_rejected", str(exc), None
        except RunCancelledError:
            broker.publish("run_cancelled", {})
            return "cancelled", None, None, None
        except RunawayRunError as exc:
            error_detail = str(exc)
            broker.publish("run_failed", {"error": "runaway", "detail": error_detail})
            _log.warning(
                "runaway.terminated",
                extra={
                    "run_id": str(run_id),
                    "guard": exc.guard,
                    "current": exc.current,
                    "limit": exc.limit,
                },
            )
            return "failed", "runaway", error_detail, node_token_usage or None
        except Exception as exc:
            broker.publish("run_failed", {"error": type(exc).__name__})
            return "failed", type(exc).__name__, None, None
