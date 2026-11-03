"""System Maps improvement-loop admission and dispatch runtime (FAR-2349).

The published improvement loop is an autonomous sidecar: it observes a signal
and dispatches its ``action_pipeline_id`` when the signal warrants it. What
stops it becoming a runaway is *admission* — the single transaction that decides
whether a loop may start a run at all (``docs/system-maps.md`` §4.4).

This module is that runtime:

* :func:`admit_loop_run` — one transaction, canonical lock order, every gate
  (publication, active pipeline, attribution binding, declared autonomy,
  cooldown, concurrency, monthly budget). It admits by creating the run and its
  snapshot, or rejects by persisting a deterministic ``rejection_reason``. The
  same ``admission_key`` never admits twice.
* :func:`settle_loop_reservation_for_run` (in repositories) — the reservation
  lifecycle: a ``complete`` run consumes its reservation, every other terminal
  status releases it, and the immutable cooldown boundary survives either way.
* :func:`dispatch_due_loops` — the SAQ periodic task's work function. It selects
  published loops whose observation window has elapsed, admits them, dispatches
  the created run through the EXISTING ``core.dispatch.dispatch_run`` path, and
  reconciles reservations whose runs terminated before they could be settled.

Cadence: registered as the ``system_map_loop_dispatch`` system cron in
``core.saq_worker`` on a five-minute tick. A loop's own
``observation_window_seconds`` (minimum 60s) is the real rate limit, and the
per-tick admission key is the window bucket, so a tick that lands twice in one
window replays the first decision instead of admitting a second run.

Declared autonomy: a loop dispatches automatically only when its action
pipeline declares that it may act unattended. The effective level is
``earned_autonomy_level or default_autonomy_level`` (ADR 043); ``manual_approval``
refuses with ``autonomy``, because a pipeline that declares human approval
before acting cannot be the target of an automatic dispatch.

Deliberately NOT here: a second executor. A dispatched loop run is an ordinary
pending run on the existing SAQ queue, so HITL gates, cost control, retries and
the rest of the run lifecycle apply unchanged.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.system_map import (
    SystemMap,
    SystemMapAttributionBinding,
    SystemMapLoopAdmissionAttempt,
    SystemMapLoopConfig,
    SystemMapLoopReservation,
    SystemMapRevision,
)
from modulo.db.repositories.system_map import (
    SystemMapRepository,
    settle_loop_reservation_for_run,
)
from modulo.db.soft_delete import include_soft_deleted

_log = logging.getLogger(__name__)


def _unused_session_factory() -> AsyncGenerator[AsyncSession, None]:
    """Fail loudly if a repository method uses the factory instead of a session.

    Every method this module calls takes an explicit session; the shared
    repository base only requires a factory to construct.
    """
    raise RuntimeError("system_map_loop_admission requires an explicit session")


_REPO = SystemMapRepository(session_factory=_unused_session_factory)

#: The closed rejection vocabulary. MUST stay in step with migration 0348's
#: ``ck_loop_admission_attempts_reason`` and the model's
#: ``_VALID_REJECTION_REASONS``.
REJECTION_ATTRIBUTION = "attribution"
REJECTION_AUTONOMY = "autonomy"
REJECTION_BUDGET = "budget"
REJECTION_CONCURRENCY = "concurrency"
REJECTION_COOLDOWN = "cooldown"
REJECTION_INACTIVE_PIPELINE = "inactive_pipeline"
REJECTION_UNATTRIBUTED = "unattributed"
REJECTION_UNPUBLISHED_REVISION = "unpublished_revision"
REJECTION_REASONS: frozenset[str] = frozenset(
    {
        REJECTION_ATTRIBUTION,
        REJECTION_AUTONOMY,
        REJECTION_BUDGET,
        REJECTION_CONCURRENCY,
        REJECTION_COOLDOWN,
        REJECTION_INACTIVE_PIPELINE,
        REJECTION_UNATTRIBUTED,
        REJECTION_UNPUBLISHED_REVISION,
    }
)

STATUS_ADMITTED = "admitted"
STATUS_REJECTED = "rejected"

#: Audit event emitted once per NEW admission decision (a replay is not a new
#: decision and emits nothing).
AUDIT_EVENT_ADMISSION = "system_map_loop_admission"

#: The autonomy level that forbids unattended action. A pipeline declaring this
#: requires a human before it acts, so a loop may never dispatch it.
_AUTONOMY_MANUAL = "manual_approval"

#: Loop runs are ordinary runs; the existing trigger vocabulary has no
#: loop-specific value, so they carry ``cron`` (a scheduled, trigger-less
#: autonomous dispatch). ``trigger_id`` stays NULL — there is no trigger row.
LOOP_RUN_TRIGGER_TYPE = "cron"

#: The run's synthetic input payload key carrying the loop context. It is
#: informational only; it is not used for attribution (the binding is).
_LOOP_PAYLOAD_KEY = "system_map_loop"

_MIN_RESERVED_BUDGET = Decimal("0.000001")


class LoopConfigNotFoundError(Exception):
    """The loop configuration is not visible in this organisation."""


@dataclass(frozen=True)
class LoopAdmissionDecision:
    """The durable outcome of one admission attempt."""

    loop_config_id: uuid.UUID
    attempt_id: uuid.UUID
    status: str
    run_id: uuid.UUID | None = None
    reservation_id: uuid.UUID | None = None
    rejection_reason: str | None = None
    admission_key: str = ""
    replay: bool = False

    @property
    def admitted(self) -> bool:
        """True when this decision admitted (or replayed an admitted) run."""
        return self.status == STATUS_ADMITTED and self.run_id is not None


def utc_month_start(moment: datetime) -> date:
    """Return the UTC calendar month start for *moment*."""
    return moment.astimezone(UTC).date().replace(day=1)


def _as_utc(value: datetime) -> datetime:
    """Normalise a datetetime read back from the database to an aware UTC value.

    Postgres returns tz-aware ``timestamptz`` values; the in-memory SQLite schema
    used by the unit tests drops the offset. Normalising keeps the window
    comparison correct on both.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def default_reserved_budget(loop: SystemMapLoopConfig) -> Decimal:
    """Return the budget one admission reserves for the month.

    The loop contract gives a monthly cap but no per-run price, so the
    reservation is ``budget_limit_usd / max_concurrent_runs``, rounded UP to the
    storage scale: the loop's full concurrency may reserve the cap and no more,
    and an admitted run can never overspend past the remaining budget. Once the
    run terminalises, the observed cost replaces the reservation (see
    :func:`settle_loop_reservation_for_run`), so later admissions are priced against
    reality rather than the estimate.
    """
    slots = max(loop.max_concurrent_runs, 1)
    amount = (Decimal(loop.budget_limit_usd) / Decimal(slots)).quantize(Decimal("0.000001"), rounding=ROUND_UP)
    return max(amount, _MIN_RESERVED_BUDGET)


