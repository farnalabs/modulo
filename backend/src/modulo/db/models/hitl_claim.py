import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import JSONB_COL, OrgScoped


class HitlClaim(OrgScoped):
    __tablename__ = "hitl_claims"
    __table_args__ = (
        UniqueConstraint("run_id", "review_id", name="uq_hitl_claims_run_review"),
        # Decision vocabulary is otherwise enforced only at the write layer
        # (HITLManager._decide's _DECISION_* constants); crud paths do not
        # validate, so a bad value would persist unchecked and be silently
        # ignored by the sweep alarm's decision.in_ filter. NULL-tolerant:
        # undecided gates carry NULL. Mirrors migration 0309.
        CheckConstraint(
            "decision IS NULL OR decision IN ('approved', 'rejected', 'deliver_manual')",
            name="ck_hitl_claims_decision",
        ),
        # Per-actor sweep-alarm window (sweep_alarm.py): WHERE
        # organisation_id = ? AND decided_by = ? AND decision IN (...) AND
        # decision_at >= ?. The single-column organisation_id / decided_by
        # legs force a bitmap-and plus heap time filter; the composite
        # serves it in one descent. Partial: undecided gates never enter.
        # Mirrors migration 0309.
        Index(
            "ix_hitl_claims_sweep_actor_window",
            "organisation_id",
            "decided_by",
            "decision_at",
            postgresql_where=text("decided_by IS NOT NULL AND decision IS NOT NULL"),
            sqlite_where=text("decided_by IS NOT NULL AND decision IS NOT NULL"),
        ),
        # FAR-2055: the compliance evidence index's decided-gate scan
        # (compliance/evidence_index.py) filters
        # ``organisation_id = ? AND decision IS NOT NULL`` and orders by
        # ``decision_at DESC NULLS LAST`` (an audit-period window adds a
        # ``decision_at`` range). The per-actor window index above leads on
        # ``decided_by``, which this query does not filter, so it cannot serve
        # the scan. Partial: undecided gates never enter - and they are the
        # growing majority. Mirrors migration 0325.
        Index(
            "ix_hitl_claims_org_decision_at",
            "organisation_id",
            "decision_at",
            postgresql_where=text("decision IS NOT NULL"),
            sqlite_where=text("decision IS NOT NULL"),
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    required_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    review_id: Mapped[str] = mapped_column(String(255), nullable=False)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    # FAR-748: the durable per-actor DECISION record. ``account_id`` is the
    # claimant and is NULLed at decision time (a decided row no longer owns a
    # claim), so the FAR-611 sweep alarm previously had to reconstruct the
    # actor from the hash-chained audit chain (best-effort — an audit append
    # failure made a decision invisible to the alarm). ``decided_by`` is
    # stamped at decision time (the single stamp authority, ``HITLManager.
    # _decide``) so detection is one indexed claim-row query with no join to
    # the audit table. NOT NULL going forward: every decision after this
    # column exists carries an actor; rows decided before it carry NULL
    # (legacy) and are skipped by the sweep detection rather than
    # backfilled with a fictitious actor.
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(20))
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Full resume payload persisted at decision time (B1) - the same dict the
    # web routes pass to executor.resume. Survives SAQ job loss so a recovered
    # resume injects the human's actual verdict, never an empty approval.
    # jsonb in the database after migration 0147 — JSONB_COL keeps the ORM
    # declaration honest while preserving SQLite/MariaDB parity.
    decision_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB_COL, nullable=True, default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set by the hitl_overdue notification job once a `hitl_overdue` event has
    # been dispatched for this claim — keeps the job idempotent (one warning
    # per claim, no re-alerting every tick).
    overdue_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # FAR-613: the fire-time decision briefing, captured by the executor's
    # interrupt handler — {description, condition, trigger, source_node_id,
    # source_node_label, artifacts, reason, pipeline_name}. Persisted on the
    # claim row so the reviewer's briefing reflects the graph state at FIRE
    # time and reads without a per-gate snapshot walk. Nullable: legacy gates
    # that fired before this column existed carry NULL (the UI renders a muted
    # "no description" fallback). jsonb in the parallel migration; JSONB_COL
    # keeps the ORM declaration honest while preserving SQLite/MariaDB parity
    # (same pattern as decision_payload).
    context_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB_COL, nullable=True, default=None)
    # FAR-634: the resolved ``hitl_review_config``, stamped by the executor's
    # interrupt handler at fire time. The human_only resolver
    # (``db.crud.hitl_review_config.resolve_hitl_review_config``) reads this FIRST
    # (one claim-row lookup instead of the snapshot/live-edge walk), falling
    # back to the walk for legacy rows that fired before this column existed.
    # Nullable: legacy gates carry NULL and the resolver walk covers them; a
    # stamp failure at fire time is failure-isolated (the gate still fires
    # with NULL config and the resolver falls back). jsonb in the parallel
    # migration; JSONB_COL keeps the ORM declaration honest while preserving
    # SQLite/MariaDB parity (same pattern as context_json).
    gate_config_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB_COL, nullable=True, default=None)
    # FAR-1257: the ABSOLUTE terminalization deadline, stamped ONCE at fire time
    # by ``HITLManager.create_gate`` from the resolved review window (pipeline
    # override > org default > instance/env default). The FAR-648 terminalizer's
    # deadline predicate is ``terminalize_at < now()`` for stamped rows; NULL
    # (legacy rows fired before this column existed) falls back to the legacy
    # ``expires_at + grace`` arithmetic. Deliberately separate from
    # ``expires_at``: that column is the claim TTL and the claim-expiry job
    # RESETS it on every claim, so overloading it would move the
    # terminalization deadline whenever a claim is re-armed.
    terminalize_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NOTE (qa F13): no ``parked_at`` column. The park sweep (run_admission.
    # park_expired_hitl_runs) marks a parked run via the RUN's ``hitl_parked``
    # status — the gate row is untouched (park ≠ decide) and a separate stamp
    # would duplicate that signal, go stale on a re-park, and add schema +
    # phantom-count surface for zero consumers.
