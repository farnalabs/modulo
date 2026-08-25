"""SuiteRun — the capstone eval-comparison entity (FAR-376 Phase 3).

The generic Eval Product MVP closes the flywheel with a *run*: take an
``EvalSuite`` (Phase 1) onto a repeatable ``EvalDataset`` (Phase 2), execute it
against a pinned Model Backend, snapshot the exact inputs + definition config it
ran against, persist every per-case outcome into the existing ``eval_results``
table, and — when a same-tuple baseline already exists — detect a pass-rate
regression.

The dataset version and definition checksum are **snapshots** captured at
creation (never live-looked-up), so re-running on a changed corpus or a changed
eval config is a NEW tuple that gets its own baseline rather than corrupting a
prior one. ``baseline_tuple`` is the immutable comparison key.

State machine
-------------
``pending -> running -> completed | partial | failed``, plus ``cancelled``.
``partial`` (some cases errored) is distinct from ``failed`` (orchestration
error). Transitions are guarded by an optimistic-lock version column so two
workers cannot both transition the same run to ``completed``.

RLS
---
``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` + ``rls_org_isolation`` on
``suite_runs`` (owned by ``modulo_migrate``), so the app role cannot bypass
org isolation. The ``OrgScoped`` mixin alone is insufficient — the migration
installs the same FORCE-RLS ceremony as the other eval tables.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    update,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.eval_result import EvalResult


class SuiteRunState(StrEnum):
    """Legal states for a ``SuiteRun``."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Single source of truth for legal transitions. Terminal states (``completed``,
# ``partial``, ``failed``, ``cancelled``) have no outgoing transitions.
_ALLOWED_TRANSITIONS: dict[SuiteRunState, frozenset[SuiteRunState]] = {
    SuiteRunState.PENDING: frozenset({SuiteRunState.RUNNING, SuiteRunState.CANCELLED}),
    SuiteRunState.RUNNING: frozenset(
        {SuiteRunState.COMPLETED, SuiteRunState.PARTIAL, SuiteRunState.FAILED, SuiteRunState.CANCELLED}
    ),
    SuiteRunState.COMPLETED: frozenset(),
    SuiteRunState.PARTIAL: frozenset(),
    SuiteRunState.FAILED: frozenset(),
    SuiteRunState.CANCELLED: frozenset(),
}

_TERMINAL_STATES: frozenset[str] = frozenset(
    {
        SuiteRunState.COMPLETED.value,
        SuiteRunState.PARTIAL.value,
        SuiteRunState.FAILED.value,
        SuiteRunState.CANCELLED.value,
    }
)


def is_terminal(state: str | SuiteRunState) -> bool:
    """Return True when *state* is terminal (no further transition legal)."""
    value = state.value if isinstance(state, SuiteRunState) else state
    return value in _TERMINAL_STATES


def can_transition(current: str | SuiteRunState, target: str | SuiteRunState) -> bool:
    """Return True when transitioning *current* -> *target* is legal.

    Calls with ``RuntimeError``-free semantics: always raises
    ``IllegalStateTransition`` when the move is not legal. Set transitions are
    the single source of truth in ``_ALLOWED_TRANSITIONS``.
    """
    cur = SuiteRunState(current) if isinstance(current, str) else current
    tgt = SuiteRunState(target) if isinstance(target, str) else target
    return tgt in _ALLOWED_TRANSITIONS[cur]


class IllegalStateTransitionError(RuntimeError):
    """Raised when a ``SuiteRun`` is moved along a non-legal edge."""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"Illegal SuiteRun transition {current!r} -> {target!r}")
        self.current = current
        self.target = target


class OptimisticLockError(RuntimeError):
    """Raised when a version-guarded transition races with another writer."""

    def __init__(self, run_id: uuid.UUID, expected_version: int) -> None:
        super().__init__(f"SuiteRun {run_id} version {expected_version} was already bumped by another writer")
        self.run_id = run_id
        self.expected_version = expected_version