def _effective_autonomy(pipeline: Pipeline) -> str:
    """Return the pipeline's effective autonomy level (ADR 043 fallback)."""
    return pipeline.earned_autonomy_level or pipeline.default_autonomy_level or _AUTONOMY_MANUAL


async def _get_locked_loop(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    loop_config_id: uuid.UUID,
) -> tuple[SystemMap, SystemMapRevision, SystemMapLoopConfig, Pipeline | None]:
    """Lock map -> revision -> loop config -> action pipeline in canonical order.

    The parent ids are resolved with an unlocked read, then every level is
    locked in the documented total order (``system_maps``, revision, loop
    config, action pipeline). The locked rows are the decision's evidence: a
    parent that moved between the resolution read and the map lock fails the
    re-verification below, which is a fail-closed ``unpublished_revision``
    rejection rather than an admission against stale state.
    """
    resolved = (
        await session.execute(
            select(SystemMapLoopConfig.revision_id).where(
                SystemMapLoopConfig.id == loop_config_id,
                SystemMapLoopConfig.organisation_id == organisation_id,
                SystemMapLoopConfig.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if resolved is None:
        raise LoopConfigNotFoundError(str(loop_config_id))
    map_id = (
        await session.execute(
            select(SystemMapRevision.system_map_id).where(
                SystemMapRevision.id == resolved,
                SystemMapRevision.organisation_id == organisation_id,
                SystemMapRevision.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if map_id is None:
        raise LoopConfigNotFoundError(str(loop_config_id))

    map_row = (
        await session.execute(
            select(SystemMap)
            .where(
                SystemMap.id == map_id,
                SystemMap.organisation_id == organisation_id,
                SystemMap.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if map_row is None:
        raise LoopConfigNotFoundError(str(loop_config_id))
    revision_row = (
        await session.execute(
            select(SystemMapRevision)
            .where(
                SystemMapRevision.id == resolved,
                SystemMapRevision.organisation_id == organisation_id,
                SystemMapRevision.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    loop_row = (
        await session.execute(
            select(SystemMapLoopConfig)
            .where(
                SystemMapLoopConfig.id == loop_config_id,
                SystemMapLoopConfig.organisation_id == organisation_id,
                SystemMapLoopConfig.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if loop_row is None:
        raise LoopConfigNotFoundError(str(loop_config_id))
    pipeline_row = (
        await session.execute(
            select(Pipeline)
            .where(
                Pipeline.id == loop_row.action_pipeline_id,
                Pipeline.organisation_id == organisation_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if revision_row is None:
        raise LoopConfigNotFoundError(str(loop_config_id))
    return map_row, revision_row, loop_row, pipeline_row


async def _existing_attempt(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    loop_config_id: uuid.UUID,
    admission_key: str,
) -> SystemMapLoopAdmissionAttempt | None:
    """Return the decision already recorded for this key, if any."""
    return (
        await session.execute(
            include_soft_deleted(
                select(SystemMapLoopAdmissionAttempt).where(
                    SystemMapLoopAdmissionAttempt.organisation_id == organisation_id,
                    SystemMapLoopAdmissionAttempt.loop_config_id == loop_config_id,
                    SystemMapLoopAdmissionAttempt.idempotency_key == admission_key,
                )
            )
        )
    ).scalar_one_or_none()


async def _reservation_for_attempt(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    attempt_id: uuid.UUID,
) -> SystemMapLoopReservation | None:
    """Return the reservation created by an admission attempt, if admitted."""
    return (
        await session.execute(
            include_soft_deleted(
                select(SystemMapLoopReservation).where(
                    SystemMapLoopReservation.organisation_id == organisation_id,
                    SystemMapLoopReservation.admission_attempt_id == attempt_id,
                )
            )
        )
    ).scalar_one_or_none()


def _replay_decision(
    attempt: SystemMapLoopAdmissionAttempt,
    reservation: SystemMapLoopReservation | None,
    admission_key: str,
) -> LoopAdmissionDecision:
    """Reconstruct the original decision for an idempotent replay."""
    return LoopAdmissionDecision(
        loop_config_id=attempt.loop_config_id,
        attempt_id=attempt.id,
        status=attempt.status,
        run_id=attempt.run_id,
        reservation_id=reservation.id if reservation is not None else None,
        rejection_reason=attempt.rejection_reason,
        admission_key=admission_key,
        replay=True,
    )


async def _audit_decision(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    loop: SystemMapLoopConfig,
    decision: LoopAdmissionDecision,
    reserved_budget_usd: Decimal | None,
    month_start: date,
) -> None:
    """Append the admission-decision audit event (best-effort, fail-open).

    Runs in a savepoint of the admission transaction so a broken audit chain can
    never roll back a decision that was correctly recorded; the failure is
    logged loudly. Every NEW decision is audited — admitted or rejected.
    """
    from modulo.core.audit_logger import append_audit_event

    try:
        async with session.begin_nested():
            await append_audit_event(
                session,
                org_id=organisation_id,
                event_type=AUDIT_EVENT_ADMISSION,
                actor_user_id=None,
                resource_type="system_map_loop_config",
                resource_id=loop.id,
                payload_json={
                    "decision": decision.status,
                    "attempt_id": str(decision.attempt_id),
                    "admission_key": decision.admission_key,
                    "run_id": str(decision.run_id) if decision.run_id else None,
                    "reservation_id": str(decision.reservation_id) if decision.reservation_id else None,
                    "rejection_reason": decision.rejection_reason,
                    "target_signal": loop.target_signal,
                    "month_start": month_start.isoformat(),
                    "reserved_budget_usd": (str(reserved_budget_usd) if reserved_budget_usd is not None else None),
                },
            )
    except Exception:
        _log.exception(
            "system_map_loop_admission.audit_failed loop=%s attempt=%s decision=%s",
            loop.id,
            decision.attempt_id,
            decision.status,
        )


async def _reject(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    loop: SystemMapLoopConfig,
    attempt: SystemMapLoopAdmissionAttempt,
    reason: str,
    admission_key: str,
    month_start: date,
    reserved_budget_usd: Decimal | None = None,
) -> LoopAdmissionDecision:
    """Persist a rejected decision with its deterministic reason and audit it."""
    if reason not in REJECTION_REASONS:  # defensive: the CHECK would reject it
        raise ValueError(f"unknown loop rejection reason: {reason!r}")
    await _REPO.update_admission_attempt_decision(
        session,
        organisation_id=organisation_id,
        attempt_id=attempt.id,
        status=STATUS_REJECTED,
        rejection_reason=reason,
    )
    decision = LoopAdmissionDecision(
        loop_config_id=loop.id,
        attempt_id=attempt.id,
        status=STATUS_REJECTED,
        rejection_reason=reason,
        admission_key=admission_key,
    )
    await _audit_decision(
        session,
        organisation_id=organisation_id,
        loop=loop,
        decision=decision,
        reserved_budget_usd=reserved_budget_usd,
        month_start=month_start,
    )
    _log.info(
        "system_map_loop_admission.rejected loop=%s key=%s reason=%s",
        loop.id,
        admission_key,
        reason,
    )
    return decision


def _publication_failure_reason(
    map_row: SystemMap,
    revision_row: SystemMapRevision,
    loop: SystemMapLoopConfig,
) -> str | None:
    """Return the fail-closed reason when the published-loop predicate fails."""
    if map_row.published_revision_id != revision_row.id:
        return REJECTION_UNPUBLISHED_REVISION
    if revision_row.status != "published":
        return REJECTION_UNPUBLISHED_REVISION
    if revision_row.system_map_id != map_row.id:
        return REJECTION_UNPUBLISHED_REVISION
    if loop.revision_id != revision_row.id:
        return REJECTION_UNPUBLISHED_REVISION
    return None


def _pipeline_failure_reason(pipeline: Pipeline | None) -> str | None:
    """Return the fail-closed reason when the action pipeline cannot run."""
    if pipeline is None:
        return REJECTION_INACTIVE_PIPELINE
    if pipeline.archived_at is not None:
        return REJECTION_INACTIVE_PIPELINE
    if not pipeline.run_enabled:
        return REJECTION_INACTIVE_PIPELINE
    return None


async def _attribution_missing(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    loop: SystemMapLoopConfig,
) -> bool:
    """True when the loop has no locked ``action_pipeline`` attribution binding."""
    binding_id = (
        await session.execute(
            select(SystemMapAttributionBinding.id)
            .where(
                SystemMapAttributionBinding.organisation_id == organisation_id,
                SystemMapAttributionBinding.pipeline_id == loop.action_pipeline_id,
                SystemMapAttributionBinding.node_key == loop.node_key,
                SystemMapAttributionBinding.binding_kind == "action_pipeline",
                SystemMapAttributionBinding.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    return binding_id is None


async def _admit(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    loop: SystemMapLoopConfig,
    attempt: SystemMapLoopAdmissionAttempt,
    admission_key: str,
    month_start: date,
    reserved_budget_usd: Decimal,
    input_payload: Mapping[str, Any] | None,
) -> LoopAdmissionDecision:
    """Create the run + snapshot, record the admitted attempt and reservation."""
    from modulo.core.exceptions import PipelineNotRunnableError, TriggersPausedError
    from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
    from modulo.db.crud.run import create_run

    snapshot = await create_snapshot_from_live_graph(
        session,
        pipeline_id=loop.action_pipeline_id,
        account_id=None,
    )
    if snapshot is None:
        return await _reject(
            session,
            organisation_id=organisation_id,
            loop=loop,
            attempt=attempt,
            reason=REJECTION_INACTIVE_PIPELINE,
            admission_key=admission_key,
            month_start=month_start,
            reserved_budget_usd=reserved_budget_usd,
        )

    payload = {
        _LOOP_PAYLOAD_KEY: {
            "loop_config_id": str(loop.id),
            "node_key": loop.node_key,
            "target_signal": loop.target_signal,
            "outcome_metric": loop.outcome_metric,
            "admission_key": admission_key,
        }
    }
    if input_payload:
        payload.update(dict(input_payload))
    try:
        run = await create_run(
            session,
            org_id=organisation_id,
            pipeline_id=loop.action_pipeline_id,
            snapshot_id=snapshot.id,
            trigger_type=LOOP_RUN_TRIGGER_TYPE,
            trigger_id=None,
            input_payload=payload,
        )
    except (TriggersPausedError, PipelineNotRunnableError) as exc:
        # The single run-creation gate refused: the loop is not dispatched. The
        # reason is deterministic and persisted; nothing is silently dropped.
        _log.info(
            "system_map_loop_admission.dispatch_refused loop=%s reason=%s",
            loop.id,
            type(exc).__name__,
        )
        return await _reject(
            session,
            organisation_id=organisation_id,
            loop=loop,
            attempt=attempt,
            reason=REJECTION_INACTIVE_PIPELINE,
            admission_key=admission_key,
            month_start=month_start,
            reserved_budget_usd=reserved_budget_usd,
        )

    await _REPO.update_admission_attempt_decision(
        session,
        organisation_id=organisation_id,
        attempt_id=attempt.id,
        status=STATUS_ADMITTED,
        run_id=run.id,
    )
    reservation = await _REPO.create_reservation(
        session,
        organisation_id=organisation_id,
        loop_config_id=loop.id,
        run_id=run.id,
        idempotency_key=admission_key,
        cooldown_until=datetime.now(UTC) + timedelta(seconds=loop.cooldown_seconds),
        month_start=month_start,
        reserved_budget_usd=reserved_budget_usd,
        admission_attempt_id=attempt.id,
    )
    decision = LoopAdmissionDecision(
        loop_config_id=loop.id,
        attempt_id=attempt.id,
        status=STATUS_ADMITTED,
        run_id=run.id,
        reservation_id=reservation.id,
        admission_key=admission_key,
    )
    await _audit_decision(
        session,
        organisation_id=organisation_id,
        loop=loop,
        decision=decision,
        reserved_budget_usd=reserved_budget_usd,
        month_start=month_start,
    )
    _log.info(
        "system_map_loop_admission.admitted loop=%s key=%s run=%s budget=%s",
        loop.id,
        admission_key,
        run.id,
        reserved_budget_usd,
    )
    return decision


async def admit_loop_run(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    loop_config_id: uuid.UUID,
    admission_key: str,
    input_payload: Mapping[str, Any] | None = None,
    reserved_budget_usd: Decimal | None = None,
    now: datetime | None = None,
) -> LoopAdmissionDecision:
    """Decide (and, when admitted, start) one loop run in ONE transaction.

    The caller owns the transaction — the run, its snapshot, the attempt and the
    reservation commit together, so a reservation can never exist without the
    run it funds. The caller dispatches the returned ``run_id`` after commit.

    Gate order (deterministic when several fail): publication -> active pipeline
    -> attribution -> declared autonomy -> cooldown -> concurrency -> budget.
    """
    map_row, revision_row, loop, pipeline = await _get_locked_loop(
        session,
        organisation_id=organisation_id,
        loop_config_id=loop_config_id,
    )

    # Resolve the decision time AFTER the locks are held: a caller that waited
    # behind a concurrent admission must evaluate the cooldown against the clock
    # as of its own turn, not the instant it entered the function (a zero-length
    # cooldown would otherwise look active because the winner's boundary is
    # newer than the loser's pre-lock timestamp).
    moment = now or datetime.now(UTC)
    month_start = utc_month_start(moment)

    # Idempotency: the same key returns the recorded decision without consuming
    # another slot, budget or cooldown. Held under the loop lock, so two
    # concurrent callers for one loop serialise here.
    existing = await _existing_attempt(
        session,
        organisation_id=organisation_id,
        loop_config_id=loop_config_id,
        admission_key=admission_key,
    )
    if existing is not None:
        reservation = await _reservation_for_attempt(
            session,
            organisation_id=organisation_id,
            attempt_id=existing.id,
        )
        _log.info(
            "system_map_loop_admission.replayed loop=%s key=%s status=%s",
            loop.id,
            admission_key,
            existing.status,
        )
        return _replay_decision(existing, reservation, admission_key)

    amount = reserved_budget_usd if reserved_budget_usd is not None else default_reserved_budget(loop)
    if amount <= 0:
        raise ValueError("reserved_budget_usd must be positive")

    attempt = await _REPO.create_admission_attempt(
        session,
        organisation_id=organisation_id,
        loop_config_id=loop.id,
        idempotency_key=admission_key,
        month_start=month_start,
    )
    if attempt.status != "pending":
        # The insert lost a race against a decision already recorded for this
        # key (the helper resolves the winner). Replay it: never decide twice.
        reservation = await _reservation_for_attempt(
            session,
            organisation_id=organisation_id,
            attempt_id=attempt.id,
        )
        return _replay_decision(attempt, reservation, admission_key)

    reason = _publication_failure_reason(map_row, revision_row, loop)
    if reason is None:
        reason = _pipeline_failure_reason(pipeline)
    if reason is None and await _attribution_missing(session, organisation_id=organisation_id, loop=loop):
        reason = REJECTION_ATTRIBUTION
    if reason is None and pipeline is not None and _effective_autonomy(pipeline) == _AUTONOMY_MANUAL:
        reason = REJECTION_AUTONOMY
    if reason is None and (
        await _REPO.get_loop_cooldown_deadline(
            session,
            organisation_id=organisation_id,
            loop_config_id=loop.id,
            now=moment,
        )
        is not None
    ):
        reason = REJECTION_COOLDOWN
    if reason is None and pipeline is not None:
        limit = min(loop.max_concurrent_runs, pipeline.max_concurrent_runs or 1)
        active = await _REPO.count_active_loop_reservations(
            session,
            organisation_id=organisation_id,
            loop_config_id=loop.id,
        )
        if active >= limit:
            reason = REJECTION_CONCURRENCY
    if reason is None:
        used = await _REPO.get_loop_month_usage(
            session,
            organisation_id=organisation_id,
            loop_config_id=loop.id,
            month_start=month_start,
        )
        if used + amount > Decimal(loop.budget_limit_usd):
            reason = REJECTION_BUDGET

    if reason is not None:
        return await _reject(
            session,
            organisation_id=organisation_id,
            loop=loop,
            attempt=attempt,
            reason=reason,
            admission_key=admission_key,
            month_start=month_start,
            reserved_budget_usd=amount,
        )

    return await _admit(
        session,
        organisation_id=organisation_id,
        loop=loop,
        attempt=attempt,
        admission_key=admission_key,
        month_start=month_start,
        reserved_budget_usd=amount,
        input_payload=input_payload,
    )


async def reconcile_loop_reservations(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
) -> int:
    """Settle reservations whose runs already terminated (idempotent).

    The backstop for a terminal write that could not settle inline (crash
    between the run write and the settlement, or a direct-SQL terminaliser).
    Never invents a second reservation.
    """
    rows = await _REPO.list_terminal_reservations(session, organisation_id=organisation_id)
    settled = 0
    for row in rows:
        if await settle_loop_reservation_for_run(
            session,
            organisation_id=organisation_id,
            run_id=row.run_id,
            run_status=row.status,
            actual_spend_usd=row.total_cost_usd,
        ):
            settled += 1
    return settled


def _window_key(loop: SystemMapLoopConfig, moment: datetime) -> str:
    """Return the stable admission key for the loop's current observation window."""
    window = max(loop.observation_window_seconds, 60)
    bucket = int(moment.timestamp()) // window
    return f"window:{loop.id}:{bucket}"


async def _loop_is_due(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    loop: SystemMapLoopConfig,
    moment: datetime,
) -> bool:
    """Return True when the loop may be considered for admission at *moment*.

    Due means: its observation window has elapsed since the last attempt, no
    reservation is still holding capacity, and no cooldown is running. The
    admission transaction re-checks all three; this only avoids writing a
    pointless rejection every tick.
    """
    last_attempt = await _REPO.get_last_loop_attempt_at(
        session,
        organisation_id=organisation_id,
        loop_config_id=loop.id,
    )
    if last_attempt is not None:
        window = timedelta(seconds=max(loop.observation_window_seconds, 60))
        if moment < _as_utc(last_attempt) + window:
            return False
    if await _REPO.count_active_loop_reservations(
        session,
        organisation_id=organisation_id,
        loop_config_id=loop.id,
    ):
        return False
    cooldown_deadline = await _REPO.get_loop_cooldown_deadline(
        session,
        organisation_id=organisation_id,
        loop_config_id=loop.id,
        now=moment,
    )
    return cooldown_deadline is None


async def _system_maps_enabled(organisation_id: uuid.UUID) -> bool:
    """Resolve the ``system_maps`` feature flag for one org (fail-closed)."""
    from modulo.core.feature_flags import get_registry

    try:
        return bool(await get_registry().resolve_flag("system_maps", org_id=organisation_id))
    except Exception:
        _log.exception("system_map_loop_admission.flag_read_failed org=%s", organisation_id)
        return False


async def _dispatch_run(run_id: uuid.UUID, organisation_id: uuid.UUID) -> str:
    """Dispatch a created loop run through the ONE existing dispatch path."""
    from modulo.core.dispatch import dispatch_run
    from modulo.settings import get_settings

    outcome, _job_id = await dispatch_run(
        str(run_id),
        str(organisation_id),
        queue=get_settings().saq_runs_queue,
    )
    return outcome


async def _process_org(
    factory: async_sessionmaker[AsyncSession],
    organisation_id: uuid.UUID,
    *,
    moment: datetime,
    summary: dict[str, int],
) -> None:
    """Run the due-loop admission, dispatch and reconciliation pass for one org."""
    from modulo.db.rls import set_rls_execution_context, set_rls_org

    async with factory() as session, session.begin():
        await set_rls_org(session, organisation_id)
        await set_rls_execution_context(session)
        summary["reconciled"] += await reconcile_loop_reservations(session, organisation_id=organisation_id)
        loops = await _REPO.list_published_loop_configs(session, organisation_id=organisation_id)
        due_loops = [
            loop
            for loop in loops
            if await _loop_is_due(session, organisation_id=organisation_id, loop=loop, moment=moment)
        ]

    for loop in due_loops:
        summary["loops_considered"] += 1
        try:
            async with factory() as session, session.begin():
                await set_rls_org(session, organisation_id)
                await set_rls_execution_context(session)
                decision = await admit_loop_run(
                    session,
                    organisation_id=organisation_id,
                    loop_config_id=loop.id,
                    admission_key=_window_key(loop, moment),
                    now=moment,
                )
        except LoopConfigNotFoundError:
            _log.info(
                "system_map_loop_admission.loop_vanished loop=%s org=%s",
                loop.id,
                organisation_id,
            )
            continue
        if decision.admitted:
            summary["admitted"] += 1
            run_id = decision.run_id
            assert run_id is not None  # admitted implies a run (see LoopAdmissionDecision)
            try:
                outcome = await _dispatch_run(run_id, organisation_id)
            except Exception:
                _log.exception(
                    "system_map_loop_admission.dispatch_failed loop=%s run=%s",
                    loop.id,
                    decision.run_id,
                )
                summary["dispatch_failed"] += 1
                continue
            if outcome in {"enqueued", "deduped"}:
                summary["dispatched"] += 1
        else:
            summary["rejected"] += 1


async def dispatch_due_loops(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Admit, dispatch and reconcile every due improvement loop (one tick).

    Per-org isolation: one org's failure never stops the others. The org-wide
    trigger pause is honoured — a paused org fires no autonomous loop action.
    Returns a summary of what the tick did.
    """
    from modulo.core.cron_helpers import _open_factory
    from modulo.db.rls import set_rls_org
    from modulo.db.settings_resolver import org_is_paused

    factory = session_factory or _open_factory()
    moment = now or datetime.now(UTC)
    summary = {
        "orgs_scanned": 0,
        "orgs_skipped": 0,
        "loops_considered": 0,
        "admitted": 0,
        "rejected": 0,
        "dispatched": 0,
        "dispatch_failed": 0,
        "reconciled": 0,
        "errors": 0,
    }

    async with factory() as session, session.begin():
        org_ids = list((await session.execute(select(Organisation.id))).scalars())

    for organisation_id in org_ids:
        summary["orgs_scanned"] += 1
        try:
            if not await _system_maps_enabled(organisation_id):
                summary["orgs_skipped"] += 1
                continue
            async with factory() as session, session.begin():
                await set_rls_org(session, organisation_id)
                if await org_is_paused(session, organisation_id):
                    summary["orgs_skipped"] += 1
                    continue
            await _process_org(factory, organisation_id, moment=moment, summary=summary)
        except Exception:
            summary["errors"] += 1
            _log.exception("system_map_loop_admission.org_pass_failed org=%s", organisation_id)

    _log.info("system_map_loop_dispatch summary: %s", summary)
    return summary
