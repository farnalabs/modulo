"""PolicyGateDecision ORM model — persisted audit trail (FAR-1060, FAR-1102).

Identity and constraint surface (chunk 1) plus the six descriptive payload
columns added by chunk 4: resolved_action, error_detail, node_id,
eval_result_id, run_id, policy_gate_version.

Event identity (FAR-1108 chunk 8b — decision, recorded here):
-------------------------------------------------------------
A ``PolicyGateDecision`` row is the decision made on **one persisted
evaluation result**. The event-identity key is therefore ``eval_result_id``,
enforced by the partial unique index ``uq_policy_gate_decisions_eval_result_id``
(``WHERE eval_result_id IS NOT NULL``).

Why NOT ``(run_id, policy_gate_id)`` — the tempting key is wrong:

* A ``warn``-configured node's retry re-runs its evaluations **within the same
  run id**. Each re-evaluation persists a fresh ``EvalResult`` and is a new,
  distinct governance event. A ``block``-configured node's retry is likewise a
  distinct event (the first attempt halted; the retry re-evaluates and may
  resolve differently). One ``(run, gate)`` pair can therefore legitimately
  host many events, so it cannot identify one.

Why ``eval_result_id`` is the right key:

* Chunk 2 (persist-before-decide) persists a **distinct ``EvalResult`` per
  evaluation attempt, before the decision is made**. The result id therefore
  names exactly one evaluation attempt — the governance event. One attempt →
  one result → one decision. ``run_id`` and ``policy_gate_id`` are denormalized
  context carried for audit, not identity.

Why chunk 4's temporary composite ``(run_id, policy_gate_id, eval_result_id)``
is replaced rather than made permanent:

* It is **redundant** — any two rows sharing a result id also share the run and
  gate the result belongs to — and it is **weaker**: it would permit two rows
  with the same ``eval_result_id`` but a different ``run_id`` (a transposition
  bug, which is exactly the residual gap the plain non-FK columns carry). The
  result-only key rejects that case too. Chunk 4 named this work item (FAR-1108
  chunk 8b) as the owner of that temporary bound; this module and migration
  ``0295_decision_record_event_identity`` formalise the lifecycle.

Rows with ``eval_result_id IS NULL`` stay **unbounded by design** (PostgreSQL
and SQLite treat NULLs as distinct in a unique index): such a row is itself an
anomaly — a decision recorded with no grounded evaluation result — and is the
reconciliation job's responsibility
(``modulo.core.eval_engine.decision_reconcile``), not the index's.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped

# Valid resolved_action vocabulary — must match the CHECK constraint.
RESOLVED_ACTION_VALUES = ("continue", "warn", "block")


class PolicyGateDecision(OrgScoped):
    __tablename__ = "policy_gate_decisions"
    __table_args__ = (
        UniqueConstraint("id", "organisation_id", name="uq_policy_gate_decisions_id_organisation_id"),
        ForeignKeyConstraint(
            ["policy_gate_id", "organisation_id"],
            ["policy_gates.id", "policy_gates.organisation_id"],
            ondelete="RESTRICT",
            name="fk_policy_gate_decisions_gate_org",
        ),
        ForeignKeyConstraint(
            ["eval_id", "organisation_id"],
            ["evals.id", "evals.organisation_id"],
            ondelete="RESTRICT",
            name="fk_policy_gate_decisions_eval_org",
        ),
        CheckConstraint(
            "resolved_action IN ('continue', 'warn', 'block')",
            name="ck_policy_gate_decisions_resolved_action",
        ),
        # Permanent event-identity key (FAR-1108 chunk 8b). One decision record
        # per persisted evaluation result. Replaces chunk 4's temporary bridge
        # index ``ix_tmp_policy_gate_decisions_run_gate_result`` (dropped by
        # migration 0295_decision_record_event_identity). NULL result ids stay
        # unbounded by design — see the module docstring.
        Index(
            "uq_policy_gate_decisions_eval_result_id",
            "eval_result_id",
            unique=True,
            postgresql_where=text("eval_result_id IS NOT NULL"),
            sqlite_where=text("eval_result_id IS NOT NULL"),
        ),
    )

    policy_gate_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    eval_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)

    # --- Payload columns (chunk 4 / FAR-1102) ---
    resolved_action: Mapped[str] = mapped_column(Text, nullable=False, server_default="'continue'")
    error_detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    eval_result_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    policy_gate_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
