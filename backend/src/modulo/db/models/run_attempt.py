"""run_attempts — durable per-attempt execution history for a run (FAR-2078).

One row per execution ATTEMPT of a run. Before this table only the FINAL
attempt's triage survived (the agent-return-contract columns on ``runs`` and
``run_node_outputs``); an intermediate attempt that stalled, hit the node
deadline, exhausted the guard cap or lost its worker left no durable record, so
diagnosing the 2026-10-22 Branch Fixer timeline took roughly an hour of
forensics from audit events and ``dispatched_at`` rewrites.

This is the PERSISTENCE layer only: the executor instrumentation that writes
these rows and the API/UI that reads them ship as separate slices.

``attempt_index`` semantics (LOAD BEARING — the executor relies on these):
    * **0-based.** The first attempt of a run is ``0``; the second is ``1``.
      This matches the allocator expression
      ``COALESCE(MAX(attempt_index), -1) + 1`` with no ``-1`` correction needed
      anywhere between the executor and this table. It is deliberately
      independent of the per-NODE ``attempt_key`` suffix
      (``run:<run_id>:node:<node_id>:<claim_count>``, which counts node claims,
      not run-level attempts), so the two must not be treated as the same
      number.
    * **Per run, contiguous, monotonic.** Allocation never skips a value, so
      ``MAX(attempt_index) + 1`` is also the number of attempts started.
    * **Never reused within a run.** Rows are only removed by the run-deletion
      cascade, so a live run can never release an index back into circulation.
    * **Unique per run.** ``uq_run_attempts_run_attempt_index`` is the hard
      invariant; the allocator (``crud.run_attempts.start_attempt``) serialises
      concurrent starters on a Postgres per-run advisory lock so the constraint
      is never reached by the normal path.

``terminal_reason`` is a CLOSED vocabulary, enforced by
``ck_run_attempts_terminal_reason`` (DB backstop) and validated by the CRUD
closer before the write (fail loud rather than let an unknown value reach the
column). ``NULL`` means the attempt has not ended yet — the row is OPEN, and
``crud.run_attempts.close_attempt`` stamps ``ended_at`` / ``terminal_reason`` /
``error_code`` on the latest open row.

Retention: this table inherits its parent run's retention. ``run_id`` is a
``FOREIGN KEY ... ON DELETE CASCADE`` to ``runs``, so every run-deletion path —
the age-based retention purge (``crud.run_retention.purge_terminal_runs``) and
org deletion — removes the run's attempts in the same statement. There is NO
separate sweep and NO unbounded growth: attempts cannot outlive their run. The
``organisation_id`` FK is also ``ON DELETE CASCADE`` (tenant anchor, matching
the other run child tables).

Indexing: the one access path is "all attempts for one run, in order", so the
table carries ONE index — the ``(run_id, attempt_index)`` unique constraint.
Its btree also serves ``WHERE run_id = ?`` via the leftmost prefix and
``ORDER BY attempt_index``, so a separate non-unique index on the same pair
would be redundant (database lens 1). ``organisation_id`` is separately indexed
by ``OrgScoped`` for the RLS policy and tenant-scoped lookups.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped

# ---------------------------------------------------------------------------
# Closed vocabulary behind ``run_attempts.terminal_reason``.
#
# ``ck_run_attempts_terminal_reason`` (migration 0326) enforces the same set at
# the DB. Adding a cause means adding the constant here, widening the CHECK via
# a NEW migration (never editing 0326) and updating the write site — together,
# never as a follow-up. ``NULL`` is not in the vocabulary: it is the OPEN state.
# ---------------------------------------------------------------------------
ATTEMPT_TERMINAL_REASON_DEADLINE: Final[str] = "deadline"
ATTEMPT_TERMINAL_REASON_STALL: Final[str] = "stall"
ATTEMPT_TERMINAL_REASON_GUARD_CAP: Final[str] = "guard_cap"
ATTEMPT_TERMINAL_REASON_WORKER_LOST: Final[str] = "worker_lost"
ATTEMPT_TERMINAL_REASON_PROVIDER_ERROR: Final[str] = "provider_error"
ATTEMPT_TERMINAL_REASON_COMPLETED: Final[str] = "completed"
ATTEMPT_TERMINAL_REASON_RESET: Final[str] = "reset"

ATTEMPT_TERMINAL_REASON_VALUES: Final[frozenset[str]] = frozenset(
    {
        ATTEMPT_TERMINAL_REASON_DEADLINE,
        ATTEMPT_TERMINAL_REASON_STALL,
        ATTEMPT_TERMINAL_REASON_GUARD_CAP,
        ATTEMPT_TERMINAL_REASON_WORKER_LOST,
        ATTEMPT_TERMINAL_REASON_PROVIDER_ERROR,
        ATTEMPT_TERMINAL_REASON_COMPLETED,
        ATTEMPT_TERMINAL_REASON_RESET,
    }
)

#: The CHECK expression, single-sourced for the model. Migration 0326 carries
#: its own frozen copy (a shipped migration must not depend on live code); the
#: migration test asserts the two are identical, so widening the vocabulary
#: without a migration fails CI rather than drifting silently.
ATTEMPT_TERMINAL_REASON_SQL: Final[str] = (
    "terminal_reason IN ('deadline', 'stall', 'guard_cap', 'worker_lost', 'provider_error', 'completed', 'reset')"
)


class RunAttempt(OrgScoped):
    """One execution attempt of a run (see the module docstring).

    Inherits ``id`` (uuid PK), ``organisation_id`` (FK -> organisations,
    CASCADE, indexed) and ``created_at``/``updated_at`` from :class:`OrgScoped`.
    ``run_id`` + ``attempt_index`` are the natural key for the run's attempt
    sequence and are unique together.
    """

    __tablename__ = "run_attempts"
    __table_args__ = (
        # The natural key of the attempt sequence. Doubles as the index for
        # "all attempts for one run" (leftmost prefix + ordering) — see the
        # module docstring; a separate (run_id, attempt_index) index would be
        # redundant.
        UniqueConstraint("run_id", "attempt_index", name="uq_run_attempts_run_attempt_index"),
        # 0-based, so a negative index is a caller bug, never a valid sentinel.
        CheckConstraint("attempt_index >= 0", name="ck_run_attempts_attempt_index_non_negative"),
        CheckConstraint(ATTEMPT_TERMINAL_REASON_SQL, name="ck_run_attempts_terminal_reason"),
        # Deliberately NO ``ended_at >= started_at`` CHECK: the two instants can
        # come from different clocks (the executor may pass an observed
        # ``started_at`` from the sandbox while ``ended_at`` is the app host's),
        # and a diagnostic row must never be rejected because two clocks
        # disagree by a second. Ordering is informational, not an invariant.
    )

    # Retention: deleting the run deletes its attempts (see module docstring).
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    # 0-based, per run, contiguous. See the module docstring for the contract.
    attempt_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    # When the attempt began. The allocator defaults this to server time; the
    # executor may pass the dispatch instant it actually observed.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    # NULL while the attempt is OPEN; stamped by close_attempt.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # One of ATTEMPT_TERMINAL_REASON_VALUES while open->closed; NULL = open.
    terminal_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # The concrete model used for THIS attempt (nullable: unknown for a
    # pre-instrumentation attempt or a runtime that does not report it).
    model_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Run-level error taxonomy value observed at the attempt's end (if any).
    error_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
