"""PolicyGateDecision ORM model — persisted audit trail (FAR-1060, FAR-1102).

Identity and constraint surface (chunk 1) plus the six descriptive payload
columns added by chunk 4: resolved_action, error_detail, node_id,
eval_result_id, run_id, policy_gate_version.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint, Uuid
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
