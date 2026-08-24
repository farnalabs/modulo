"""Tenant-isolate run_evidence via organisation_id + RLS (FAR-?).

Revision ID: 0133_run_evidence_rls
Revises: 0132_agent_connector_report_soft_delete_audit
Create Date: 2026-08-23

``run_evidence`` was created in 0110 without tenant scoping (it carries no
``organisation_id`` and is not covered by any RLS policy). This closes that
tenant-isolation gap:

  * Add ``organisation_id`` (NOT NULL, FK -> organisations) derived from the
    parent run's org, backfilling all existing rows via the ``run_id`` FK.
  * ENABLE + FORCE ROW LEVEL SECURITY so even the table owner is confined.
  * Add the canonical ``rls_org_isolation`` policy (org-scope USING on
    ``app.organisation_id``), mirroring every other tenant table.
  * GRANT full DML to ``modulo_app`` (the runtime role).

``run_evidence`` already exists, so this is an ALTER (not a ``modulo_migrate``
ownership ceremony): FORCE RLS is what confines the app/owner role here, which
is the same mechanism the 0066 ceremony relies on for newly-created tables.

Postgres-only: the column/FK/RLS/policy are all Postgres-specific; SQLite relies
on app-level tenant filtering.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0133_run_evidence_rls"
down_revision: str | None = "0132_agent_connector_report_soft_delete_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "modulo_app"
_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"


def _role_exists(bind, role: str) -> bool:
    """Return True when the Postgres role exists (fresh dev/BDD DBs have none)."""
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("SET search_path TO public")

    # 1. Add the column (nullable first so the backfill can run).
    op.execute(sa.text('ALTER TABLE public."run_evidence" ADD COLUMN IF NOT EXISTS organisation_id uuid'))

    # 2. Backfill every existing row from its parent run's organisation.
    op.execute(
        sa.text(
            "UPDATE public.run_evidence re "
            "SET organisation_id = r.organisation_id "
            "FROM public.runs r "
            "WHERE re.run_id = r.id AND re.organisation_id IS NULL"
        )
    )

    # 3. Now enforce NOT NULL + FK + index.
    op.execute(sa.text('ALTER TABLE public."run_evidence" ALTER COLUMN organisation_id SET NOT NULL'))
    op.execute(
        sa.text(
            'ALTER TABLE public."run_evidence" '
            "ADD CONSTRAINT fk_run_evidence_organisation_id "
            "FOREIGN KEY (organisation_id) REFERENCES organisations(id) ON DELETE CASCADE"
        )
    )
    op.execute(
        sa.text('CREATE INDEX IF NOT EXISTS ix_run_evidence_organisation_id ON public."run_evidence" (organisation_id)')
    )

    # 4. Confine with RLS (FORCE so the owner role is also subject to it).
    op.execute(sa.text('ALTER TABLE public."run_evidence" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('ALTER TABLE public."run_evidence" FORCE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON public."run_evidence" USING ({_ORG_SCOPE})'))

    # 5. Runtime role needs DML (guarded on the role existing — on fresh dev/BDD
    # databases the app roles are bootstrapped after alembic runs).
    if _role_exists(bind, _APP_ROLE):
        op.execute(sa.text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON public."run_evidence" TO {_APP_ROLE}'))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("SET search_path TO public")
    op.execute(sa.text('DROP POLICY IF EXISTS rls_org_isolation ON public."run_evidence"'))
    op.execute(sa.text('ALTER TABLE public."run_evidence" DISABLE ROW LEVEL SECURITY'))
    op.execute(sa.text('ALTER TABLE public."run_evidence" DROP CONSTRAINT IF EXISTS fk_run_evidence_organisation_id'))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_run_evidence_organisation_id"))
    op.execute(sa.text('ALTER TABLE public."run_evidence" DROP COLUMN IF EXISTS organisation_id'))
