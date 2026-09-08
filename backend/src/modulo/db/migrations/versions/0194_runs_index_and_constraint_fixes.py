"""Drop redundant ix_runs_refusal, add CHECK constraints, add idempotency unique.

Revision ID: 0194_runs_index_and_constraint_fixes
Revises: 0193_run_node_outputs_sweep_index
Create Date: 2026-09-08
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0194_runs_index_and_constraint_fixes"
down_revision = "0193_run_node_outputs_sweep_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop redundant index ix_runs_refusal — it is a strict prefix of
    #    ix_runs_org_created_pipeline (organisation_id, created_at) INCLUDE (pipeline_id).
    #    The INCLUDE column does not affect selectivity; both indexes serve the same
    #    leading-key lookups. Dropping it halves write amplification on the runs table.
    op.execute("DROP INDEX IF EXISTS ix_runs_refusal")

    # 2. Temporal ordering: completed_at >= started_at >= created_at.
    #    Prevents impossible timelines where a run completes before it starts.
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_temporal_ordering "
        "CHECK (started_at IS NULL OR started_at >= created_at)"
    )
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_completed_after_started "
        "CHECK (completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at))"
    )

    # 3. Self-referential FK cycle prevention: parent_run_id != id.
    #    Prevents direct self-loops (A→A) that cause infinite recursion in tree-walk CTEs.
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_parent_not_self CHECK (parent_run_id IS NULL OR parent_run_id != id)"
    )

    # 4. Non-negative counters: claim_count and node_attempt_count must be >= 0.
    #    A bug could decrement below zero; the CHECK prevents silent data corruption.
    op.execute("ALTER TABLE runs ADD CONSTRAINT ck_runs_claim_count_nonneg CHECK (claim_count >= 0)")
    op.execute("ALTER TABLE runs ADD CONSTRAINT ck_runs_node_attempt_count_nonneg CHECK (node_attempt_count >= 0)")

    # 5. Partial unique index on idempotency_key — prevents duplicate work from
    #    concurrent create_run calls with the same idempotency key.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_idempotency "
        "ON runs (pipeline_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_runs_idempotency")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_node_attempt_count_nonneg")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_claim_count_nonneg")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_parent_not_self")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_completed_after_started")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS ck_runs_temporal_ordering")
    op.execute("CREATE INDEX ix_runs_refusal ON runs (organisation_id, created_at)")
