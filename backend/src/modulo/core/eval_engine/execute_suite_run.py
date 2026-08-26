"""SuiteRun execution runner — scheduled/event-driven eval execution (FAR-377).

This is the *wiring* layer that turns a ``run_kind='suite_run'`` trigger into an
end-to-end ``SuiteRun`` execution. It REUSES the behaviour layer in
``modulo.core.eval_engine.suite_run`` (baseline resolution, comparison, spend,
notification) and the existing ``EvalEngine.evaluate`` for per-case scoring —
it does NOT reimplement any comparison or scoring logic.

Execution path (given a resolved trigger config):
    1. Load the org-scoped ``EvalSuite``, ``EvalDataset`` (pin ``version``) and
       its active ``EvalDefinition`` rows, plus the pinned ``ModelBackend``.
    2. Refuse an empty dataset (``validate_dataset_has_cases`` == 0) -> fail
       loudly (never a silent pass).
    3. Construct the ``SuiteRun`` with the immutable baseline tuple
       (``build_baseline_tuple``), snapshot the definition checksum +
       dataset version + scenario signature, persist it ``pending``.
    4. Transition ``pending -> running`` (optimistic-lock guarded).
    5. Iterate the active ``EvalCase`` rows -> synthesise each case's output ->
       ``EvalEngine.evaluate`` per definition -> persist each per-case
       ``EvalResult`` with ``suite_run_id`` + the ``eval_definition_version``
       stamp (see FAR-382).
    6. Reconcile pass/failed/excluded counts + claimed cost ledger.
    7. Transition ``running -> completed | partial | failed``.
    8. ``record_completion`` (comparison + regression alerting).

Load-bearing guards:
* **Org isolation** — every query injects an unconditional
  ``organisation_id = :org`` predicate (the BYPASSRLS -> explicit predicate is
  the isolation control); RLS is never relied on alone.
* **Loop guard** — the enforcement is the WRITE SURFACE: a SuiteRun completion
  writes ONLY to ``suite_runs`` and ``eval_results``. It never creates a ``Run``,
  never writes a ``TriggerEvent`` and never writes a
  ``WebhookPayload``/dedup row, so a finished eval can NEVER re-trigger another
  eval through the trigger-watch/dedup event set. ``exclude_eval_families`` and
  ``is_eval_trigger`` are exposed helpers for the (forward) event-watch wiring —
  the cron dispatch routes on ``run_kind == 'suite_run'`` directly, and the
  suite-run execution path writes no watchable event.
* **Separate spend pool** — the ``suite_run`` trigger uses its OWN
  ``daily_spend_limit`` (summed over ``suite_runs``, never over ``runs``) and a
  separate per-suite cumulative ceiling via the row-locked
  ``claim_suite_run_cost`` ledger.
* **Failure sink (monitored)** — an orchestration failure transitions the run to
  ``failed`` and RE-RAISES a typed error (the SAQ ``after_process`` hook sinks it
  to the Error Dashboard); the run is surfaced with ``error_detail`` populated so
  a missed run is never rendered as current.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.eval_engine.suite_run import (
    SuiteRunError,
    build_baseline_tuple,
    claim_suite_run_cost,
    record_completion,
    suite_cumulative_exceeded,
)
from modulo.db.models.eval_dataset import EvalCase, EvalDataset
from modulo.db.models.eval_definition import EvalDefinition as EvalDefinitionRow
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.eval_suite import EvalSuite
from modulo.db.models.eval_suite_run import (
    SuiteRun,
    SuiteRunState,
    can_transition,
    compute_definition_checksum,
    compute_scenario_signature,
)
from modulo.db.models.model_backend import ModelBackend

_log = logging.getLogger(__name__)

# Nominal cost charged to the per-suite ledger per evaluated case when the suite
# contains an llm_judge definition and no explicit ``cost_per_case`` is given.
# Deterministic evals cost nothing. This is an ESTIMATE — real per-token cost
# accounting is the model backend's responsibility.
_DEFAULT_COST_PER_LLM_CASE = Decimal("0.001")

# Eval/feedback event families that must NEVER re-fire a trigger through the
# trigger-watch/dedup event set (loop guard). Selection of the watch set calls
# ``exclude_eval_families`` to drop these.
EVAL_WATCH_EVENT_FAMILIES: frozenset[str] = frozenset(
    {"eval_regression", "eval_blocked", "suite_run", "eval_result", "feedback"}
)


class SuiteRunExecutionError(SuiteRunError):
    """Raised when a SuiteRun orchestration step fails irrecoverably."""


class SuiteRunEmptyDatasetError(SuiteRunExecutionError):
    """Raised when the dataset has zero active cases (never a silent pass)."""


class SuiteRunSpendExceededError(SuiteRunExecutionError):
    """Raised when the suite run would exceed its cost ceiling."""


async def _suite_run_transition(session: AsyncSession, run: SuiteRun, target: SuiteRunState) -> None:
    """Async counterpart of ``transition_state`` (the caller is an ``AsyncSession``).

    ``eval_suite_run.transition_state`` is a SYNC helper (it calls the ORM
    ``session.execute`` without ``await``), so it is unusable from the async SAQ
    path. This replicates its optimistic-lock semantics for ``AsyncSession``:
    illegal edges raise before touching the DB; a version-guarded
    ``UPDATE ... RETURNING`` lands the transition atomically and raises when a
    concurrent writer bumped the version first.
    """
    from sqlalchemy import update

    from modulo.db.models.eval_suite_run import IllegalStateTransitionError, OptimisticLockError

    if run.id is None:
        raise ValueError("_suite_run_transition requires a persisted SuiteRun")
    new_state = SuiteRunState(target) if isinstance(target, str) else target
    if not can_transition(run.state, new_state):
        raise IllegalStateTransitionError(run.state, new_state.value)
    ver = run.version
    values: dict[str, Any] = {"state": new_state.value, "version": SuiteRun.version + 1}
    if new_state in (SuiteRunState.COMPLETED, SuiteRunState.PARTIAL, SuiteRunState.FAILED, SuiteRunState.CANCELLED):
        values["completed_at"] = datetime.now(UTC)
    stmt = (
        update(SuiteRun)
        .where(
            SuiteRun.id == run.id,
            SuiteRun.organisation_id == run.organisation_id,
            SuiteRun.version == ver,
        )
        .values(**values)
        .returning(SuiteRun.version)
    )
    new_version = (await session.execute(stmt)).scalar_one_or_none()
    if new_version is None:
        raise OptimisticLockError(run.id, ver)
    run.state = new_state.value
    run.version = new_version
    if new_state in (SuiteRunState.COMPLETED, SuiteRunState.PARTIAL, SuiteRunState.FAILED, SuiteRunState.CANCELLED):
        run.completed_at = values.get("completed_at")


def is_eval_trigger(trigger: Any) -> bool:
    """Return True when *trigger* fires a SuiteRun instead of a pipeline Run.

    Uses the ``run_kind`` discriminator (``'suite_run'``) OR a bound
    ``eval_suite_id``. This is the discriminator the cron/ongoing dispatch path
    SHOULD route on; the current cron routing uses the ``run_kind`` attribute
    directly, and a future event-watch loop-guard can use this to treat an eval
    trigger as out-of-scope for the production watch set.
    """
    run_kind = getattr(trigger, "run_kind", "run")
    return run_kind == "suite_run" or getattr(trigger, "eval_suite_id", None) is not None


def exclude_eval_families(event_families: Sequence[str] | None) -> set[str]:
    """Drop eval/feedback event families from a watch set (loop guard).

    Returns a new set without any family in :data:`EVAL_WATCH_EVENT_FAMILIES`.
    A caller that selects the production trigger-watch set SHOULD filter it
    through this before deciding what re-fires a trigger, so a
    ``SuiteRun``/``EvalResult`` write can never be observed as a re-fire trigger.
    It is exposed for the forward event-watch wiring; the suite-run execution
    path itself writes no watchable event, so the loop guard currently holds via
    the write surface alone.
    """
    return {e for e in (event_families or []) if e} - EVAL_WATCH_EVENT_FAMILIES


# --------------------------------------------------------------------------- #
# Org-isolated loads (unconditional organisation_id predicate)                #
# --------------------------------------------------------------------------- #
async def load_suite(session: AsyncSession, org_id: uuid.UUID, suite_id: uuid.UUID) -> EvalSuite:
    row = (
        await session.execute(
            select(EvalSuite).where(
                EvalSuite.id == suite_id,
                EvalSuite.organisation_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise SuiteRunExecutionError(f"EvalSuite {suite_id} not found (org {org_id})")
    return row


async def load_dataset(session: AsyncSession, org_id: uuid.UUID, dataset_id: uuid.UUID) -> EvalDataset:
    row = (
        await session.execute(
            select(EvalDataset).where(
                EvalDataset.id == dataset_id,
                EvalDataset.organisation_id == org_id,
                EvalDataset.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise SuiteRunExecutionError(f"EvalDataset {dataset_id} not found (org {org_id})")
    return row


async def load_model_backend(session: AsyncSession, org_id: uuid.UUID, model_backend_id: uuid.UUID) -> ModelBackend:
    row = (
        await session.execute(
            select(ModelBackend).where(
                ModelBackend.id == model_backend_id,
                ModelBackend.organisation_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise SuiteRunExecutionError(f"ModelBackend {model_backend_id} not found (org {org_id})")
    return row


async def load_active_cases(session: AsyncSession, org_id: uuid.UUID, dataset_id: uuid.UUID) -> list[EvalCase]:
    return list(
        (
            await session.execute(
                select(EvalCase).where(
                    EvalCase.dataset_id == dataset_id,
                    EvalCase.organisation_id == org_id,
                    EvalCase.deleted_at.is_(None),
                )
            )
        ).scalars()
    )


async def load_suite_definitions(
    session: AsyncSession, org_id: uuid.UUID, suite_id: uuid.UUID
) -> list[EvalDefinitionRow]:
    return list(
        (
            await session.execute(
                select(EvalDefinitionRow).where(
                    EvalDefinitionRow.eval_suite_id == suite_id,
                    EvalDefinitionRow.organisation_id == org_id,
                    EvalDefinitionRow.deleted_at.is_(None),
                )
            )
        ).scalars()
    )


def _definition_snapshot(rows: Sequence[EvalDefinitionRow]) -> list[dict[str, Any]]:
    """Build the immutable definition snapshot list for the checksum.

    ``config_json`` is snapshotted at creation so a later config edit produces a
    NEW checksum + NEW baseline tuple, never a silent comparison against a
    different contract.
    """
    return [{"id": str(r.id), "eval_type": r.eval_type, "config_json": r.config_json or {}} for r in rows]


def _definition_dto(row: EvalDefinitionRow, org_id: uuid.UUID) -> EvalDefinition:
    """Convert a persisted ``EvalDefinition`` row to the in-memory DTO."""
    return EvalDefinition(
        id=row.id,
        org_id=org_id,
        pipeline_id=row.pipeline_id,
        node_id=row.node_id,
        name=row.name,
        eval_type=EvalType(row.eval_type),
        config=dict(row.config_json or {}),
        failure_behaviour=row.failure_behaviour,
        pass_threshold=float(row.pass_threshold) if row.pass_threshold is not None else None,
        suite_id=str(row.eval_suite_id) if row.eval_suite_id else None,
    )


def _synthesise_output(case: EvalCase, scenario_inputs: dict[str, Any] | None) -> dict[str, Any]:
    """Synthesise the eval output for a dataset case.

    The case payload is exposed under ``output`` (the default ``field`` the eval
    definitions reference) with optional ``scenario`` inputs merged alongside, so
    ``field: "output"`` / ``field: "output.category"`` and scenario-sensitive
    definitions all resolve deterministically.
    """
    return {"output": case.input_payload, "scenario": scenario_inputs or {}}


# --------------------------------------------------------------------------- #
# Baseline tuple + construction                                               #
# --------------------------------------------------------------------------- #
def build_suite_run_tuple(
    *,
    suite_id: uuid.UUID,
    dataset_id: uuid.UUID,
    dataset_version: int,
    definition_snapshots: Sequence[dict[str, Any]],
    model_backend_id: uuid.UUID,
    scenario_inputs: dict[str, Any] | None,
) -> tuple[dict[str, Any], str, str | None]:
    """Return ``(baseline_tuple, definition_checksum, scenario_signature)``.

    These are snapshotted at creation (never live-looked-up). A changed dataset
    version, definition config, or scenario input produces a NEW tuple and a NEW
    baseline, never a corrupted comparison.
    """
    definition_checksum = compute_definition_checksum(list(definition_snapshots))
    scenario_signature = compute_scenario_signature(scenario_inputs)
    eval_definition_ids = [uuid.UUID(s["id"]) for s in definition_snapshots]
    baseline_tuple = build_baseline_tuple(
        suite_id=suite_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        eval_definition_ids=eval_definition_ids,
        definition_checksum=definition_checksum,
        model_backend_id=model_backend_id,
        scenario_signature=scenario_signature,
    )
    return baseline_tuple, definition_checksum, scenario_signature


# --------------------------------------------------------------------------- #
# Per-case evaluation                                                         #
# --------------------------------------------------------------------------- #
def _case_results(
    run: SuiteRun,
    case: EvalCase,
    definitions: Sequence[EvalDefinition],
    engine: EvalEngine,
    llm_judge_callable: Any,
    scenario_inputs: dict[str, Any] | None,
    *,
    eval_definition_version: int,
) -> tuple[list[EvalResult], int]:
    """Evaluate one case against every suite definition.

    Returns ``(results, errored_count)``. An ``llm_judge`` definition without a
    provided judge callable returns a FAIL path (never a silent pass) — the
    runner rejects it up front via ``_has_llm_judge``, so this is defensive. Every
    result is stamped with ``suite_run_id`` and
    ``extra['eval_definition_version']`` (FAR-382 stamp).
    """
    output = _synthesise_output(case, scenario_inputs)
    results: list[EvalResult] = []
    errored = 0
    for definition in definitions:
        try:
            result = engine.evaluate(
                output,
                definition,
                run_id=run.id,
                llm_judge_callable=llm_judge_callable,
            )
        except Exception as exc:
            _log.warning("suite_run case eval errored case=%s definition=%s: %s", case.id, definition.id, exc)
            errored += 1
            continue
        results.append(
            EvalResult(
                organisation_id=run.organisation_id,
                suite_run_id=run.id,
                eval_id=definition.id,
                passed=bool(result.passed),
                score=result.score,
                detail=(result.detail or "")[:2000],
                observed=True,
                eval_definition_version=eval_definition_version,
            )
        )
    return results, errored


def _has_llm_judge(definitions: Sequence[EvalDefinition]) -> bool:
    return any(d.eval_type == EvalType.LLM_JUDGE for d in definitions)


async def _claim_case_cost(session: AsyncSession, run: SuiteRun, cost: Decimal, suite_ceiling: Decimal | None) -> None:
    """Atomically claim a case's cost against the per-suite cumulative ledger.

    The ledger is incremented FIRST (row-locked) then compared against the
    ceiling so a read-check-write race cannot overshoot.
    """
    new_total = await claim_suite_run_cost(session, run, cost)
    if suite_cumulative_exceeded(new_total, suite_ceiling):
        raise SuiteRunSpendExceededError(f"suite cumulative cost ceiling exceeded (claimed: {new_total})")


async def execute_suite_run(
    session: AsyncSession,
    run: SuiteRun,
    *,
    llm_judge_callable: Any = None,
    entity_thresholds: dict[str, Any] | None = None,
    suite_ceiling: Decimal | None = None,
    scenario_inputs: dict[str, Any] | None = None,
    eval_definition_version: int = 1,
    cost_per_llm_case: Decimal = _DEFAULT_COST_PER_LLM_CASE,
) -> dict[str, Any]:
    """Execute a persisted ``SuiteRun`` end-to-end (pending -> terminal).

    Runs a single evaluation pass: iterate active cases, score each against the
    suite's definitions, persist per-case outcomes, reconcile counts + cost,
    and transition to the terminal state. Orchestration errors are caught and
    the run is transitioned to ``failed`` with ``error_detail`` populated (the
    monitored failure sink) — the typed exception is re-raised so the SAQ
    ``after_process`` hook can escalate to the Error Dashboard.

    Returns a stats dict. The caller owns the transaction.
    """
    if suite_ceiling is None and run.extra:
        raw = run.extra.get("suite_ceiling")
        if isinstance(raw, str):
            suite_ceiling = Decimal(raw)
        elif raw is not None:
            suite_ceiling = Decimal(str(raw))
    try:
        # The SAQ ``execute_suite_run`` job hands a run straight from
        # ``build_suite_run`` (via ``fire_suite_run_trigger``) — a ``pending``
        # run that has NOT been moved to ``running`` by a separate step. Self-
        # transition it so the terminal transition below (``running ->
        # completed/partial/failed``) is a legal edge. Without this the runner
        # would try ``pending -> completed`` (illegal) and strand the run in
        # ``pending`` forever — never terminal, never surfaced.
        if run.state == SuiteRunState.PENDING.value:
            await _suite_run_transition(session, run, SuiteRunState.RUNNING)
            await session.flush()

        definitions = await load_suite_definitions(session, run.organisation_id, run.suite_id)
        if not definitions:
            raise SuiteRunExecutionError(f"suite {run.suite_id} has no active eval definitions")

        cases = await load_active_cases(session, run.organisation_id, run.dataset_id)
        if not cases:
            raise SuiteRunEmptyDatasetError(f"dataset {run.dataset_id} has no active cases")

        suite_defs = [_definition_dto(r, run.organisation_id) for r in definitions]
        engine = EvalEngine()
        if _has_llm_judge(suite_defs) and llm_judge_callable is None:
            raise SuiteRunExecutionError(
                "suite contains llm_judge definitions but no judge callable was provided "
                "(wire a ModelBackendHub-backed callable — deterministic evals need none)"
            )

        total_cases = len(cases)
        passed = 0
        failed = 0
        excluded = 0
        claims_llm = _has_llm_judge(suite_defs)

        for case in cases:
            if claims_llm:
                await _claim_case_cost(session, run, cost_per_llm_case, suite_ceiling)
            elif run.claimed_cost is None:
                run.claimed_cost = Decimal(0)

            results, errored = _case_results(
                run,
                case,
                suite_defs,
                engine,
                llm_judge_callable,
                scenario_inputs,
                eval_definition_version=eval_definition_version,
            )
            for result in results:
                session.add(result)

            # ``all(passed_flags)`` is only reached when ``passed_flags`` is
            # non-empty (guarded by ``bool``) — a case that resolved to zero
            # results is never a silent pass. The flags are collected first so
            # the ``all`` is evaluated on a concrete list, never an empty one.
            passed_flags = [r.passed for r in results]
            case_passed = errored == 0 and len(results) == len(suite_defs) and bool(passed_flags) and all(passed_flags)
            if errored:
                excluded += 1
            elif case_passed:
                passed += 1
            else:
                failed += 1

        run.total_cases = total_cases
        run.passed_cases = passed
        run.failed_cases = failed
        run.excluded_case_count = excluded
        # Feed the SEPARATE suite-run daily spend pool. ``suite_run_daily_spend_used``
        # sums ``total_cost_usd`` over the org's ``suite_runs`` (never the production
        # ``runs`` pool); ``claimed_cost`` is the row-locked per-suite ledger the
        # runner already incremented per judge call. Mirroring it here is what makes
        # the daily limit enforceable — without it the pool always reads 0 and the
        # ``fire_suite_run_trigger`` daily guard is vacuous.
        run.total_cost_usd = run.claimed_cost or Decimal(0)

        # ``partial`` means SOME CASES ERRORED (excluded). A run that executed
        # every case — even one where evals failed — is ``completed``; failed
        # evals are counted in ``failed_cases`` and feed the pass-rate, not the
        # state machine. ``failed`` is reserved for an orchestration error.
        target = SuiteRunState.PARTIAL if excluded else SuiteRunState.COMPLETED

        await _suite_run_transition(session, run, target)
        await session.flush()

        # Only a fully-completed run participates in baseline comparison/alerting.
        if target == SuiteRunState.COMPLETED:
            await record_completion(session, run, entity_thresholds or {})

        return {
            "state": target.value,
            "total_cases": run.total_cases,
            "passed_cases": run.passed_cases,
            "failed_cases": run.failed_cases,
            "excluded_case_count": run.excluded_case_count,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        }
    except SuiteRunExecutionError as exc:
        await _fail_run(session, run, str(exc))
        raise
    except SQLAlchemyError as exc:
        # Raw DB errors (IntegrityError/ProgrammingError/...) previously escaped
        # this handler UNCAUGHT — the run was never terminalised and the error
        # surfaced only via the after_process sink. Map them to a stable status
        # code and route them through ``_fail_run`` so they also terminalise the
        # run, then re-raise a typed orchestration error (the caller — the SAQ
        # job — persists the failure in its OWN committed transaction).
        detail = f"{_sql_error_status(exc)}: {exc}"
        await _fail_run(session, run, detail)
        raise SuiteRunExecutionError(detail) from exc


async def _fail_run(session: AsyncSession, run: SuiteRun, detail: str) -> None:
    """Terminalise the run as ``failed`` + populate the monitored failure sink.

    Handles BOTH the caller-owned-transaction case (a run already ``running``
    when orchestration failed) AND the SAQ-after-rollback case (the run is
    re-read fresh and is ``pending`` again because the rolled-back transaction
    discarded the ``pending -> running`` transition). ``pending`` only allows
    the edge ``{running, cancelled}``, so a ``pending`` run is promoted to
    ``running`` first — otherwise the ``failed`` edge would be illegal and the
    run would stay stranded ``pending``. This is the transaction-boundary fix
    (FAR-377): a persistent failure must land ``failed`` in a transaction that
    survives the caller's rollback.

    The Error-Ingestion sink is best-effort AND isolated in a savepoint, so a
    broken sink (transient DB error, missing table) rolls back only the sink and
    can never roll back the terminal ``failed`` transition below it.
    """
    run.error_detail = detail[:2000]
    try:
        if run.state == SuiteRunState.PENDING.value:
            # A fresh re-read after a rolled-back transaction is PENDING again;
            # promote to RUNNING so the FAILED edge below is a legal transition.
            await _suite_run_transition(session, run, SuiteRunState.RUNNING)
            await session.flush()
        await _suite_run_transition(session, run, SuiteRunState.FAILED)
        await session.flush()
    except Exception:
        _log.exception("suite_run fail transition failed run=%s", run.id)

    # Monitored sink: escalate to the Error Dashboard (best-effort — the run is
    # already transitioning; a sink failure must not fail the run write). The
    # savepoint isolates the sink so a failure here can never roll back the
    # terminal transition in the same transaction.
    try:
        from modulo.core.error_tracking import ErrorIngestionService

        async with session.begin_nested():
            await ErrorIngestionService().ingest(
                session,
                run.organisation_id,
                {
                    "level": "error",
                    "message": f"SuiteRun {run.id} failed: {run.error_detail or detail}",
                    "source": "suite_run",
                    "context_json": {
                        "suite_id": str(run.suite_id),
                        "dataset_id": str(run.dataset_id),
                        "suite_run_id": str(run.id),
                    },
                },
            )
        await session.flush()
    except Exception:
        _log.exception("suite_run failure sink ingest failed run=%s", run.id)


def _sql_error_status(exc: Exception) -> int:
    """Map a raw DB/exception to a stable status code (codebase convention).

    ``IntegrityError``->409, ``ProgrammingError``->501, other ``SQLAlchemyError``
    (e.g. ``OperationalError``)->503, and any other ``Exception``->500. Used to
    label a SuiteRun orchestration failure (stored in ``error_detail``) so the
    Error Dashboard / after_process sink shows a consistent code.
    """
    from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

    if isinstance(exc, IntegrityError):
        return 409
    if isinstance(exc, ProgrammingError):
        return 501
    if isinstance(exc, OperationalError):
        return 503
    if isinstance(exc, SQLAlchemyError):
        return 503
    return 500


def _failure_detail(exc: Exception) -> str:
    """Derive the ``error_detail`` message for a SuiteRun orchestration failure.

    A typed ``SuiteRunExecutionError`` carries its own message; any other
    exception (a raw DB error, a job-level failure) is surfaced with a mapped
    status code, satisfying the "always terminalise with a labelled error"
    contract.
    """
    if isinstance(exc, SuiteRunExecutionError):
        return str(exc)
    return f"{_sql_error_status(exc)}: {exc}"


async def build_suite_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    suite_id: uuid.UUID,
    dataset_id: uuid.UUID,
    model_backend_id: uuid.UUID,
    scenario_inputs: dict[str, Any] | None = None,
    pipeline_id: uuid.UUID | None = None,
    owner_team_id: uuid.UUID | None = None,
) -> SuiteRun:
    """Construct and persist a ``pending`` SuiteRun with its immutable tuple.

    Loads the suite + dataset (pins ``version``) + definitions + model backend
    (all org-scoped), snapshots the baseline tuple, and inserts the run. The
    caller owns the transaction. Raises ``SuiteRunEmptyDatasetError`` when the
    dataset is empty (never a silent pass).
    """
    # ``load_suite`` validates the suite is org-scoped (raises otherwise); the
    # dataset pins the corpus version and the definitions pin the checksum.
    await load_suite(session, org_id, suite_id)
    dataset = await load_dataset(session, org_id, dataset_id)
    await load_model_backend(session, org_id, model_backend_id)
    definitions = await load_suite_definitions(session, org_id, suite_id)
    if not definitions:
        raise SuiteRunExecutionError(f"suite {suite_id} has no active eval definitions")
    definition_snapshots = _definition_snapshot(definitions)

    active_cases = await load_active_cases(session, org_id, dataset_id)
    if not active_cases:
        raise SuiteRunEmptyDatasetError(f"dataset {dataset_id} has no active cases")

    baseline_tuple, definition_checksum, scenario_signature = build_suite_run_tuple(
        suite_id=suite_id,
        dataset_id=dataset_id,
        dataset_version=dataset.version,
        definition_snapshots=definition_snapshots,
        model_backend_id=model_backend_id,
        scenario_inputs=scenario_inputs,
    )

    import uuid as _uuid

    run = SuiteRun(
        id=_uuid.uuid4(),
        organisation_id=org_id,
        owner_team_id=owner_team_id,
        suite_id=suite_id,
        dataset_id=dataset_id,
        dataset_version=dataset.version,
        definition_checksum=definition_checksum,
        model_backend_id=model_backend_id,
        scenario_signature=scenario_signature,
        baseline_tuple=baseline_tuple,
        state=SuiteRunState.PENDING.value,
        version=0,
        total_cases=0,
        passed_cases=0,
        failed_cases=0,
        excluded_case_count=0,
        claimed_cost=Decimal(0),
    )
    # Audit: stamp the owning pipeline id (the trigger's pipeline) so a scheduled
    # SuiteRun is traceable to its pipeline even though it writes no production Run.
    run.extra = {"pipeline_id": str(pipeline_id)} if pipeline_id is not None else None
    session.add(run)
    await session.flush()
    return run


async def run_scheduled_suite(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    suite_id: uuid.UUID,
    dataset_id: uuid.UUID,
    model_backend_id: uuid.UUID,
    pipeline_id: uuid.UUID | None = None,
    scenario_inputs: dict[str, Any] | None = None,
    entity_thresholds: dict[str, Any] | None = None,
    llm_judge_callable: Any = None,
    eval_definition_version: int = 1,
    cost_per_llm_case: Decimal = _DEFAULT_COST_PER_LLM_CASE,
) -> SuiteRun:
    """Build + execute a scheduled SuiteRun end-to-end (pending -> terminal).

    This is the entry point the SAQ ``execute_suite_run`` job calls with a
    resolved trigger config. Returns the terminal run; callers read
    ``run.state`` + the stats from ``run.extra['execution']``.
    """
    run = await build_suite_run(
        session,
        org_id=org_id,
        suite_id=suite_id,
        dataset_id=dataset_id,
        model_backend_id=model_backend_id,
        scenario_inputs=scenario_inputs,
        pipeline_id=pipeline_id,
    )
    await _suite_run_transition(session, run, SuiteRunState.RUNNING)
    await session.flush()
    stats = await execute_suite_run(
        session,
        run,
        llm_judge_callable=llm_judge_callable,
        entity_thresholds=entity_thresholds,
        scenario_inputs=scenario_inputs,
        eval_definition_version=eval_definition_version,
        cost_per_llm_case=cost_per_llm_case,
    )
    run.extra = dict(run.extra or {})
    run.extra["execution"] = stats
    return run


def suite_run_daily_spend_exceeded(current_daily_used: Decimal, daily_limit: Decimal | None) -> bool:
    """True when the SuiteRun daily spend already meets a limit.

    This is the SEPARATE spend pool counter — it is fed by ``suite_runs`` cost,
    never by production ``runs``. ``None`` limit = unlimited.
    """
    if daily_limit is None:
        return False
    return current_daily_used >= daily_limit


async def suite_run_daily_spend_used(session: AsyncSession, org_id: uuid.UUID) -> Decimal:
    """Sum today's ``suite_runs`` cost for *org_id* (the suite-run spend pool)."""
    from modulo.core.cost_controller import created_at_day_start

    today_start = created_at_day_start()
    value = (
        await session.execute(
            select(func.coalesce(func.sum(SuiteRun.total_cost_usd), 0)).where(
                SuiteRun.organisation_id == org_id,
                SuiteRun.created_at >= today_start,
            )
        )
    ).scalar_one()
    return Decimal(str(value or 0))


