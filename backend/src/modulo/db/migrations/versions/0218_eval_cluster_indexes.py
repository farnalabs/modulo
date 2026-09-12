"""Add missing hot-path indexes for the eval cluster.

Revision ID: 0218_eval_cluster_indexes
Revises: 0217_hitl_claims_decided_by
Create Date: 2026-09-12

Adds composite indexes that cover the eval cluster's dominant query paths:

1. eval_definitions: partial composite for the guardrail-loading hot path
   (called on every run creation, pipeline graph save, snapshot pinning).
2. eval_definitions: index on eval_suite_id (model declares index=True but
   migration 0130 used raw SQL, so no index was created).
3. eval_results: composite (organisation_id, evaluated_at) for dashboard
   date-range queries and regression detection scans.
4. suite_runs: composite (organisation_id, suite_id, notified_at) for the
   alert rate-limiting query that fires on every suite completion.
5. suite_runs: composite (organisation_id, created_at) for the daily-spend
   range scan that fires on every suite_run execution.
6. suite_runs: composite (organisation_id, suite_id, dataset_id, state) for
   the concurrency-limit active-count check before firing new runs.
7. eval_cases: composite (organisation_id, dataset_id) for the dominant
   org-scoped case-fetch pattern.

All indexes are created with plain ``CREATE INDEX IF NOT EXISTS`` — NOT
``CREATE INDEX CONCURRENTLY`` — because Alembic runs every revision inside a
single transaction (``env.py`` wraps each migration in one ``engine.begin()``),
and ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction block. This
follows the deploy-safety precedent of 0128/0154/0155/0171/0182/0193/0200.
``IF NOT EXISTS`` keeps the migration idempotent and re-runnable. Downgrade
drops every index created here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0218_eval_cluster_indexes"
down_revision: str | None = "0217_hitl_claims_decided_by"
branch_labels: str | None = None
depends_on: str | None = None


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")

    # 1. Guardrail-loading hot path (eval_definitions).
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_eval_definitions_org_pipeline_type_active "
        "ON eval_definitions (organisation_id, pipeline_id, eval_type) "
        "WHERE deleted_at IS NULL"
    )

    # 2. eval_suite_id FK lookup (model declares index=True but 0130 used
    #    raw SQL so no index was created).
    op.execute("CREATE INDEX IF NOT EXISTS ix_eval_definitions_eval_suite_id ON eval_definitions (eval_suite_id)")

    # 3. Dashboard date-range + regression scan (eval_results).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_results_org_evaluated_at ON eval_results (organisation_id, evaluated_at)"
    )

    # 4. Alert rate-limiting query (suite_runs).
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_suite_runs_org_suite_notified "
        "ON suite_runs (organisation_id, suite_id, notified_at)"
    )

    # 5. Daily-spend range scan (suite_runs).
    op.execute("CREATE INDEX IF NOT EXISTS ix_suite_runs_org_created ON suite_runs (organisation_id, created_at)")

    # 6. Concurrency-limit active-count check (suite_runs).
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_suite_runs_active_check "
        "ON suite_runs (organisation_id, suite_id, dataset_id, state)"
    )

    # 7. Org-scoped case fetch (eval_cases).
    op.execute("CREATE INDEX IF NOT EXISTS ix_eval_cases_org_dataset ON eval_cases (organisation_id, dataset_id)")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")

    op.execute("DROP INDEX IF EXISTS ix_eval_cases_org_dataset")
    op.execute("DROP INDEX IF EXISTS ix_suite_runs_active_check")
    op.execute("DROP INDEX IF EXISTS ix_suite_runs_org_created")
    op.execute("DROP INDEX IF EXISTS ix_suite_runs_org_suite_notified")
    op.execute("DROP INDEX IF EXISTS ix_eval_results_org_evaluated_at")
    op.execute("DROP INDEX IF EXISTS ix_eval_definitions_eval_suite_id")
    op.execute("DROP INDEX IF EXISTS ix_eval_definitions_org_pipeline_type_active")
