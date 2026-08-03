"""PipelineExecutor — orchestrates a single run end-to-end.

Responsibilities:
  - Seed initial LangGraph state from snapshot.run_context_defaults + input_payload
  - Obtain/compile the StateGraph (via graph_cache)
  - Enforce per-pipeline max_concurrent_runs and the per-org sandbox
    concurrency cap via count-based capacity checks (no FOR UPDATE; runs
    declined at capacity are demoted back to ``pending`` with a reason
    marker and retried by :meth:`PipelineExecutor._retry_pending`)
  - Consume astream_events() and publish to the per-run RunEventBroker
  - Set up AsyncPostgresSaver as LangGraph checkpointer
  - Stream graph execution, updating Run status on transitions
  - Mark run complete/failed/cancelled/awaiting_human/eval_failed in DB

Handles GraphInterrupt by transitioning the run to awaiting_human.
Does NOT handle WebSocket fan-out, HITL claim/approve/reject, or webhook triggers (phases 3+).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import socket
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from langchain_core.messages import BaseMessage
from langgraph.errors import GraphInterrupt
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.audit_logger import append_audit_event
from modulo.core.cost_controller import check_and_record_spend
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
from modulo.core.hitl_manager import HITLManager
from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.pipeline_engine.decorator import (
    RunCancelledError,
    set_cancellation_check,
    set_connector_hub,
    set_model_backend_hub,
)
from modulo.core.pipeline_engine.event_broker import RunEventBroker, get_registry
from modulo.core.pipeline_engine.graph_cache import build_graph_from_json, get_or_compile
from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver
from modulo.core.pipeline_engine.output_filter import OutputRejectedError
from modulo.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError
from modulo.core.trigger_engine.agent_signal import fire_agent_signal
from modulo.db.crud.run import (
    ERROR_CODE_CAPACITY_TIMEOUT,
    ERROR_CODE_ORG_CAPACITY_LIMITED,
    ERROR_CODE_PIPELINE_CAPACITY,
    _graph_contains_sandbox_agent,
    count_active_runs_for_pipeline,
    count_active_sandbox_runs_for_org,
    get_run,
    get_sandbox_concurrency_limit,
    update_run_status,
)
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org
from modulo.otel_bridge import LangGraphOtelBridge

_WORKER_ID: str = f"{socket.gethostname()}:{os.getpid()}"
_RETRY_SEMAPHORE: asyncio.Semaphore | None = None

# Statuses a run may still be admitted from when a retry's backoff elapses.
# Any terminal status (complete/failed/cancelled/eval_failed) means the run
# was already finalised while the retry loop slept — it must never be
# resurrected back to ``running``.
_ADMISSIBLE_STATUSES = frozenset({"pending", "running", "awaiting_human", "claimed", "waiting_for_lock"})

# Terminal statuses. A run in one of these is already finalised; it must never
# be resurrected AND must never spawn (or hold) a retry task.
_TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled", "eval_failed"})

_SANDBOX_AGENT_CACHE: OrderedDict[str, bool] = OrderedDict()
_SANDBOX_AGENT_CACHE_MAX = 512

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


def _seed_state(snapshot: PipelineSnapshot, input_payload: dict[str, Any]) -> dict[str, Any]:
    """Build the initial LangGraph state for a run.

    If *input_payload* contains a ``_feedback_correction`` key, it is
    promoted to the top-level ``run_context`` as ``feedback_correction``
    and removed from the input dict so the pipeline agents never see it
    as part of their normal input.
    """
    # Copy input_payload to avoid mutating the caller's dict.
    payload = dict(input_payload)
    run_context_defaults: dict[str, Any] = snapshot.run_context_defaults or {}
    run_context: dict[str, Any] = {
        **run_context_defaults,
        "cancelled": False,
        "input": payload,
    }
    # Promote feedback_correction from input_payload to run_context
    # so the entire graph can access rejection metadata.
    feedback_correction = payload.pop("_feedback_correction", None)
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
        data = lg_event.get("data")
        error = data.get("error", "") if isinstance(data, dict) else ""
        return "node_failed", {"node_id": name, "error": str(error)}
    return None


def _streamed_interrupts(lg_event: dict[str, Any]) -> tuple[Any, ...]:
    """Extract native LangGraph interrupts from a top-level stream event."""
    if lg_event.get("event") != "on_chain_stream":
        return ()
    data = lg_event.get("data")
    chunk = data.get("chunk") if isinstance(data, dict) else None
    interrupts = chunk.get("__interrupt__") if isinstance(chunk, dict) else None
    if isinstance(interrupts, (list, tuple)):
        return tuple(interrupts)
    return (interrupts,) if interrupts is not None else ()


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


def _sandbox_agent_for_snapshot(snapshot_id: uuid.UUID, graph_json: dict[str, Any] | None) -> bool:
    """Bounded per-snapshot cache over the (immutable) snapshot graph JSON."""
    key = str(snapshot_id)
    cached = _SANDBOX_AGENT_CACHE.get(key)
    if cached is not None:
        return cached
    result = _graph_contains_sandbox_agent(graph_json)
    if len(_SANDBOX_AGENT_CACHE) >= _SANDBOX_AGENT_CACHE_MAX:
        _SANDBOX_AGENT_CACHE.clear()
    _SANDBOX_AGENT_CACHE[key] = result
    return result


async def org_sandbox_capacity_free(
    session: AsyncSession,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> bool:
    """True when the org's sandbox cap (if configured) can admit *run_id*.

    Used by HITL routes as a PRE-approval check: the gate decision must not be
    committed when the org is at sandbox capacity. Fail-open — any error reads
    as ``True`` (admit) with a warning, never raises.
    """
    try:
        run = await get_run(session, run_id)
        if run is None or run.snapshot_id is None:
            return True
        snap_result = await session.execute(
            select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == run.snapshot_id)
        )
        graph_json = snap_result.scalar_one_or_none()
        if not _graph_contains_sandbox_agent(graph_json):
            return True
        cap = await get_sandbox_concurrency_limit(session, org_id)
        if cap is None:
            return True
        active = await count_active_sandbox_runs_for_org(session, org_id, exclude_run_id=run_id)
        return active < cap
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "hitl.sandbox_capacity_check_failed",
            extra={"org_id": str(org_id), "run_id": str(run_id)},
        )
        return True


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
        self._session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
        self._checkpointer_conn_string = checkpointer_conn_string
        self._otel_bridge = LangGraphOtelBridge()

    # Override in tests to avoid real delays.
    _capacity_poll_interval: float = 15.0
    # Retry backoff schedule (production: 120 → 240 → ... → 600s). Tests set
    # these tiny so a retry cycle completes in milliseconds.
    _retry_initial_delay: float = 120.0
    _retry_max_delay: float = 600.0
    _retry_max_attempts: int = 10
    # Token pricing constants
    _INPUT_TOKEN_RATE = Decimal("0.00001")
    _OUTPUT_TOKEN_RATE = Decimal("0.00003")

    async def _check_capacity(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        max_concurrent: int,
        graph_json: dict[str, Any] | None = None,
        snapshot_id: uuid.UUID | None = None,
    ) -> Run:
        """Non-blocking capacity check using count-based comparison.

        Soft-cap, no advisory lock. If the pipeline's ``max_concurrent_runs``
        limit OR the organisation's sandbox concurrency cap (when the graph
        contains a ``sandbox_agent`` node and a cap is configured) is reached,
        the run is atomically demoted back to ``pending`` with a reason marker
        on ``error_code`` (``pipeline_capacity`` / ``org_capacity_limited``)
        and picked up by :meth:`_retry_pending`.

        When *max_concurrent* is 0 or negative the pipeline is unlimited, but
        the organisation sandbox cap (if configured) still applies.

        Fail-open, loud: any error reading the org setting, counting org runs,
        or scanning the graph logs a warning and ADMITS the run (treats it as
        no-cap) rather than raising.
        """
        org_sandbox_cap: int | None = await self._read_org_sandbox_cap(org_id, graph_json, snapshot_id)

        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            run = await get_run(session, run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            if run.cancellation_requested:
                await update_run_status(session, run_id, "cancelled")
                cancelled_run = await get_run(session, run_id)
                if cancelled_run is None:
                    raise RunNotFoundError(run_id)
                return cancelled_run

            pipeline_capacity_ok = True
            active_count = 0
            if max_concurrent > 0:
                active_count = await count_active_runs_for_pipeline(
                    session, pipeline_id, include_pending=False, exclude_run_id=run_id
                )
                pipeline_capacity_ok = active_count < max_concurrent

            org_capacity_ok = True
            org_count = 0
            if org_sandbox_cap is not None:
                try:
                    org_count = await count_active_sandbox_runs_for_org(session, org_id, exclude_run_id=run_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.warning(
                        "pipeline.sandbox_org_count_failed",
                        extra={"org_id": str(org_id), "run_id": str(run_id)},
                    )
                else:
                    org_capacity_ok = org_count < org_sandbox_cap

            if pipeline_capacity_ok and org_capacity_ok:
                if run.status not in _ADMISSIBLE_STATUSES:
                    # The run went terminal (or hold) while a retry was backing
                    # off — never resurrect it. Return it untouched so the
                    # caller does not resume execution.
                    return run
                await update_run_status(
                    session,
                    run_id,
                    "running",
                    claimed_by=_WORKER_ID,
                    clear_error_code=True,
                )
                running_run = await get_run(session, run_id)
                if running_run is None:
                    raise RunNotFoundError(run_id)
                return running_run

            decline_code, decline_detail = self._capacity_decline(
                max_concurrent=max_concurrent,
                active_count=active_count,
                pipeline_capacity_ok=pipeline_capacity_ok,
                org_sandbox_cap=org_sandbox_cap,
                org_count=org_count,
                org_capacity_ok=org_capacity_ok,
            )
            # Demote to pending + reason marker so the caller's retry branch fires.
            await update_run_status(
                session,
                run_id,
                "pending",
                error_code=decline_code,
                error_detail=decline_detail,
            )
            pending_run = await get_run(session, run_id)
            if pending_run is None:
                raise RunNotFoundError(run_id)
            return pending_run

    async def _read_org_sandbox_cap(
        self,
        org_id: uuid.UUID,
        graph_json: dict[str, Any] | None,
        snapshot_id: uuid.UUID | None,
    ) -> int | None:
        """Read the org's sandbox cap (``None`` = no cap / no sandbox / fail-open).

        Short-circuits BEFORE any DB read: a graph with no ``sandbox_agent``
        node skips the settings read entirely. Each fail-open path logs a
        warning and treats the run as uncapped.
        """
        try:
            if snapshot_id is not None:
                has_sandbox = _sandbox_agent_for_snapshot(snapshot_id, graph_json)
            else:
                has_sandbox = _graph_contains_sandbox_agent(graph_json)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.sandbox_graph_scan_failed",
                extra={"org_id": str(org_id)},
            )
            return None
        if not has_sandbox:
            return None
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                return await get_sandbox_concurrency_limit(session, org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.sandbox_cap_read_failed",
                extra={"org_id": str(org_id)},
            )
            return None

    @staticmethod
    def _capacity_decline(
        *,
        max_concurrent: int,
        active_count: int,
        pipeline_capacity_ok: bool,
        org_sandbox_cap: int | None,
        org_count: int,
        org_capacity_ok: bool,
    ) -> tuple[str, str]:
        """Pick the capacity reason marker + sanitized human detail."""
        if not org_capacity_ok and org_sandbox_cap is not None:
            return (
                ERROR_CODE_ORG_CAPACITY_LIMITED,
                f"Org sandbox concurrency limit reached: {org_count} active, cap {org_sandbox_cap}",
            )
        return (
            ERROR_CODE_PIPELINE_CAPACITY,
            f"Pipeline max_concurrent_runs reached: {active_count} active, limit {max_concurrent}",
        )

    async def _run_status_retryable(self, run_id: uuid.UUID, org_id: uuid.UUID) -> bool:
        """True when the run is still in a retryable state (pending/running).

        Guards against re-admitting a run that went terminal while a retry
        was waiting its backoff. Fail-open True: if the status cannot be read,
        let :meth:`execute` decide.
        """
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                run = await get_run(session, run_id)
                return run is not None and run.status in ("pending", "running")
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "retry_pending.status_check_failed",
                extra={"run_id": str(run_id)},
            )
            return True

    async def _fail_capacity_timeout(self, *, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
        """Terminal-fail a run that stayed capacity-blocked past all retries."""
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                run = await get_run(session, run_id)
                if run is None or run.status != "pending":
                    return
                await update_run_status(
                    session,
                    run_id,
                    "failed",
                    error_code=ERROR_CODE_CAPACITY_TIMEOUT,
                    error_detail="Run stayed capacity-blocked beyond the retry window",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "pipeline.capacity_timeout_fail_failed",
                extra={"run_id": str(run_id)},
            )

    async def _touch_run_heartbeat(self, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
        """Refresh ``heartbeat_at`` so the beat sweep's stale-heartbeat fence
        sees a live retry loop.

        A capacity-blocked retry can span hours (10 attempts with backoff up to
        600s). Without a per-attempt heartbeat refresh the run would look
        STRANDED to ``stale_run_recovery_sweep``'s re-dispatch branch even
        though this loop is alive — the sweep would re-dispatch it, spawn a
        second retry loop, and double-execute the run when a slot frees. The
        refresh keeps ``heartbeat_at`` well inside
        ``_STRANDED_REDISPATCH_TTL_MINUTES`` (12 min) for any live loop.
        """
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await session.execute(
                    text("UPDATE runs SET heartbeat_at=now() WHERE id=:rid"),
                    {"rid": run_id},
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "retry_pending.heartbeat_refresh_failed",
                extra={"run_id": str(run_id)},
            )

    async def _retry_pending(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        input_payload: dict[str, Any],
    ) -> None:
        """Retry a pending run with exponential backoff until capacity frees up.

        Single-flight: the caller (``execute``) only spawns this task when NOT
        itself called from within a retry (``from_retry``), so at most one
        retry loop exists per blocked run. Each attempt re-checks the run's
        status (never re-admits a terminal run), refreshes ``heartbeat_at``
        (the beat sweep's liveness fence), exits on ``running`` or any
        non-``pending`` terminal/hold status, and after
        :attr:`_retry_max_attempts` terminal-fails with
        ``capacity_timeout`` instead of looping forever.
        """
        global _RETRY_SEMAPHORE
        if _RETRY_SEMAPHORE is None:
            _RETRY_SEMAPHORE = asyncio.Semaphore(2)
        async with _RETRY_SEMAPHORE:
            current_delay = self._retry_initial_delay
            attempts = 0
            while attempts < self._retry_max_attempts:
                attempts += 1
                await asyncio.sleep(self._capacity_poll_interval)
                await self._touch_run_heartbeat(run_id, org_id)
                if not await self._run_status_retryable(run_id, org_id):
                    return
                try:
                    result = await self.execute(
                        run_id=run_id,
                        org_id=org_id,
                        input_payload=input_payload,
                        from_retry=True,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception(
                        "retry_pending attempt failed for run %s (attempt %d/%d)",
                        run_id,
                        attempts,
                        self._retry_max_attempts,
                    )
                else:
                    if result.status != "pending":
                        return
                if attempts < self._retry_max_attempts:
                    await asyncio.sleep(current_delay)
                    current_delay = min(current_delay * 1.5, self._retry_max_delay)
            await self._fail_capacity_timeout(run_id=run_id, org_id=org_id)

    async def _load_eval_defs_for_pipeline(
        self,
        session: AsyncSession,
        pipeline_id: uuid.UUID,
    ) -> list[EvalDefinition]:
        """Load eval definitions for a pipeline that are scoped to a node."""
        eval_stmt = select(EvalDefinition).where(
            EvalDefinition.pipeline_id == pipeline_id,
            EvalDefinition.node_id.isnot(None),
        )
        return list((await session.execute(eval_stmt)).scalars().all())

    @staticmethod
    def _build_eval_defs_by_node(
        eval_rows: list[EvalDefinition],
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> dict[str, list[EvalDefDTO]]:
        """Convert eval definition ORM rows to a dict keyed by node id."""
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
        return eval_defs_by_node

    async def _init_model_backend_hub(self, org_id: uuid.UUID) -> ModelBackendHub | None:
        """Load active model backends for the org and initialise ModelBackendHub.

        Sets the hub on the current ContextVar so node_runner can access it.
        Returns the hub (or None if no backends are configured) for cleanup
        in the caller's finally block.
        """
        hub: ModelBackendHub | None = None
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                backend_rows = (
                    (
                        await session.execute(
                            select(ModelBackend).where(
                                ModelBackend.organisation_id == org_id,
                                ModelBackend.status == "active",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if isinstance(backend_rows, list) and backend_rows:
                    from modulo.settings import get_settings

                    _settings = get_settings()
                    from modulo.core.secrets_backend import create_secrets_backend

                    secrets_backend = create_secrets_backend(
                        fernet_key=_settings.fernet_key,
                        session=session,
                    )
                    hub = ModelBackendHub()
                    await hub.__aenter__()
                    await hub.initialise(backend_rows, secrets_backend=secrets_backend)
                    set_model_backend_hub(hub)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.model_backend_hub_init_failed")
            if hub is not None:
                try:
                    await hub.__aexit__(None, None, None)
                except Exception:
                    _log.exception("pipeline.model_backend_hub_cleanup_failed")
            hub = None
        return hub

    async def _init_connector_hub(self, org_id: uuid.UUID) -> Any | None:
        """Load active ConnectorInstance rows for the org and initialise ConnectorHub.

        Sets the hub on the current ContextVar so make_connector_fn can access it.
        Returns the hub (or None if no connectors are configured).
        """
        hub: Any | None = None
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                from sqlalchemy import select

                from modulo.db.models.connector_instance import ConnectorInstance

                rows = (
                    (
                        await session.execute(
                            select(ConnectorInstance).where(
                                ConnectorInstance.organisation_id == org_id,
                                ConnectorInstance.status == "active",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if isinstance(rows, list) and rows:
                    from modulo.core.connector_hub import ConnectorHub
                    from modulo.core.pipeline_engine.decorator import set_connector_hub
                    from modulo.core.runtime_provider import create_default_hub
                    from modulo.core.secrets_backend import create_secrets_backend
                    from modulo.settings import get_settings

                    _settings = get_settings()
                    secrets_backend = create_secrets_backend(
                        fernet_key=_settings.fernet_key,
                        session=session,
                    )
                    runtime_hub = create_default_hub()
                    hub = ConnectorHub(
                        secrets_backend=secrets_backend,
                        runtime_provider=runtime_hub,
                    )
                    await hub.__aenter__()
                    await hub.initialise(rows)
                    set_connector_hub(hub)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.connector_hub_init_failed")
            if hub is not None:
                try:
                    await hub.__aexit__(None, None, None)
                except Exception:
                    _log.exception("pipeline.connector_hub_cleanup_failed")
            hub = None
        return hub

    def _check_db_cancellation(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> Callable[[], Awaitable[bool]]:
        """Build a DB-backed cancellation check closure for a run."""

        async def _check() -> bool:
            try:
                return await asyncio.wait_for(
                    self._do_db_cancellation_check(org_id, run_id),
                    timeout=5.0,
                )
            except TimeoutError:
                _log.warning(
                    "run_context.cancellation_db_timeout",
                    extra={"run_id": str(run_id)},
                )
                return False

        return _check

    async def _do_db_cancellation_check(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> bool:
        """Execute the DB cancellation check query."""
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            run = await get_run(session, run_id)
            return run is not None and run.cancellation_requested

    @staticmethod
    def _compute_token_costs(
        node_token_usage: dict[str, dict[str, int]] | None,
        input_rate: Decimal,
        output_rate: Decimal,
    ) -> tuple[int | None, Decimal | None, dict[str, Any] | None]:
        """Compute total tokens, total cost, and per-node cost from node token usage."""
        if node_token_usage is None:
            return None, None, None

        total_tokens = sum(n.get("total_tokens") or 0 for n in node_token_usage.values())
        total_cost = Decimal(0)
        result_usage: dict[str, dict[str, Any]] = {}
        for node_id, n_data in node_token_usage.items():
            input_tokens = n_data.get("input_tokens")
            output_tokens = n_data.get("output_tokens")
            n_cost = Decimal(str(input_tokens if input_tokens is not None else 0)) * input_rate
            n_cost += Decimal(str(output_tokens if output_tokens is not None else 0)) * output_rate
            result_usage[node_id] = {**n_data, "cost_usd": float(n_cost)}
            total_cost += n_cost

        return total_tokens, total_cost, result_usage

    @staticmethod
    def _aggregate_sandbox_cost(completed_node_outputs: dict[str, Any] | None) -> Decimal:
        """Sum per-node sandbox-agent cost estimates into a single Decimal.

        Each completed node's captured output is ``{"artifacts": [...],
        "output": {...}}``; the inner ``output`` dict carries the numeric
        ``cost_estimate_usd`` attached by
        :func:`modulo.core.pipeline_engine.node_runner._compute_sandbox_cost`.
        Non-dict outputs, missing estimates, non-positive, and non-finite
        values (NaN/inf, which would otherwise corrupt the run total) contribute
        zero. Kept as a small pure helper so cost parity is testable and shared
        between :meth:`execute` and :meth:`resume`.
        """
        if not completed_node_outputs:
            return Decimal(0)
        sandbox_cost = Decimal(0)
        for node_output in completed_node_outputs.values():
            if not isinstance(node_output, dict):
                continue
            out = node_output.get("output")
            if not isinstance(out, dict):
                continue
            est = out.get("cost_estimate_usd")
            # bool is an int subclass; exclude it so `cost_estimate_usd: true`
            # doesn't aggregate as $1.
            if isinstance(est, (int, float)) and not isinstance(est, bool) and est > 0 and math.isfinite(est):
                sandbox_cost += Decimal(str(est))
        return sandbox_cost

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
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            run = await get_run(session, run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            await update_run_status(session, run_id, "running", claimed_by=_WORKER_ID)

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

            # Load eval definitions while session is active.
            eval_rows = await self._load_eval_defs_for_pipeline(session, run.pipeline_id)
            self._build_eval_defs_by_node(eval_rows, org_id, run.pipeline_id)

        pipeline_id = run.pipeline_id
        snapshot_id = run.snapshot_id
        thread_id = run.langgraph_thread_id

        async with self._session_factory() as session, session.begin():
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

        if not self._checkpointer_conn_string:
            raise RuntimeError("Cannot resume without a checkpointer configured")

        compiled = get_or_compile(
            pipeline_id,
            snapshot_id,
            lambda: build_graph_from_json(
                graph_json,
                session_factory=self._session_factory,
                org_id=org_id,
                pipeline_node_timeout_seconds=pipeline.node_timeout_seconds,
            ),
            pipeline_node_timeout_seconds=pipeline.node_timeout_seconds,
        )

        config = {"configurable": {"thread_id": thread_id}}
        node_ids = {str(n["id"]) for n in graph_json.get("nodes", [])}
        node_token_budgets: dict[str, int] = {
            str(n["id"]): n["token_budget"] for n in graph_json.get("nodes", []) if n.get("token_budget") is not None
        }

        final_status: str = "failed"
        error_code: str | None = None
        error_detail: str | None = None
        node_token_usage: dict[str, Any] | None = None
        completed_node_outputs: dict[str, Any] = {}
        broker = get_registry().get_or_create(run_id)
        set_cancellation_check(self._check_db_cancellation(org_id, run_id))
        self._otel_bridge.set_run_context(str(org_id), str(pipeline_id))

        # Load model backends for this run's org.
        model_backend_hub = await self._init_model_backend_hub(org_id)

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
                    pipeline_id=pipeline_id,
                    org_id=org_id,
                    completed_node_outputs=completed_node_outputs,
                    guard=guard,
                    node_token_budgets=node_token_budgets,
                )
        except RuntimeError as exc:
            if "checkpointer" in str(exc):
                _log.warning("pipeline.resume_no_checkpointer", extra={"run_id": str(run_id)})
                final_status = "failed"
                error_code = "configuration_error"
            else:
                raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            import traceback

            error_detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:2000]
            _log.exception("pipeline.resume_error", extra={"run_id": str(run_id)})
            final_status = "failed"
            error_code = type(exc).__name__
        finally:
            set_cancellation_check(None)
            set_model_backend_hub(None)
            if model_backend_hub is not None:
                await model_backend_hub.__aexit__(None, None, None)
            if final_status != "awaiting_human":
                get_registry().close(run_id)

        # Record audit events for block failures on resume.
        if final_status == "eval_failed" and error_code == "eval_blocked":
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                try:
                    await append_audit_event(
                        session,
                        org_id=org_id,
                        event_type="eval.blocked",
                        resource_type="run",
                        resource_id=run_id,
                        payload_json={"error_detail": error_code},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("audit.eval_blocked_failed", extra={"run_id": str(run_id)})

        total_tokens, total_cost_usd_val, _ = self._compute_token_costs(
            node_token_usage,
            self._INPUT_TOKEN_RATE,
            self._OUTPUT_TOKEN_RATE,
        )

        # Add sandbox-agent runtime cost from the nodes completed during resume,
        # mirroring execute() so cost parity holds for resumed HITL runs.
        _sandbox_cost = self._aggregate_sandbox_cost(completed_node_outputs)
        if _sandbox_cost > 0:
            total_cost_usd_val = (total_cost_usd_val or Decimal(0)) + _sandbox_cost

        async with self._session_factory() as session, session.begin():
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

        if total_cost_usd_val is not None:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await check_and_record_spend(session, org_id=org_id, cost_usd=total_cost_usd_val)

        if final_run is None:
            raise RunNotFoundError(run_id)
        return final_run

    async def execute(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        input_payload: dict[str, Any],
        from_retry: bool = False,
    ) -> Run:
        """Execute the run to completion. Returns the final Run row.

        *from_retry* is True when called from :meth:`_retry_pending`; it
        suppresses spawning a fresh retry task on a capacity decline so at most
        one single-flight retry loop exists per blocked run.
        """
        # Load run + pipeline + snapshot in one short-lived transaction.
        # Query directly to avoid SQLAlchemy async lazy-load (MissingGreenlet).
        async with self._session_factory() as session, session.begin():
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
        guard = RunawayGuard(
            max_duration_seconds=pipeline.max_duration_seconds,
            max_steps=pipeline.max_steps,
            token_budget=pipeline.token_budget,
        )

        snapshot_id = run.snapshot_id
        thread_id = run.langgraph_thread_id

        # Load eval definitions for conditional HITL gating (eval-before-interrupt).
        eval_defs_by_node: dict[str, list[EvalDefDTO]] = {}
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            eval_rows = await self._load_eval_defs_for_pipeline(session, pipeline_id)
        eval_defs_by_node = self._build_eval_defs_by_node(eval_rows, org_id, pipeline_id)

        # Non-blocking capacity check — if at limit the run is demoted back to
        # pending (with a reason marker) and retried by _retry_pending.
        capacity_run = await self._check_capacity(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            max_concurrent=max_concurrent,
            graph_json=graph_json,
            snapshot_id=snapshot_id,
        )
        if capacity_run.status != "running":
            if capacity_run.status in _TERMINAL_STATUSES:
                # The run went terminal while the capacity check ran (e.g. it
                # was cancelled/completed while backing off). Never spawn a
                # retry task for a terminal run — it would sleep
                # _capacity_poll_interval while holding the global retry
                # semaphore, then exit on the retryable check without doing
                # anything, briefly starving genuinely blocked runs of slots.
                return capacity_run
            if not from_retry:
                retry_task = asyncio.create_task(
                    self._retry_pending(
                        run_id=run_id,
                        org_id=org_id,
                        input_payload=input_payload,
                    )
                )
                retry_task.add_done_callback(lambda t: t.exception())
            return capacity_run

        final_status: str = "failed"
        error_code: str | None = None
        error_detail: str | None = None
        node_token_usage: dict[str, Any] | None = None
        completed_node_outputs: dict[str, Any] = {}
        broker = get_registry().get_or_create(run_id)
        set_cancellation_check(self._check_db_cancellation(org_id, run_id))
        self._otel_bridge.set_run_context(str(org_id), str(pipeline_id))

        # Load model backends for this run's org — provides LLM access to agent nodes.
        model_backend_hub = await self._init_model_backend_hub(org_id)
        # Load connector hub for this run's org — provides connector access to connector nodes.
        connector_hub = await self._init_connector_hub(org_id)

        try:
            # Compile (or retrieve from cache) the StateGraph.
            compiled = get_or_compile(
                pipeline_id,
                snapshot_id,
                lambda: build_graph_from_json(
                    graph_json,
                    eval_definitions_by_node=eval_defs_by_node,
                    session_factory=self._session_factory,
                    org_id=org_id,
                    pipeline_node_timeout_seconds=pipeline.node_timeout_seconds,
                ),
                pipeline_node_timeout_seconds=pipeline.node_timeout_seconds,
            )

            initial_state = _seed_state(snapshot, input_payload)
            initial_state["_run_id"] = run_id
            initial_state["_org_id"] = org_id
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
                        compiled,
                        initial_state,
                        config,
                        node_ids,
                        broker,
                        run_id,
                        pipeline_id=pipeline_id,
                        org_id=org_id,
                        completed_node_outputs=completed_node_outputs,
                        guard=guard,
                        node_token_budgets=node_token_budgets,
                    )
            else:
                final_status, error_code, error_detail, node_token_usage = await self._stream_graph(
                    compiled,
                    initial_state,
                    config,
                    node_ids,
                    broker,
                    run_id,
                    pipeline_id=pipeline_id,
                    org_id=org_id,
                    completed_node_outputs=completed_node_outputs,
                    guard=guard,
                    node_token_budgets=node_token_budgets,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            import traceback

            _tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:2000]
            _log.exception("pipeline.execution_error", extra={"run_id": str(run_id)})
            final_status = "failed"
            error_code = type(exc).__name__
            error_detail = _tb

        try:
            # Record audit events for block failures.
            if final_status == "eval_failed" and error_code == "eval_blocked":
                async with self._session_factory() as session, session.begin():
                    await set_rls_org(session, org_id)
                    try:
                        await append_audit_event(
                            session,
                            org_id=org_id,
                            event_type="eval.blocked",
                            resource_type="run",
                            resource_id=run_id,
                            payload_json={"error_detail": error_detail},
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _log.exception("audit.eval_blocked_failed", extra={"run_id": str(run_id)})

            # If the run completed, check for eval suite thresholds.
            if final_status == "complete":
                async with self._session_factory() as session, session.begin():
                    await set_rls_org(session, org_id)
                    try:
                        await self._check_eval_suites(session, run_id, pipeline_id)
                    except EvalSuiteBlockedError as exc:
                        final_status = "failed"
                        error_code = "eval_suite_blocked"
                        error_detail = str(exc)
                        broker.publish("run_failed", {"error": "eval_suite_blocked", "detail": str(exc)})
                        _log.warning(
                            "eval.suite_blocked",
                            extra={
                                "run_id": str(run_id),
                                "suite_id": exc.suite_id,
                                "score": exc.score,
                            },
                        )
                        try:
                            await append_audit_event(
                                session,
                                org_id=org_id,
                                event_type="eval.suite_blocked",
                                resource_type="run",
                                resource_id=run_id,
                                payload_json={
                                    "error_detail": error_detail,
                                    "suite_id": exc.suite_id,
                                    "score": exc.score,
                                },
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            _log.exception("audit.eval_suite_blocked_failed", extra={"run_id": str(run_id)})

                # Fire agent_signal triggers for each completed node.
                if completed_node_outputs:
                    async with self._session_factory() as session, session.begin():
                        await set_rls_org(session, org_id)
                        for node_id, node_output in completed_node_outputs.items():
                            try:
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
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                _log.exception(
                                    "agent_signal.failed",
                                    extra={
                                        "run_id": str(run_id),
                                        "node_id": node_id,
                                    },
                                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.post_stream_error", extra={"run_id": str(run_id)})
        finally:
            # Close broker after all post-stream work (suite checks, signals).
            set_cancellation_check(None)
            set_model_backend_hub(None)
            set_connector_hub(None)
            if model_backend_hub is not None:
                await model_backend_hub.__aexit__(None, None, None)
            if connector_hub is not None:
                await connector_hub.__aexit__(None, None, None)
            if final_status != "awaiting_human":
                get_registry().close(run_id)

        # Compute aggregate token/cost data from per-node usage.
        total_tokens, total_cost_usd_val, node_token_usage = self._compute_token_costs(
            node_token_usage,
            self._INPUT_TOKEN_RATE,
            self._OUTPUT_TOKEN_RATE,
        )

        # Add sandbox-agent runtime cost (wall-clock x E2B rate + agent-reported)
        # from the completed nodes' outputs, so Run.total_cost_usd covers every
        # cost class attributable to the run.
        _sandbox_cost = self._aggregate_sandbox_cost(completed_node_outputs)
        if _sandbox_cost > 0:
            total_cost_usd_val = (total_cost_usd_val or Decimal(0)) + _sandbox_cost

        # Mark complete/failed/cancelled/awaiting_human.
        async with self._session_factory() as session, session.begin():
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
                outputs_json=completed_node_outputs,
            )

        if total_cost_usd_val is not None:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await check_and_record_spend(session, org_id=org_id, cost_usd=total_cost_usd_val)

        if final_run is None:
            raise RunNotFoundError(run_id)
        return final_run

    async def _check_eval_suites(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> list[SuiteEvalResult]:
        """Check all eval suites with pass_threshold for a completed run.

        Queries eval definitions for the pipeline that belong to a suite
        with a pass_threshold, aggregates their results, and returns
        SuiteEvalResult for each suite.
        """
        stmt = select(EvalDefinition).where(
            EvalDefinition.pipeline_id == pipeline_id,
            EvalDefinition.suite_id.isnot(None),
            EvalDefinition.pass_threshold.isnot(None),
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
            if not defs_in_suite:
                continue
            eval_ids = [d.id for d in defs_in_suite]

            result_stmt = select(EvalResult).where(
                EvalResult.run_id == run_id,
                EvalResult.eval_id.in_(eval_ids),
            )
            result_result = await session.execute(result_stmt)
            eval_results = result_result.scalars().all()

            threshold_raw = next(
                (d.pass_threshold for d in defs_in_suite if d.pass_threshold is not None),
                None,
            )
            threshold = float(threshold_raw) if threshold_raw is not None else None

            suite_result_raw = evaluate_suite(
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
                aggregate_score=suite_result_raw.aggregate_score,
                passed=suite_result_raw.passed,
                blocking_failures=suite_result_raw.blocking_failures,
            )
            if threshold is not None and not suite_result.passed:
                raise EvalSuiteBlockedError(suite_id, suite_result.aggregate_score, threshold)
            results.append(suite_result)

        return results

    async def _handle_graph_interrupt(
        self,
        interrupts: Any,
        broker: RunEventBroker,
        run_id: uuid.UUID,
        *,
        pipeline_id: uuid.UUID | None,
        org_id: uuid.UUID | None,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Create the HITL gate and publish the awaiting event for an interrupt."""
        first_interrupt = interrupts[0] if interrupts else None
        value = getattr(first_interrupt, "value", None)
        gate_payload = value if isinstance(value, dict) else {}
        gate_id = gate_payload.get("gate_id", "")
        required_team_id_str = gate_payload.get("required_team_id")
        required_team_id = uuid.UUID(required_team_id_str) if required_team_id_str else None

        if pipeline_id is not None and org_id is not None:
            mgr = HITLManager()
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await mgr.create_gate(
                    session,
                    run_id=run_id,
                    gate_id=gate_id,
                    pipeline_id=pipeline_id,
                    org_id=org_id,
                    required_team_id=required_team_id,
                )
            broker.publish(
                "hitl_awaiting",
                {
                    "gate_payload": gate_payload,
                    "team_id": str(required_team_id) if required_team_id else None,
                },
            )
            return "awaiting_human", None, None, None

        _log.warning(
            "hitl_gate.cannot_create",
            extra={"run_id": str(run_id), "pipeline_id": str(pipeline_id), "org_id": str(org_id)},
        )
        broker.publish("run_failed", {"error": "gate_creation_failed", "detail": "Pipeline or org ID is None"})
        return "failed", "configuration_error", "Missing pipeline_id or org_id for HITL gate creation", None

    async def _stream_graph(
        self,
        compiled: Any,
        initial_state: dict[str, Any] | None,
        config: dict[str, Any],
        node_ids: set[str],
        broker: RunEventBroker,
        run_id: uuid.UUID,
        *,
        pipeline_id: uuid.UUID | None = None,
        org_id: uuid.UUID | None = None,
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

                interrupts = _streamed_interrupts(lg_event)
                if interrupts:
                    return await self._handle_graph_interrupt(
                        interrupts,
                        broker,
                        run_id,
                        pipeline_id=pipeline_id,
                        org_id=org_id,
                    )

                mapped = _map_lg_event(lg_event, node_ids)
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

                if event_kind == "on_chat_model_end":
                    metadata = lg_event.get("metadata") or {}
                    node_name = metadata.get("langgraph_node")
                    if node_name:
                        data = lg_event.get("data", {})
                        output = data.get("output", {}) if isinstance(data, dict) else {}
                        if isinstance(output, BaseMessage):
                            usage_metadata = getattr(output, "usage_metadata", None)
                        else:
                            usage_metadata = None
                        if usage_metadata is not None:
                            token_usage = {
                                "input_tokens": usage_metadata.get("input_tokens", 0) or 0,
                                "output_tokens": usage_metadata.get("output_tokens", 0) or 0,
                                "total_tokens": usage_metadata.get("total_tokens", 0) or 0,
                            }
                        elif isinstance(output, dict):
                            # Legacy fallback: llm_output.token_usage
                            llm_output = output.get("llm_output", {})
                            token_usage = llm_output.get("token_usage", {}) if isinstance(llm_output, dict) else {}
                        else:
                            token_usage = {}
                        if isinstance(token_usage, dict):
                            node_data = node_token_usage.setdefault(
                                node_name, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                            )
                            pt = token_usage.get("input_tokens", token_usage.get("prompt_tokens", 0)) or 0
                            ct = token_usage.get("output_tokens", token_usage.get("completion_tokens", 0)) or 0
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
                                        "token_budget",
                                        node_data["total_tokens"],
                                        node_budget,
                                    )

                elif event_kind == "on_llm_end":
                    # Legacy LLM interface — output is a dict with llm_output.
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
                            if node_token_budgets is not None:
                                node_budget = node_token_budgets.get(node_name)
                                if node_budget is not None and node_data["total_tokens"] > node_budget:
                                    raise RunawayRunError(
                                        "token_budget",
                                        node_data["total_tokens"],
                                        node_budget,
                                    )

            broker.publish("run_completed", {})
            return "complete", None, None, node_token_usage or None
        except GraphInterrupt as exc:
            interrupts = exc.args[0] if exc.args else []
            return await self._handle_graph_interrupt(
                interrupts,
                broker,
                run_id,
                pipeline_id=pipeline_id,
                org_id=org_id,
            )
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
        except TimeoutError as exc:
            error_detail = str(exc)
            broker.publish("run_failed", {"error": "node_timeout", "detail": error_detail})
            _log.warning(
                "node.timeout",
                extra={"run_id": str(run_id), "detail": error_detail},
            )
            return "failed", "node_timeout", error_detail, node_token_usage or None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            import traceback

            _tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:5000]
            broker.publish("run_failed", {"error": type(exc).__name__, "detail": _tb[:5000]})
            return "failed", type(exc).__name__, _tb[:5000], node_token_usage or None
