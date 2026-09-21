"""Per-eval compute→persist→decide helper (FAR-971 chunk 2).

Implements the persist-before-decide reordering for both the post-node
evaluation loop (executor) and the HITL gate-eval loop (node-runner).

Each eval definition is processed independently:
  1. **Compute** via :meth:`EvalEngine.evaluate_result` (never raises ``EvalBlockedError``).
  2. **Persist** one ``EvalResult`` row in its own transaction (per-eval commit).
  3. **Decide** — raise ``EvalBlockedError`` if the eval failed *and* its
     ``failure_behaviour == "block"``.

Both call sites (``_run_post_node_evals`` and ``_run_gate_evals``) delegate
to :func:`run_evals_persist_before_decide`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from modulo.core.eval_engine import (
    EvalBlockedError,
    EvalEngine,
    LLMJudgeCallable,
)
from modulo.core.eval_engine import (
    EvalDefinition as EvalDefDTO,
)
from modulo.core.eval_engine import (
    EvalResult as EngineEvalResult,
)
from modulo.db.models.eval_result import EvalResult as EvalResultModel
from modulo.db.rls import set_rls_execution_context, set_rls_org

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metrics — OTel counter for persistence failures
# ---------------------------------------------------------------------------
_eval_result_persist_failures_total: Any = None


def _get_otel_meter() -> Any:
    """Return the ``modulo.pipeline_engine`` meter, or ``None``."""
    try:
        from opentelemetry import metrics as _otel_metrics

        provider = _otel_metrics.get_meter_provider()
        if provider is None:
            return None
        return provider.get_meter("modulo.pipeline_engine", version="0.1.0")
    except Exception:
        return None


def _ensure_metrics() -> None:
    """Lazily initialise the persistence-failure counter (idempotent)."""
    global _eval_result_persist_failures_total
    if _eval_result_persist_failures_total is not None:
        return
    meter = _get_otel_meter()
    if meter is None:
        return
    _eval_result_persist_failures_total = meter.create_counter(
        name="modulo_eval_result_persist_failures_total",
        description="EvalResult persistence failures, by failure_behaviour",
        unit="1",
    )


def _record_persist_failure(*, failure_behaviour: str) -> None:
    """Best-effort counter increment on a persistence failure."""
    try:
        _ensure_metrics()
        if _eval_result_persist_failures_total is not None:
            _eval_result_persist_failures_total.add(1, {"failure_behaviour": failure_behaviour})
    except Exception:
        _log.warning("eval_persist_order.metrics_unavailable", exc_info=True)


# ---------------------------------------------------------------------------
# Shared per-eval helper
# ---------------------------------------------------------------------------

# Type alias for the session-factory callable.  Matches the signature used
# by both the executor and the node-runner: ``session_factory()`` returns an
# async context manager yielding an ``AsyncSession``.
SessionFactory = Callable[[], Any]


async def run_evals_persist_before_decide(
    *,
    eval_defs: Sequence[EvalDefDTO],
    resolve_eval_target: Callable[[EvalDefDTO], Any],
    run_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    session_factory: SessionFactory | None = None,
    node_id: str | None = None,
    resolve_llm_judge: Callable[[EvalDefDTO], LLMJudgeCallable | None] | None = None,
    on_eval_result: Callable[[EvalDefDTO, EngineEvalResult], None] | None = None,
) -> dict[str, EngineEvalResult]:
    """Per-eval compute→persist→decide loop (shared by both call sites).

    Each eval is processed independently:
      1. Compute via ``EvalEngine.evaluate_result`` (never raises
         ``EvalBlockedError``).
      2. Persist one ``EvalResult`` row in its **own** transaction (per-eval
         commit).  RLS org and execution context are re-set inside every
         transaction.
      3. Decide — raise ``EvalBlockedError`` if the eval failed and its
         ``failure_behaviour == "block"``.

    When ``session_factory`` is ``None``, ``org_id`` is ``None``, or
    ``run_id`` is ``None``, only the compute and decide steps run —
    persistence is skipped (matching the legacy no-persistence path when
    no session or run id is available).

    Persistence-failure handling (spec §4.1):
      - **block gate** → halt with the existing ``eval_blocked`` error code.
        The error detail carries a machine-parseable persistence-failure
        marker (JSON ``{"persistence_failure": true, "eval_id": "...",
        "failure_behaviour": "block"}``).
      - **warn gate** → log and continue (fail-open); the next eval is
        still processed.

    The helper **always opens its own session** via ``session_factory()``
    (spec §3.7.1 — the HITL call site has an ambient-transaction hazard).

    Args:
        eval_defs: Eval definitions to process sequentially.
        resolve_eval_target: Callback ``eval_def → Any`` that resolves the
            eval target for each definition.  The executor ignores the
            eval_def and returns a pre-resolved target; the node-runner
            calls ``_resolve_gate_eval_target`` per eval.
        run_id: Pipeline run ID.  When ``None``, persistence is skipped
            (no fabricated run id — the absence is intentional).
        org_id: Organisation ID (for RLS context).  When ``None``,
            persistence is skipped.
        session_factory: Callable returning an async context manager for
            ``AsyncSession``.  When ``None``, persistence is skipped.
        node_id: Graph node id (for logging / DB row).
        resolve_llm_judge: Optional callback ``eval_def → LLMJudgeCallable | None``.
            The executor passes ``None`` (no LLM judge); the node-runner
            passes ``_resolve_llm_judge_callable``.
        on_eval_result: Optional per-eval callback invoked after each eval
            is computed.  Signature: ``(eval_def, result) → None``.
            Each call site passes its own callback to emit the appropriate
            structured log event.

    Returns:
        Name→result mapping for all evals that were *computed* (including
        those that triggered a block — their result is persisted before
        the exception).

    Raises:
        EvalBlockedError: When a ``block`` eval fails and persistence
            succeeds.  Also raised on persistence failure for a ``block``
            eval (the detail carries the persistence-failure marker).
    """
    engine = EvalEngine()
    results: dict[str, EngineEvalResult] = {}
    can_persist = session_factory is not None and org_id is not None and run_id is not None
    # Use a stable UUID for the engine compute call when no run_id is
    # available — the engine needs a run_id parameter, but we must NOT
    # use it for persistence (Defect 2 fix: absent run_id ⇒ no persist).
    compute_run_id = run_id if run_id is not None else uuid.uuid4()

    for eval_def in eval_defs:
        # --- 1. Compute (never raises EvalBlockedError) ----------------
        judge = resolve_llm_judge(eval_def) if resolve_llm_judge else None
        eval_target = resolve_eval_target(eval_def)
        eval_result = engine.evaluate_result(
            eval_target,
            eval_def,
            run_id=compute_run_id,
            llm_judge_callable=judge,
        )
        results[eval_def.name] = eval_result

        # Per-eval structured log callback (Defect 3 fix).
        if on_eval_result is not None:
            on_eval_result(eval_def, eval_result)

        # --- 2. Persist (own transaction, per-eval commit) -------------
        if can_persist:
            assert session_factory is not None and org_id is not None and run_id is not None
            try:
                async with session_factory() as session, session.begin():
                    await set_rls_org(session, org_id)
                    await set_rls_execution_context(session)
                    node_uuid: uuid.UUID | None = uuid.UUID(eval_def.node_id) if eval_def.node_id else None
                    session.add(
                        EvalResultModel(
                            organisation_id=org_id,
                            run_id=run_id,
                            node_id=node_uuid,
                            eval_id=eval_def.id,
                            eval_definition_version=eval_def.version,
                            passed=eval_result.passed,
                            score=eval_result.score,
                            detail=eval_result.detail,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # --- Persistence failure (spec §4.1) -------------------
                failure_behaviour = eval_def.failure_behaviour
                _log.exception(
                    "eval_persist_order.persist_failed",
                    extra={
                        "node_id": node_id,
                        "eval_name": eval_def.name,
                        "eval_id": str(eval_def.id),
                        "failure_behaviour": failure_behaviour,
                        "outcome": "persistence_failure",
                    },
                )
                _record_persist_failure(failure_behaviour=failure_behaviour)

                if failure_behaviour == "block":
                    # Halt with the EXISTING block control-flow identity
                    # (eval_blocked).  The distinction lives in the detail
                    # channel: a machine-parseable persistence-failure marker.
                    marker = json.dumps(
                        {
                            "persistence_failure": True,
                            "eval_id": str(eval_def.id),
                            "failure_behaviour": failure_behaviour,
                        }
                    )
                    raise EvalBlockedError(eval_def.name, marker) from None
                # warn → log and continue (fail-open); the next eval is
                # still processed.
                continue

        # --- 3. Decide (after successful persistence) ------------------
        _log.debug(
            "eval_persist_order.persisted",
            extra={
                "eval_name": eval_def.name,
                "eval_id": str(eval_def.id),
                "node_id": node_id,
                "passed": eval_result.passed,
            },
        )
        if not eval_result.passed and eval_def.failure_behaviour == "block":
            raise EvalBlockedError(eval_def.name, eval_result.detail)

    return results
