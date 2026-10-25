"""Per-eval compute→persist→decide helper (FAR-971 chunk 2, FAR-1102 chunk 4).

Implements the persist-before-decide reordering for both the post-node
evaluation loop (executor) and the HITL gate-eval loop (node-runner).

Each eval definition is processed independently:
  1. **Compute** via :meth:`EvalEngine.evaluate_result` (never raises ``EvalBlockedError``).
  2. **Persist** one ``EvalResult`` row in its own transaction (per-eval commit).
  3. **Persist decision record** (FAR-1102 chunk 4) — build a ``PolicyGateDecision``
     row from the ``resolve_policy_gate`` outcome and persist it in its own
     savepoint with a **fail-open** wrapper.  Only when a PolicyGate exists
     for this eval.
  4. **Decide** — raise ``EvalBlockedError`` if the eval failed *and* its
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
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from modulo.core.eval_engine import (
    EvalBlockedError,
    EvalEngine,
    ModelJudgeCallable,
)
from modulo.core.eval_engine import (
    EvalResult as EngineEvalResult,
)
from modulo.core.eval_engine.policy_gate import (
    EvalPolicySnapshot,
    EvalResultView,
    EvalView,
    PolicyGateView,
    build_decision_row,
    resolve_policy_gate,
)
from modulo.db.models.eval_result import EvalResult as EvalResultModel
from modulo.db.rls import set_rls_execution_context, set_rls_org


@dataclass
class EvalDefDTO:
    """Per-eval DTO consumed by EvalEngine.evaluate() and run_evals_persist_before_decide.

    After the FAR-1100 cutover this is built from ``Eval`` + ``PolicyGate``
    (not from ``EvalDefinition``).  The ``failure_behaviour`` field is populated
    from ``PolicyGate.action`` when a gate exists, defaulting to ``"warn"``
    when no gate is present (guardrail-typed Evals and any eval whose binding
    was rejected during backfill).

    Attribute names are identical to the previous Pydantic ``EvalDefinition``
    alias so that existing tests (which pass Pydantic DTOs or duck-typed
    objects as the DTO) continue to work — the engine and helpers only read
    attributes by name.

    PolicyGate metadata (FAR-1102 chunk 4): when a PolicyGate is present for
    this eval, ``policy_gate_id``, ``policy_gate_version``, and
    ``policy_gate_node_id`` carry the gate's own identity for decision-record
    construction.  These are ``None`` when no gate exists (guardrail-typed
    Evals, backfill-rejected bindings).
    """

    id: uuid.UUID
    org_id: uuid.UUID
    pipeline_id: uuid.UUID | None = None
    node_id: str | None = None
    name: str = ""
    eval_type: str = "regex"
    config: dict[str, Any] = field(default_factory=dict)
    failure_behaviour: str = "warn"
    pass_threshold: Decimal | float | None = None
    suite_id: str | None = None
    version: int = 1

    # PolicyGate metadata (FAR-1102 chunk 4) — None when no gate exists.
    policy_gate_id: uuid.UUID | None = None
    policy_gate_version: int | None = None
    policy_gate_node_id: uuid.UUID | None = None
    # FAR-939 chunk 12, extended by FAR-1749 — the governing gate's
    # evaluation mode (``policy_gates.evaluation_mode``): ``"automatic"``
    # (current behaviour) or ``"human"`` (the human's HITL decision is the
    # gate's evaluation result). ``None`` when no gate governs this eval.
    # Read once at DTO assembly (run start) and run-frozen for the run's
    # duration: the snapshot PIN carries the run-start mode for every
    # pinned gate (FAR-1749), so a live flip in EITHER direction does not
    # affect an in-flight run. The fire-time stamp on the HITL claim still
    # covers the already-fired gate and legacy pins. A live DISABLE (or a
    # replaced gate) still wins for an in-flight run — operator control can
    # remove, never add or alter, a gate. Operator-facing limitation:
    # tightening an in-flight run (automatic -> human) requires cancelling
    # and restarting it; that total freeze is the FAR-1749 design.
    policy_gate_evaluation_mode: str | None = None


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metrics — OTel counters for persistence failures and suite completeness
# ---------------------------------------------------------------------------
_eval_result_persist_failures_total: Any = None
_decision_record_persist_failures_total: Any = None


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
    """Lazily initialise the persistence-failure counters (idempotent)."""
    global _eval_result_persist_failures_total, _decision_record_persist_failures_total
    if _eval_result_persist_failures_total is not None and _decision_record_persist_failures_total is not None:
        return
    meter = _get_otel_meter()
    if meter is None:
        return
    if _eval_result_persist_failures_total is None:
        _eval_result_persist_failures_total = meter.create_counter(
            name="modulo_eval_result_persist_failures_total",
            description="EvalResult persistence failures, by failure_behaviour",
            unit="1",
        )
    if _decision_record_persist_failures_total is None:
        _decision_record_persist_failures_total = meter.create_counter(
            name="modulo_policy_gate_decision_persistence_failures_total",
            description="PolicyGateDecision persistence failures, by resolved_action and failure_class",
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
# FAR-1102 chunk 4: decision-record persistence helpers
# ---------------------------------------------------------------------------


def _classify_persistence_failure(exc: BaseException) -> str:
    """Classify a decision-record persistence failure as ``referential`` or ``transient``.

    A referential failure is an ``IntegrityError`` whose constraint name
    (extracted via the shared ``_extract_constraint_name`` helper which
    handles asyncpg ``exc.orig`` and message parsing) matches one of the
    two composite FK constraint names on ``policy_gate_decisions``.
    Everything else is transient.
    """
    from sqlalchemy.exc import IntegrityError

    if isinstance(exc, IntegrityError):
        from modulo.db.crud.policy_gate_decision import (
            _DECISION_FK_CONSTRAINTS,
            _extract_constraint_name,
        )

        constraint_name = _extract_constraint_name(exc)
        if constraint_name is not None:
            if constraint_name in _DECISION_FK_CONSTRAINTS:
                return "referential"
            # Substring fallback for exact name unavailable (e.g. some drivers)
            lower = constraint_name.lower()
            if "policy_gate_decision" in lower:
                return "referential"
    return "transient"


def _record_decision_persist_failure(*, resolved_action: str, failure_class: str) -> None:
    """Best-effort counter increment on a decision-record persistence failure."""
    try:
        _ensure_metrics()
        if _decision_record_persist_failures_total is not None:
            _decision_record_persist_failures_total.add(
                1,
                {"resolved_action": resolved_action, "failure_class": failure_class},
            )
    except Exception:
        _log.warning("eval_persist_order.metrics_unavailable", exc_info=True)


async def _persist_decision_row(
    snapshot: EvalPolicySnapshot,
    outcome: Any,
    run_id: uuid.UUID,
    *,
    session_factory: SessionFactory,
    org_id: uuid.UUID,
) -> None:
    """Persist a PolicyGateDecision row in its own savepoint (fail-OPEN).

    The decision has ALREADY been made and is already propagating.  A
    failure to persist the record must NEVER block, delay, or reverse
    the decision.  This is the OPPOSITE of EvalResult persistence
    (which is fail-CLOSED for block gates, per chunk 2).

    ``CancelledError`` propagates — a cancelled run's missing record
    is acceptable.
    """
    decision_row = build_decision_row(snapshot, outcome, run_id)
    try:
        async with session_factory() as session, session.begin(), session.begin_nested():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            session.add(decision_row)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failure_class = _classify_persistence_failure(exc)
        if failure_class == "referential":
            from modulo.db.crud.policy_gate_decision import _extract_constraint_name

            _log.exception(
                "policy_gate_decision.persist_failed_referential",
                extra={
                    "policy_gate_id": str(snapshot.policy_gate.id),
                    "eval_id": str(snapshot.eval.id),
                    "run_id": str(run_id),
                    "resolved_action": outcome.action,
                    "organisation_id": str(snapshot.policy_gate.organisation_id),
                    "constraint_name": _extract_constraint_name(exc),
                    "exception_message": str(exc),
                    "failure_class": "referential",
                },
            )
        else:
            _log.warning(
                "policy_gate_decision.persist_failed",
                extra={
                    "policy_gate_id": str(snapshot.policy_gate.id),
                    "run_id": str(run_id),
                    "resolved_action": outcome.action,
                    "failure_class": "transient",
                },
            )
        _record_decision_persist_failure(
            resolved_action=outcome.action,
            failure_class=failure_class,
        )
        # The decision propagates normally — the failure is logged, not raised.


# ---------------------------------------------------------------------------
# Shared per-eval helper
# ---------------------------------------------------------------------------

# Type alias for the session-factory callable.  Matches the signature used
# by both the executor and the node-runner: ``session_factory()`` returns an
# async context manager yielding an ``AsyncSession``.
SessionFactory = Callable[[], Any]


# ---------------------------------------------------------------------------
# Per-phase helpers for run_evals_persist_before_decide (S3776 structural
# extraction).  The phases are extracted WITHOUT changing the per-eval
# interleaving: each loop iteration still runs compute → persist → decision
# record → decide, and a block on eval N still prevents eval N+1 from being
# computed at all.
# ---------------------------------------------------------------------------


#: Sentinel for "no pre-resolved judge was supplied".
_UNRESOLVED: Any = object()


def _compute_eval_result(
    eval_def: EvalDefDTO,
    *,
    engine: EvalEngine,
    compute_run_id: uuid.UUID,
    resolve_eval_target: Callable[[EvalDefDTO], Any],
    resolve_model_judge: Callable[[EvalDefDTO], ModelJudgeCallable | None],
    on_eval_result: Callable[[EvalDefDTO, EngineEvalResult], None] | None,
    resolved_judge: Any = _UNRESOLVED,
) -> EngineEvalResult:
    """Phase 1 — compute the eval result (never raises ``EvalBlockedError``)."""
    judge = resolve_model_judge(eval_def) if resolved_judge is _UNRESOLVED else resolved_judge
    eval_target = resolve_eval_target(eval_def)
    eval_result = engine.evaluate_result(
        eval_target,
        eval_def,  # type: ignore[arg-type]  # EvalDefDTO duck-types EvalDefinition
        run_id=compute_run_id,
        model_judge_callable=judge,
    )
    # Per-eval structured log callback (Defect 3 fix).
    if on_eval_result is not None:
        on_eval_result(eval_def, eval_result)
    return eval_result


async def _compute_eval_result_async(
    eval_def: EvalDefDTO,
    *,
    engine: EvalEngine,
    compute_run_id: uuid.UUID,
    resolve_eval_target: Callable[[EvalDefDTO], Any],
    judge: Any,
    on_eval_result: Callable[[EvalDefDTO, EngineEvalResult], None] | None,
) -> EngineEvalResult:
    """Phase 1 for an async-capable judge — awaited on the caller's own loop.

    The pipeline post-node and HITL gate loops both run inside ``asyncio``, so
    an async judge needs no sync bridge and no worker thread. A judge without
    ``ajudge`` never reaches here (the loop keeps the sync path).
    """
    eval_target = resolve_eval_target(eval_def)
    eval_result = await engine.evaluate_result_async(
        eval_target,
        eval_def,  # type: ignore[arg-type]  # EvalDefDTO duck-types EvalDefinition
        run_id=compute_run_id,
        model_judge_callable=judge,
    )
    if on_eval_result is not None:
        on_eval_result(eval_def, eval_result)
    return eval_result


async def _persist_eval_outcome(
    eval_def: EvalDefDTO,
    eval_result: EngineEvalResult,
    *,
    session_factory: SessionFactory,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str | None,
) -> tuple[bool, uuid.UUID | None]:
    """Phase 2 — persist one ``EvalResult`` row in its own transaction.

    Returns ``(True, row_id)`` on success.  On persistence failure
    (spec §4.1): a ``block`` eval raises ``EvalBlockedError`` carrying the
    machine-parseable persistence-failure marker; a ``warn`` eval returns
    ``(False, None)`` (fail-open — the caller skips the decision record and
    decide steps for this eval).  ``CancelledError`` propagates untouched.

    ``row_id`` may be ``None`` on success when the backend has not
    populated the row's ``id`` yet (e.g. mock sessions in tests) — a
    successful persist must NOT be mistaken for the warn-fail-open
    outcome, which is why the boolean is returned alongside.
    """
    try:
        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            node_uuid: uuid.UUID | None = uuid.UUID(eval_def.node_id) if eval_def.node_id else None
            verdict = eval_result.judge_verdict
            eval_result_row = EvalResultModel(
                organisation_id=org_id,
                run_id=run_id,
                node_id=node_uuid,
                eval_id=eval_def.id,
                eval_definition_version=eval_def.version,
                # ``outcome`` is the truth; ``passed`` is its compatibility
                # projection (ADR 066 D5). ``score`` is NULL on error.
                outcome=eval_result.outcome.value,
                passed=eval_result.passed,
                score=eval_result.score,
                judge_result_json=verdict.to_json() if verdict is not None else None,
                judge_confidence_kind=(verdict.confidence_kind.value if verdict is not None else None),
                error_code=(
                    verdict.error_code.value if verdict is not None and verdict.error_code is not None else None
                ),
                detail=eval_result.detail,
            )
            session.add(eval_result_row)
            await session.flush()
            # Capture the id for decision-record construction.
            return True, eval_result_row.id
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
        return False, None


async def _persist_policy_gate_decision(
    eval_def: EvalDefDTO,
    eval_result: EngineEvalResult,
    eval_result_id: uuid.UUID | None,
    *,
    session_factory: SessionFactory,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> None:
    """Phase 2.5 — persist the PolicyGateDecision audit record (fail-OPEN).

    A no-op when no PolicyGate exists for this eval.  The decision is
    resolved by ``resolve_policy_gate``; the row is persisted via
    ``_persist_decision_row`` whose fail-OPEN wrapper is the opposite of
    EvalResult persistence (fail-CLOSED for block gates).
    """
    if eval_def.policy_gate_id is None:
        return
    if eval_def.policy_gate_node_id is None:
        _log.warning(
            "eval_persist_order.policy_gate_node_id_missing",
            extra={
                "eval_id": str(eval_def.id),
                "policy_gate_id": str(eval_def.policy_gate_id),
                "eval_name": eval_def.name,
            },
        )
    snapshot = EvalPolicySnapshot(
        policy_gate=PolicyGateView(
            id=eval_def.policy_gate_id,
            organisation_id=eval_def.org_id,
            version=eval_def.policy_gate_version or 1,
            node_id=eval_def.policy_gate_node_id or uuid.uuid4(),
            action=eval_def.failure_behaviour,
        ),
        eval=EvalView(
            id=eval_def.id,
            organisation_id=eval_def.org_id,
            node_id=uuid.UUID(eval_def.node_id) if eval_def.node_id else None,
            eval_type=eval_def.eval_type,
            deleted_at=None,
        ),
        eval_result=EvalResultView(
            id=eval_result_id,
            passed=eval_result.passed,
        )
        if eval_result_id is not None
        else None,
    )
    outcome = resolve_policy_gate(snapshot)
    await _persist_decision_row(
        snapshot,
        outcome,
        run_id,
        session_factory=session_factory,
        org_id=org_id,
    )


def _decide_after_persist(
    eval_def: EvalDefDTO,
    eval_result: EngineEvalResult,
    *,
    node_id: str | None,
) -> None:
    """Phase 3 — decide (after successful persistence)."""
    _log.debug(
        "eval_persist_order.persisted",
        extra={
            "eval_name": eval_def.name,
            "eval_id": str(eval_def.id),
            "node_id": node_id,
            "passed": eval_result.passed,
            "outcome": eval_result.outcome.value,
        },
    )
    if not eval_result.passed and eval_def.failure_behaviour == "block":
        raise EvalBlockedError(eval_def.name, eval_result.detail)


async def run_evals_persist_before_decide(
    *,
    eval_defs: Sequence[EvalDefDTO],
    resolve_eval_target: Callable[[EvalDefDTO], Any],
    run_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    session_factory: SessionFactory | None = None,
    node_id: str | None = None,
    resolve_model_judge: Callable[[EvalDefDTO], ModelJudgeCallable | None],
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
        resolve_model_judge: REQUIRED callback ``eval_def → ModelJudgeCallable | None``
            (FAR-1652: previously optional, so a call site that simply forgot
            it silently fail-closed every ``model_judge`` eval to 0.0 — the
            exact FAR-315 symptom). Both call sites pass
            ``llm_judge.resolve_model_judge_callable``: the executor's
            post-node path (FAR-315) and the node-runner HITL-gate path
            (FAR-307) — each resolves a real judge callable from
            ``eval_def.config["model_backend_id"]`` via the
            ModelBackendHub. A call site that genuinely has no hub passes an
            explicit ``lambda eval_def: None`` so the omission is visible.
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
        # FAR-209 Track B: guardrail-typed defs never enter the eval loop —
        # the eval engine raises GuardrailMisroutedError on them (a run
        # crash). Interception owns them: the input seam (run creation) and
        # the executor's output seam (_run_output_guardrails). A straggler
        # here is a wiring defect — log it loud and skip (guardrail defs on
        # BOTH call sites arrive through _build_eval_defs_by_node, which the
        # chained-Fix upstream split already routes around).
        if str(eval_def.eval_type) == "guardrail":
            _log.warning(
                "evals.guardrail_typed_def_skipped_in_eval_loop",
                extra={"eval_name": eval_def.name, "node_id": node_id},
            )
            continue

        # FAR-939 chunk 12 (§3.3): an eval governed by a HUMAN-mode
        # PolicyGate is skipped ENTIRELY here — the one skip site both loops
        # (_run_gate_evals and _run_post_node_evals) honour. No compute, no
        # EvalResult row, no PolicyGateDecision row, no EvalBlockedError: the
        # gate's evaluation result is the human's decision at the existing
        # HITL pause (grounded on an Evidence row on resume). Evals governed
        # by automatic gates on the same node still run normally below.
        if getattr(eval_def, "policy_gate_evaluation_mode", None) == "human":
            _log.info(
                "evals.human_governed_eval_skipped",
                extra={
                    "eval_name": eval_def.name,
                    "eval_id": str(eval_def.id),
                    "node_id": node_id,
                    "policy_gate_id": str(eval_def.policy_gate_id),
                },
            )
            continue

        # --- 1. Compute (never raises EvalBlockedError) ----------------
        # An async-capable judge is awaited on this loop (ADR 066 D10); a sync
        # judge keeps the historical path unchanged.
        resolved_judge = resolve_model_judge(eval_def)
        if hasattr(resolved_judge, "ajudge"):
            eval_result = await _compute_eval_result_async(
                eval_def,
                engine=engine,
                compute_run_id=compute_run_id,
                resolve_eval_target=resolve_eval_target,
                judge=resolved_judge,
                on_eval_result=on_eval_result,
            )
        else:
            eval_result = _compute_eval_result(
                eval_def,
                engine=engine,
                compute_run_id=compute_run_id,
                resolve_eval_target=resolve_eval_target,
                resolve_model_judge=resolve_model_judge,
                on_eval_result=on_eval_result,
                resolved_judge=resolved_judge,
            )
        results[eval_def.name] = eval_result

        # --- 2. Persist (own transaction, per-eval commit) -------------
        if can_persist:
            assert session_factory is not None
            assert org_id is not None
            assert run_id is not None
            persisted, eval_result_id = await _persist_eval_outcome(
                eval_def,
                eval_result,
                session_factory=session_factory,
                org_id=org_id,
                run_id=run_id,
                node_id=node_id,
            )
            if not persisted:
                # Warn fail-open: the persist helper already logged and
                # recorded the failure — skip decision record + decide.
                continue

            # --- 2.5. Persist decision record (FAR-1102 chunk 4) -------
            # The helper is a no-op unless a PolicyGate exists for this
            # eval.  The decision is resolved by resolve_policy_gate();
            # the row is an audit trace persisted with a fail-OPEN
            # wrapper (opposite of EvalResult's fail-CLOSED for block
            # gates).
            await _persist_policy_gate_decision(
                eval_def,
                eval_result,
                eval_result_id,
                session_factory=session_factory,
                org_id=org_id,
                run_id=run_id,
            )

        # --- 3. Decide (after successful persistence) ------------------
        _decide_after_persist(eval_def, eval_result, node_id=node_id)

    return results
