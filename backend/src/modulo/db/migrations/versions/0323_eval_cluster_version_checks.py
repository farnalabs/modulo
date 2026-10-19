"""Add eval-cluster version-domain CHECK constraints.

Revision ID: 0323_eval_cluster_version_checks
Revises: 0322_eval_cluster_hot_path_indexes
Create Date: 2026-10-19

Two version columns have an application-level invariant that the database does
not enforce:

* ``evals.version`` starts at 1 and is only ever incremented.
* ``eval_results.eval_definition_version`` is the definition version that scored
  the result; it is NULL only for legacy rows and otherwise starts at 1.

Both constraints are declared on the ORM models as well, so ``create_all`` and
the migrated schema agree. They are added with the ``pg_constraint`` existence
guard used by 0157/0219 so a partially-applied run is re-runnable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0323_eval_cluster_version_checks"
down_revision: str | None = "0322_eval_cluster_hot_path_indexes"
branch_labels: str | None = None
depends_on: str | None = None

_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("ck_evals_version_gte_1", "evals", "version >= 1"),
    (
        "ck_eval_results_eval_definition_version_gte_1",
        "eval_results",
        "eval_definition_version IS NULL OR eval_definition_version >= 1",
    ),
)


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return

    op.execute("SET search_path TO public")
    for name, table_name, condition in _CHECKS:
        already_present = bind.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        ).scalar_one_or_none()
        if already_present is not None:
            continue
        bind.execute(sa.text(f'ALTER TABLE public."{table_name}" ADD CONSTRAINT {name} CHECK ({condition});'))


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        return

    op.execute("SET search_path TO public")
    for name, table_name, _condition in reversed(_CHECKS):
        bind.execute(sa.text(f'ALTER TABLE public."{table_name}" DROP CONSTRAINT IF EXISTS {name};'))
