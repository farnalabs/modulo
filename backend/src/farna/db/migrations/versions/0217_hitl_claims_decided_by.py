"""Add the decided_by column to hitl_claims (FAR-748 durable detection shape).

``hitl_claims.account_id`` is the CLAIMANT and is NULLed at decision time,
so the FAR-611 sweep alarm's per-actor detection had to read the
hash-chained ``audit_events`` chain (best-effort data — a decision whose
audit append failed was invisible to the alarm). ``decided_by`` is the
durable per-actor DECISION record, stamped by the single stamp authority
(``HITLManager._decide``) at decision time and seeded by the same UPDATE
that commits the decision, so detection becomes one indexed claim-row
query with no join to the audit table.

NOT NULL going forward: every decision after this migration carries an
actor. The column is created NULLABLE — existing decided rows carry NULL
(they cannot be backfilled with a trustworthy actor) and the sweep
detection skips rather than invents one. Account deletion follows the
audit chain's SET NULL semantics: the claim row stays, only the actor
reference goes.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0217_hitl_claims_decided_by"
down_revision: str | None = "0216_audit_events_sweep_detection_index"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "hitl_claims",
        sa.Column("decided_by", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_hitl_claims_decided_by_accounts",
        "hitl_claims",
        "accounts",
        ["decided_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute('CREATE INDEX IF NOT EXISTS ix_hitl_claims_decided_by ON public."hitl_claims" USING btree (decided_by);')


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_hitl_claims_decided_by;")
    op.drop_constraint("fk_hitl_claims_decided_by_accounts", "hitl_claims", type_="foreignkey")
    op.drop_column("hitl_claims", "decided_by")
