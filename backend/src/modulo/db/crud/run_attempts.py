"""CRUD for durable per-attempt execution history (FAR-2078).

The persistence seam behind ``run_attempts`` (see
:mod:`modulo.db.models.run_attempt` for the column contract and the
``attempt_index`` semantics). Three operations:

* :func:`start_attempt` — allocate the next ``attempt_index`` for a run and
  insert the OPEN row, returning the index.
* :func:`close_attempt` — stamp ``ended_at`` / ``terminal_reason`` /
  ``error_code`` on the LATEST open row, returning the index closed.
* :func:`list_attempts` — every attempt for one run, in ``attempt_index``
  order (the table's single access path).

Why ``attempt_index`` allocation is race-safe on Postgres
--------------------------------------------------------
The allocation is ``COALESCE(MAX(attempt_index), -1) + 1 WHERE run_id = ?``
followed by the INSERT, both inside the caller's transaction. Under READ
COMMITTED two concurrent starters would each see the same committed max and
allocate the same index. ``start_attempt`` therefore takes a transaction-scoped
per-run advisory lock (``pg_advisory_xact_lock(hashtext('run_attempt:<id>'))``,
the same primitive ``db.crud._gate_advisory_lock`` uses) BEFORE the MAX read:
concurrent starters for one run serialise on the lock, so the MAX each one sees
includes every attempt the previous holder inserted. The lock is released when
the caller's transaction commits or rolls back, and the lock key is per run, so
attempts of different runs never contend.

``uq_run_attempts_run_attempt_index`` is the invariant backstop: even a writer
that bypasses this helper cannot create two attempts with the same index — it
gets an ``IntegrityError`` rather than silent corruption.

Generic backends (SQLite/MariaDB) have no advisory lock. They fall back to the
same MAX+1 computation without serialisation; SQLite serialises writers on the
database and the unique constraint still rejects a duplicate, but concurrent
starters are NOT guaranteed a retry-free outcome there. This is the same
documented generic-backend divergence as ``crud.run._allocate_run_number``;
Postgres is the production backend and the one this contract is written for.

Transaction scope: both helpers work inside the caller's session/transaction.
Neither commits. The caller must commit for the row to be durable — and until
it does, the advisory lock remains held (keep the surrounding transaction
short).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run import get_dialect_name
from modulo.db.models.run import Run
from modulo.db.models.run_attempt import (
    ATTEMPT_TERMINAL_REASON_VALUES,
    RunAttempt,
)

# Public surface of the persistence seam. The production call sites (executor
# instrumentation and the API/UI read path) land in the FAR-2078 follow-up
# slices; declaring the entry points here is also the vulture-recognised
# statement that they are intended API, not dead code.
__all__ = [
    "RunAttemptRunNotFoundError",
    "attempt_lock_key",
    "close_attempt",
    "list_attempts",
    "start_attempt",
]

#: Prefix of the per-run advisory-lock key. One derivation site (below) so the
#: start path can never build a key that fails to match a concurrent starter.
_ATTEMPT_LOCK_KEY_PREFIX = "run_attempt:"

# ``hashtext`` returns int4; ``pg_advisory_xact_lock`` takes bigint, so the
# implicit cast is what ``_gate_advisory_lock`` relies on too.
_ATTEMPT_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(:key))"


class RunAttemptRunNotFoundError(LookupError):
    """Raised when :func:`start_attempt` names a run that does not exist.

    A typed failure beats letting the ``run_id`` FK surface as a bare
    ``IntegrityError``: the caller passed a bad identifier, and that is worth
    distinguishing from any other constraint violation.
    """


def attempt_lock_key(run_id: uuid.UUID) -> str:
    """Return the canonical advisory-lock key for one run's attempt sequence."""
    return f"{_ATTEMPT_LOCK_KEY_PREFIX}{run_id}"