class SuiteRun(OrgScoped):
    """One execution of an ``EvalSuite`` against a ``EvalDataset`` snapshot."""

    __tablename__ = "suite_runs"

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','running','completed','partial','failed','cancelled')",
            name="ck_suite_runs_state",
        ),
        # The pair (suite, dataset) + version is the repeatable comparison key.
        Index("ix_suite_runs_suite_dataset", "suite_id", "dataset_id"),
        # Baseline resolution: recent completed runs of a tuple.
        Index("ix_suite_runs_state_created", "organisation_id", "state", "created_at"),
    )

    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    suite_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("eval_suites.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("eval_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # SNAPSHOT of dataset membership at creation — immutable.
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # SNAPSHOT of each eval-definition config at creation — immutable.
    definition_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    # Pinned model backend the run executed under — immutable.
    model_backend_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("model_backends.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Deterministic canonical hash of the run's scenario inputs; NULL when the
    # suite declares no scenarios (an explicit "scenarios unused" sentinel).
    scenario_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The full comparison tuple snapshot at creation — NEVER live-looked-up.
    baseline_tuple: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # The same-tuple completed run this run is compared against (resolved at
    # completion). NULL when no completed prior run exists (SKIPPED comparison).
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("suite_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Operator-pinned canonical baseline (survives new same-tuple runs).
    baseline_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    # Optimistic-lock guard: bumped on every state transition.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    passed_cases: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_cases: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Cases than errored — excluded from the pass-rate denominator.
    excluded_case_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    total_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    # Incremented atomically BEFORE each judge call (row-locked ledger) so a
    # read-check-write spend race cannot overshoot the per-suite ceiling.
    claimed_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True, server_default="0")

    # Output of ``detect_regressions`` (group_by=suite) at completion.
    comparison_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Regression decision: True when the run dropped below the configured
    # thresholds vs its baseline; None when no baseline (SKIPPED); False when
    # compared and not degraded.
    regressed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Idempotency + per-suite rate-limit marker for the regression notification.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Cost / excluded_case_count and any additional run telemetry.
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    eval_results: Mapped[list[EvalResult]] = relationship(back_populates="suite_run", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return f"<SuiteRun id={self.id} suite={self.suite_id} state={self.state} version={self.version}>"


def transition_state(
    session: Any,
    run: SuiteRun,
    target: str | SuiteRunState,
    *,
    expected_version: int | None = None,
    completed_at: datetime | None = None,
) -> int:
    """Atomically move *run* along a legal edge, guarded by an optimistic lock.

    Performs the state change as a single version-guarded ``UPDATE ... RETURNING``
    so two concurrent writers cannot both land ``completed`` — the second one
    sees a stale ``version`` and gets ``None`` back and this raises
    ``OptimisticLockError``.

    Returns the new version on success. Raises ``IllegalStateTransition`` for a
    non-legal edge before touching the DB, and ``OptimisticLockError`` when the
    version guard fails. No ``flush``/``commit`` is performed — the caller owns
    the transaction.
    """
    if run.id is None:
        raise ValueError("transition_state requires a persisted SuiteRun")
    new_state = SuiteRunState(target) if isinstance(target, str) else target
    if not can_transition(run.state, new_state):
        raise IllegalStateTransitionError(run.state, new_state.value)
    ver = run.version if expected_version is None else expected_version

    values: dict[str, Any] = {"state": new_state.value, "version": SuiteRun.version + 1}
    if new_state in (SuiteRunState.COMPLETED, SuiteRunState.PARTIAL, SuiteRunState.FAILED, SuiteRunState.CANCELLED):
        values["completed_at"] = completed_at or datetime.now()

    stmt = (
        update(SuiteRun)
        .where(SuiteRun.id == run.id, SuiteRun.version == ver)
        .values(**values)
        .returning(SuiteRun.version)
    )
    new_version: int | None = session.execute(stmt).scalar_one_or_none()
    if new_version is None:
        raise OptimisticLockError(run.id, ver)
    run.state = new_state.value
    run.version = new_version
    if new_state in (SuiteRunState.COMPLETED, SuiteRunState.PARTIAL, SuiteRunState.FAILED, SuiteRunState.CANCELLED):
        run.completed_at = values.get("completed_at")
    return new_version


# --------------------------------------------------------------------------- #
# Canonical signature helpers                                                #
# --------------------------------------------------------------------------- #
def canonical_hash(payload: Any) -> str:
    """SHA-256 of *payload*, deterministic across Python versions and key order.

    Serialised with ``sort_keys`` + stable separators so semantically identical
    inputs produce the same hash regardless of key ordering in the source.
    ``default=str`` keeps UUID-bearing scenario/definition snapshots hashable.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_scenario_signature(scenario_inputs: dict[str, Any] | None) -> str | None:
    """Deterministic canonical hash of the run's scenario inputs.

    Returns ``None`` (the explicit "scenarios unused" sentinel) when
    *scenario_inputs* is ``None`` or empty, so a suite with no scenarios is
    compared on its dataset+definition tuple rather than an arbitrary hash.
    """
    if not scenario_inputs:
        return None
    return canonical_hash({"scenario": scenario_inputs})


def compute_definition_checksum(definitions: list[dict[str, Any]]) -> str:
    """SHA-256 over every eval-definition's config snapshot at creation.

    ``definitions`` is a list of ``{"id": <uuid>, "eval_type": <str>, "config_json": <dict>}``
    snapshots. Definitions are sorted by ``id`` so re-running the same set in a
    different insertion order produces the same checksum. A changed config or a
    changed membership produces a NEW checksum and therefore a NEW baseline
    tuple — never a silent comparison against a different contract.
    """
    items = sorted(definitions, key=lambda d: str(d["id"]))
    return canonical_hash({"definitions": items})
