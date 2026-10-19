"""Add missing eval-cluster hot-path indexes.

Revision ID: 0322_eval_cluster_hot_path_indexes
Revises: 0321_eval_cluster_jsonb_parity
Create Date: 2026-10-19

The eval cutover moved reads to ``evals`` but the partial hot-path index added
for ``eval_definitions`` by 0218 was never recreated on the new table. This adds
the surviving read paths that have no covering index:

* ``evals`` guardrail binding lookup: ``(organisation_id, pipeline_id, eval_type)``
  filtered to live rows — executed on every run creation and graph save.
* ``evals`` / ``eval_datasets`` / ``eval_suites`` list pages, ordered by
  ``created_at DESC`` (datasets/evals additionally filter live rows).
* ``eval_results`` run-detail listing: ``(run_id, evaluated_at DESC)``.
* ``suite_runs`` per-trigger daily-spend scan: the JSONB expression
  ``extra->>'trigger_id'`` combined with the org/day range.

Plain ``CREATE INDEX IF NOT EXISTS`` is used (not ``CONCURRENTLY``) because
Alembic runs the migration in a transaction; the repo's deploy path takes that
pattern for the same reason (0128/0154/0218/0272). Downgrade drops each index.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision: str = "0322_eval_cluster_hot_path_indexes"
down_revision: str | None = "0321_eval_cluster_jsonb_parity"
branch_labels: str | None = None
depends_on: str | None = None

_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")

_INDEXES: tuple[str, ...] = (
    "ix_evals_org_pipeline_type_active",
    "ix_evals_org_created_active",
    "ix_eval_datasets_org_created_active",
    "ix_eval_suites_org_created",
    "ix_eval_results_run_evaluated",
    "ix_suite_runs_org_trigger_created",
)


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _validate_identifier(name: str) -> None:
    """Guard an internal SQL identifier before it reaches the statement."""
    if _IDENTIFIER_RE.fullmatch(name) is None:
        raise ValueError(f"invalid SQL identifier: {name!r}")


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return

    op.execute("SET search_path TO public")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evals_org_pipeline_type_active "
        "ON public.evals (organisation_id, pipeline_id, eval_type) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_evals_org_created_active "
        "ON public.evals (organisation_id, created_at DESC) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_datasets_org_created_active "
        "ON public.eval_datasets (organisation_id, created_at DESC) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_suites_org_created ON public.eval_suites (organisation_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_results_run_evaluated ON public.eval_results (run_id, evaluated_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_suite_runs_org_trigger_created "
        "ON public.suite_runs (organisation_id, (extra->>'trigger_id'), created_at) "
        "WHERE extra IS NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return

    op.execute("SET search_path TO public")
    for index_name in reversed(_INDEXES):
        _validate_identifier(index_name)
        op.execute("DROP INDEX IF EXISTS public." + index_name)