async def _acquire_attempt_lock(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Serialise concurrent attempt allocation for *run_id* on Postgres.

    Transaction-scoped: the lock releases on the caller's COMMIT/ROLLBACK. On
    non-Postgres backends this is a no-op (see the module docstring).
    """
    if await get_dialect_name(session) == "postgresql":
        await session.execute(text(_ATTEMPT_LOCK_SQL), {"key": attempt_lock_key(run_id)})


async def _run_organisation_id(session: AsyncSession, run_id: uuid.UUID) -> uuid.UUID:
    """Read the parent run's ``organisation_id`` — the tenant anchor for the row.

    Derived from the run rather than accepted from the caller so an attempt can
    never be filed under an organisation other than its run's. Raises
    :class:`RunAttemptRunNotFoundError` when the run does not exist.
    """
    organisation_id = (await session.execute(select(Run.organisation_id).where(Run.id == run_id))).scalar_one_or_none()
    if organisation_id is None:
        raise RunAttemptRunNotFoundError(f"run {run_id} not found; cannot start an attempt")
    return organisation_id


async def start_attempt(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    model_used: str | None = None,
    started_at: datetime | None = None,
) -> int:
    """Insert the OPEN attempt row for *run_id* and return its ``attempt_index``.

    The index is 0-based, per run, contiguous and monotonic (the model's module
    docstring is the contract). ``organisation_id`` is derived from the run.
    ``started_at`` defaults to the current UTC instant; the executor may pass
    the dispatch instant it actually observed instead.

    Does not commit — the caller owns the transaction (and therefore the
    advisory lock's lifetime).
    """
    organisation_id = await _run_organisation_id(session, run_id)
    await _acquire_attempt_lock(session, run_id)

    next_index = (
        await session.execute(
            select(func.coalesce(func.max(RunAttempt.attempt_index), -1) + 1).where(RunAttempt.run_id == run_id)
        )
    ).scalar_one()

    session.add(
        RunAttempt(
            organisation_id=organisation_id,
            run_id=run_id,
            attempt_index=int(next_index),
            started_at=started_at if started_at is not None else datetime.now(UTC),
            model_used=model_used,
        )
    )
    await session.flush()
    return int(next_index)


async def close_attempt(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    terminal_reason: str,
    error_code: str | None = None,
    ended_at: datetime | None = None,
) -> int | None:
    """Close the LATEST open attempt of *run_id*.

    Stamps ``ended_at`` (defaults to the current UTC instant), ``terminal_reason``
    and ``error_code``. Returns the closed
    ``attempt_index``, or ``None`` when the run has no open attempt — a
    documented idempotent no-op so an executor retry after a partially applied
    close does not fail.

    ``terminal_reason`` must be in :data:`ATTEMPT_TERMINAL_REASON_VALUES`; an
    unknown value raises ``ValueError`` before the write (the CHECK constraint
    is the DB backstop, but a typed pre-write rejection names the offender).

    The update selects and stamps the latest open row in ONE statement, so two
    concurrent closers cannot close two different rows for the same run.
    """
    if terminal_reason not in ATTEMPT_TERMINAL_REASON_VALUES:
        raise ValueError(
            f"unknown attempt terminal_reason {terminal_reason!r}; "
            f"expected one of {sorted(ATTEMPT_TERMINAL_REASON_VALUES)}"
        )

    latest_open_attempt_id = (
        select(RunAttempt.id)
        .where(RunAttempt.run_id == run_id, RunAttempt.ended_at.is_(None))
        .order_by(RunAttempt.attempt_index.desc())
        .limit(1)
        .scalar_subquery()
    )
    stmt = (
        update(RunAttempt)
        .where(RunAttempt.id == latest_open_attempt_id)
        .values(
            # Python-side default so the value carries the same microsecond
            # precision as ``started_at``; ``CURRENT_TIMESTAMP`` on SQLite is
            # truncated to whole seconds, which can order BEFORE a
            # microsecond-precision start.
            ended_at=ended_at if ended_at is not None else datetime.now(UTC),
            terminal_reason=terminal_reason,
            error_code=error_code,
        )
        .returning(RunAttempt.attempt_index)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_attempts(session: AsyncSession, run_id: uuid.UUID) -> Sequence[RunAttempt]:
    """Every attempt for *run_id*, ordered by ``attempt_index`` ascending.

    Served by ``uq_run_attempts_run_attempt_index`` (leftmost prefix
    ``run_id`` + the ordering column), which is why the table needs no second
    index on the pair.
    """
    result = await session.execute(
        select(RunAttempt).where(RunAttempt.run_id == run_id).order_by(RunAttempt.attempt_index.asc())
    )
    return result.scalars().all()
