"""Add CHECK constraints and idempotency unique index (ix_runs_refusal is retained).

Revision ID: 0197_runs_index_and_constraint_fixes
Revises: 0194_uuid_pk_server_defaults
Create Date: 2026-09-08
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0197_runs_index_and_constraint_fixes"
down_revision = "0194_uuid_pk_server_defaults"
branch_labels = None
depends_on = None


def _add_check(name: str, expr: str) -> None:
    # Add NOT VALID so the ALTER TABLE does not take an ACCESS EXCLUSIVE lock
    # scanning/validating every existing row (which would hard-fail or wedge
    # the deploy on legacy data), then VALIDATE CONSTRAINT online with only a
    # SHARE UPDATE EXCLUSIVE lock. New inserts are still checked immediately.
    op.execute(f"ALTER TABLE runs ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID")
    op.execute(f"ALTER TABLE runs VALIDATE CONSTRAINT {name}")


def upgrade() -> None:
    # NOTE: ix_runs_refusal is intentionally NOT dropped here. It is declared on
    # the ``runs`` model (Index("ix_runs_refusal", ...)) and required by the
    # schema-parity / integration tests, so removing it would create model/DB
    # drift. It is a legitimate standalone index owned by 0110.

    # 1. CHECK constraints (added NOT VALID + VALIDATE, see _add_check).
    #    NOTE: the originally-planned ck_runs_temporal_ordering
    #    (started_at >= created_at) is intentionally NOT added — the application
    #    legitimately inserts late/replayed run records whose started_at predates
    #    their created_at, so that constraint would reject valid writes.
    _add_check(
        "ck_runs_completed_after_started",
        "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
    )
    _add_check("ck_runs_parent_not_self", "parent_run_id IS NULL OR parent_run_id != id")
    _add_check("ck_runs_claim_count_nonneg", "claim_count >= 0")
    _add_check("ck_runs_node_attempt_count_nonneg", "node_attempt_count >= 0")

    # 3. Surface any pre-existing duplicate (pipeline_id, idempotency_key) pairs
    #    BEFORE building the unique index, so a conflict fails loudly with an
    #    actionable message instead of a bare unique-violation.
    op.execute(
        "DO $$\n"
        "DECLARE dup_count int;\n"
        "BEGIN\n"
        "  SELECT count(*) INTO dup_count FROM (\n"
        "    SELECT pipeline_id, idempotency_key FROM runs\n"
        "    WHERE idempotency_key IS NOT NULL\n"
        "    GROUP BY pipeline_id, idempotency_key HAVING count(*) > 1\n"
        "  ) d;\n"
        "  IF dup_count > 0 THEN\n"
        "    RAISE EXCEPTION 'uq_runs_idempotency: % duplicate (pipeline_id, idempotency_key) pairs exist; de-duplicate before adding the unique index', dup_count;\n"
        "  END IF;\n"
        "END $$;"
    )

    # 4. Partial unique index on idempotency_key — prevents duplicate work from
    #    concurrent create_run calls with the same idempotency key.
    #    Plain CREATE UNIQUE INDEX (NOT CONCURRENTLY): Alembic wraps each revision
    #    in a single transaction, so CONCURRENTLY is unavailable here — consistent
    #    with 0154/0171/0182/0187/0193.
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
    # ix_runs_refusal is owned by 0110 and is no longer touched by this migration.
