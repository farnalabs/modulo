"""FAR-969: composite org-scoped FK for ``eval_results.eval_id``.

``eval_results.eval_id`` carried a plain FK to ``evals.id`` — repointed there
by ``0254_eval_backfill_cutover``. A plain FK only proves the referenced row
exists: Postgres referential-integrity checks run with elevated privilege and
do NOT consult the ``rls_org_isolation`` policy on the referenced table, so a
row could bind an ``eval_id`` belonging to another organisation whenever the
same-org tenant trigger were absent or disabled. The sibling tables created by
the same eval / policy-gate taxonomy — ``policy_gates`` and
``policy_gate_decisions`` (``0250_eval_policy_gate``) — already use a composite
``(eval_id, organisation_id)`` FK instead.

This migration brings ``eval_results`` onto that same contract:

1. ensures ``evals`` carries ``uq_evals_id_organisation_id`` — the referenced
   ``(id, organisation_id)`` pair must be UNIQUE. The constraint was created by
   ``0250``; it is re-added here only when an out-of-band deployment is missing
   it, so the migration is safe on any chain state.
2. swaps the single-column ``eval_results_eval_id_fkey`` for the composite
   ``fk_eval_results_eval_org`` on ``(eval_id, organisation_id)`` referencing
   ``evals(id, organisation_id)`` with ``ON DELETE CASCADE`` — preserving the
   old FK's delete action.

The tenant trigger ``trg_eval_results_eval_id_tenant`` is deliberately left in
place: it and the composite FK are independent same-org guards.

Postgres-only: SQLite/ORM-created schemas get the composite FK from the model
``create_all`` (the 0246 / 0250 precedent). Downgrade restores the plain FK.

Revision ID: 0294_eval_results_org_fk
Revises: 0293_oauth_clients_team_id
Create Date: 2026-10-10
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0294_eval_results_org_fk"
down_revision: str | None = "0293_oauth_clients_team_id"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Constraint names (all literals — no interpolation reaches ``op.execute``).
_UNIQUE = "uq_evals_id_organisation_id"
_OLD_FK = "eval_results_eval_id_fkey"
_NEW_FK = "fk_eval_results_eval_org"

# Static DDL, built as literals so no f-string identifier interpolation appears
# in this module (the FAR-915 ``migration-fstring-sql`` semgrep rule).
_ENSURE_UNIQUE_SQL = "ALTER TABLE evals ADD CONSTRAINT uq_evals_id_organisation_id UNIQUE (id, organisation_id)"
_ADD_COMPOSITE_FK_SQL = (
    "ALTER TABLE eval_results ADD CONSTRAINT fk_eval_results_eval_org "
    "FOREIGN KEY (eval_id, organisation_id) "
    "REFERENCES evals (id, organisation_id) ON DELETE CASCADE"
)
_ADD_PLAIN_FK_SQL = (
    "ALTER TABLE eval_results ADD CONSTRAINT eval_results_eval_id_fkey "
    "FOREIGN KEY (eval_id) REFERENCES evals (id) ON DELETE CASCADE"
)


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _constraint_exists(constraint: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": constraint},
        )
        .scalar()
    )


def upgrade() -> None:
    if not _is_postgres():
        return
    if not _constraint_exists(_UNIQUE):
        op.execute(text(_ENSURE_UNIQUE_SQL))
    op.execute(text('ALTER TABLE eval_results DROP CONSTRAINT IF EXISTS "eval_results_eval_id_fkey"'))
    op.execute(text('ALTER TABLE eval_results DROP CONSTRAINT IF EXISTS "fk_eval_results_eval_org"'))
    op.execute(text(_ADD_COMPOSITE_FK_SQL))


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(text('ALTER TABLE eval_results DROP CONSTRAINT IF EXISTS "fk_eval_results_eval_org"'))
    op.execute(text('ALTER TABLE eval_results DROP CONSTRAINT IF EXISTS "eval_results_eval_id_fkey"'))
    op.execute(text(_ADD_PLAIN_FK_SQL))
