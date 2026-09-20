"""PolicyGateDecision ORM model — persisted audit trail (FAR-1060, chunk 1).

Identity and constraint surface only.  The six descriptive payload columns
(resolved_action, error_detail, node_id, eval_result_id, run_id,
policy_gate_version) ship in a later chunk as an additive migration.
"""

import uuid

from sqlalchemy import ForeignKeyConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class PolicyGateDecision(OrgScoped):
    __tablename__ = "policy_gate_decisions"
    __table_args__ = (
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
    )

    policy_gate_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    eval_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
