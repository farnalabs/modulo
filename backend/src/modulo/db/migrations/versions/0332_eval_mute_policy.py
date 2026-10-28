"""Eval/guardrail mute policy (FAR-2028).

Adds acceptance rate caching and mute policy fields to evals table.
When a definition's acceptance rate falls below a threshold across a minimum
number of recorded outcomes, it is auto-muted (stops interrupting but keeps
recording findings).

Revision ID: 0332_eval_mute_policy
Revises: 0331_eval_result_finding_resolution
Create Date: 2026-10-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0332_eval_mute_policy"
down_revision: str | None = "0331_eval_result_finding_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only run on Postgres (other backends come from ORM create_all)
    if not str(op.get_bind().dialect.name).startswith("postgres"):
        return

    # Add acceptance rate and mute policy columns
    op.execute("ALTER TABLE evals ADD COLUMN IF NOT EXISTS acceptance_rate DOUBLE PRECISION")
    op.execute("ALTER TABLE evals ADD COLUMN IF NOT EXISTS is_muted BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE evals ADD COLUMN IF NOT EXISTS muted_at TIMESTAMPTZ")
    op.execute("ALTER TABLE evals ADD COLUMN IF NOT EXISTS muted_by UUID REFERENCES accounts(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE evals ADD COLUMN IF NOT EXISTS mute_threshold DOUBLE PRECISION")
    op.execute("ALTER TABLE evals ADD COLUMN IF NOT EXISTS mute_min_outcomes INTEGER")

    # Add CHECK constraints
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_evals_mute_threshold' "
        "AND conrelid = 'public.evals'::regclass) THEN "
        "ALTER TABLE public.evals ADD CONSTRAINT ck_evals_mute_threshold "
        "CHECK (mute_threshold IS NULL OR mute_threshold BETWEEN 0 AND 1); END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_evals_mute_min_outcomes' "
        "AND conrelid = 'public.evals'::regclass) THEN "
        "ALTER TABLE public.evals ADD CONSTRAINT ck_evals_mute_min_outcomes "
        "CHECK (mute_min_outcomes IS NULL OR mute_min_outcomes > 0); END IF; END $$;"
    )

    # Index for muted evals lookup
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evals_org_is_muted ON evals (organisation_id, is_muted) WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    if not str(op.get_bind().dialect.name).startswith("postgres"):
        return

    op.execute("DROP INDEX IF EXISTS ix_evals_org_is_muted")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_evals_mute_min_outcomes' "
        "AND conrelid = 'public.evals'::regclass) THEN "
        "ALTER TABLE public.evals DROP CONSTRAINT ck_evals_mute_min_outcomes; END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_evals_mute_threshold' "
        "AND conrelid = 'public.evals'::regclass) THEN "
        "ALTER TABLE public.evals DROP CONSTRAINT ck_evals_mute_threshold; END IF; END $$;"
    )
    op.execute("ALTER TABLE evals DROP COLUMN IF EXISTS mute_min_outcomes")
    op.execute("ALTER TABLE evals DROP COLUMN IF EXISTS mute_threshold")
    op.execute("ALTER TABLE evals DROP COLUMN IF EXISTS muted_by")
    op.execute("ALTER TABLE evals DROP COLUMN IF EXISTS muted_at")
    op.execute("ALTER TABLE evals DROP COLUMN IF EXISTS is_muted")
    op.execute("ALTER TABLE evals DROP COLUMN IF EXISTS acceptance_rate")
