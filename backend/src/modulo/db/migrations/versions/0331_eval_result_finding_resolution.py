"""Eval/guardrail finding resolution (FAR-2028).

Adds resolution tracking to eval_results so that every finding (eval or guardrail)
can be marked as accepted, dismissed, superseded, or pending, with a reason,
timestamp, and resolving user.

Revision ID: 0331_eval_result_finding_resolution
Revises: 0330_eval_verdict_contract
Create Date: 2026-10-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0331_eval_result_finding_resolution"
down_revision: str | None = "0330_eval_verdict_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only run on Postgres (other backends come from ORM create_all)
    if not str(op.get_bind().dialect.name).startswith("postgres"):
        return

    # Add resolution columns
    op.execute("ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS resolution VARCHAR(20) NOT NULL DEFAULT 'pending'")
    op.execute("ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS resolution_reason TEXT")
    op.execute("ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
    op.execute(
        "ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS resolved_by UUID REFERENCES accounts(id) ON DELETE SET NULL"
    )

    # Add CHECK constraint for resolution vocabulary
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_eval_results_resolution' "
        "AND conrelid = 'public.eval_results'::regclass) THEN "
        "ALTER TABLE public.eval_results ADD CONSTRAINT ck_eval_results_resolution "
        "CHECK (resolution IN ('accepted', 'dismissed', 'superseded', 'pending')); END IF; END $$;"
    )

    # Index for finding resolutions by eval_id (useful for acceptance rate queries)
    op.execute("CREATE INDEX IF NOT EXISTS ix_eval_results_eval_id_resolution ON eval_results (eval_id, resolution)")


def downgrade() -> None:
    if not str(op.get_bind().dialect.name).startswith("postgres"):
        return

    op.execute("DROP INDEX IF EXISTS ix_eval_results_eval_id_resolution")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_eval_results_resolution' "
        "AND conrelid = 'public.eval_results'::regclass) THEN "
        "ALTER TABLE public.eval_results DROP CONSTRAINT ck_eval_results_resolution; END IF; END $$;"
    )
    op.execute("ALTER TABLE eval_results DROP COLUMN IF EXISTS resolved_by")
    op.execute("ALTER TABLE eval_results DROP COLUMN IF EXISTS resolved_at")
    op.execute("ALTER TABLE eval_results DROP COLUMN IF EXISTS resolution_reason")
    op.execute("ALTER TABLE eval_results DROP COLUMN IF EXISTS resolution")