async def suite_run_daily_spend_used_for_trigger(
    session: AsyncSession, org_id: uuid.UUID, trigger_id: uuid.UUID
) -> Decimal:
    """Sum today's ``suite_runs`` cost for one *trigger_id* (per-trigger pool).

    ``daily_spend_limit`` is configured PER TRIGGER, so the daily suite-run
    spend pool must be scoped to the trigger that owns the run, not summed over
    the whole org. The owning trigger id is stamped onto ``suite_runs.extra`` at
    fire time (see ``fire_suite_run_trigger``), so we sum ``total_cost_usd`` over
    rows whose ``extra->>'trigger_id'`` matches — matching production's per-trigger
    ``daily_spend_limit`` semantics on the ``runs`` table (``_spend_limit_skip``).
    """
    from modulo.core.cost_controller import created_at_day_start

    today_start = created_at_day_start()
    value = (
        await session.execute(
            select(func.coalesce(func.sum(SuiteRun.total_cost_usd), 0)).where(
                SuiteRun.organisation_id == org_id,
                SuiteRun.created_at >= today_start,
                SuiteRun.extra.op("->>")("trigger_id") == str(trigger_id),
            )
        )
    ).scalar_one()
    return Decimal(str(value or 0))


def suite_run_daily_spend_exceeded_for_org(current_daily_used: Decimal, daily_limit: Decimal | None) -> bool:
    """Alias kept for call-site clarity (separate pool, not production pool)."""
    return suite_run_daily_spend_exceeded(current_daily_used, daily_limit)


__all__ = [
    "EVAL_WATCH_EVENT_FAMILIES",
    "SuiteRunEmptyDatasetError",
    "SuiteRunExecutionError",
    "SuiteRunSpendExceededError",
    "build_suite_run",
    "build_suite_run_tuple",
    "exclude_eval_families",
    "execute_suite_run",
    "is_eval_trigger",
    "load_active_cases",
    "load_dataset",
    "load_model_backend",
    "load_suite",
    "load_suite_definitions",
    "run_scheduled_suite",
    "suite_run_daily_spend_exceeded",
    "suite_run_daily_spend_exceeded_for_org",
    "suite_run_daily_spend_used",
    "suite_run_daily_spend_used_for_trigger",
]
