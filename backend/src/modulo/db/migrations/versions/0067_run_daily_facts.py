"""run_daily_facts — the analytics denormalised fact table (ADR 020).

Revision ID: 0067_run_daily_facts
Revises: 0066_cost_components
Create Date: 2026-08-05

THE REAL MIGRATION TREE (normative): the live head is ``0066_cost_components``
(revision id "0066_cost_components", down_revision "0065_reconcile_staging_schema").
This is a NORMAL migration off the ACTUAL head (``0066_cost_components``),
deployed via the EXISTING ``upgrade heads`` (plural) form — NO pin.

DDL MAINTENANCE-WINDOW FLAG: this migration runs in ONE transaction and holds
the ACCESS EXCLUSIVE lock for TWO blocking CREATE INDEX
(``ix_run_daily_facts_org_date``, ``uq_run_daily_facts_run_id``) on the new
table (an empty table — a fresh table's index build is cheap). Fine at
dogfood scale; budget a MAINTENANCE WINDOW at production scale if this table
has grown large before the index build.

ROLE WIRING (the 0066 ceremony, verbatim): the migration connects via
``DATABASE_ADMIN_URL`` (env.py:52-55 — the superuser/owner URL).
``modulo_migrate`` is a NOLOGIN role (bootstrap role.py), so it cannot be
CONNECTED to — the migration executes ``SET ROLE modulo_migrate`` BEFORE
``op.create_table("run_daily_facts", ...)``, then ``RESET ROLE`` AFTER (the
RLS-enable + policy + grant steps run as the migration's caller). The
post-create ownership assertion verifies the created table's owner is
``modulo_migrate``, not the app role — the owner-bypasses-RLS precondition for
``run_daily_facts`` RLS confinement.

The ``modulo_migrate`` role needs CREATE on the public schema + REFERENCES on
the tables its new FKs reference (``organisations``, ``pipelines``, ``teams``,
``pipeline_folders``) — re-applied idempotently right before ``SET ROLE`` (the
pre-alembic bootstrap runs on a fresh DB where those tables do not exist yet).

``run_id`` has NO foreign key by design: facts must survive the 90-day run
purge (``batch_delete_old_terminal_runs``). A future "fix" into an FK breaks
retention (ADR 020).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_run_daily_facts"
down_revision: str | None = "0066_cost_components"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"

_STRICT_RLS = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _assert_run_daily_facts_owner_is_migrate(bind: sa.Connection) -> None:
    """POST-CREATE ownership assertion — the 0066 ceremony (before RLS)."""
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass('public.run_daily_facts')")
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            f"run_daily_facts owner is {owner!r}, expected '{_MIGRATE_ROLE}' "
            "(the app role must NOT own run_daily_facts — owner bypasses RLS)"
        )


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    # SET search_path pinned at the top (schema-qualified references resolve
    # against a pinned path, not the session's ambient search_path).
    if pg:
        op.execute("SET search_path TO public")

    # 1. run_daily_facts table — created under SET ROLE modulo_migrate so the
    #    owner is the MIGRATE role (the app role must NOT own it: the owner
    #    bypasses RLS). ``run_id`` has a UNIQUE index but NO FK — facts survive
    #    the 90-day run purge (ADR 020).
    if pg:
        op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
        op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")
        op.execute(f"GRANT REFERENCES ON TABLE public.pipelines TO {_MIGRATE_ROLE}")
        op.execute(f"GRANT REFERENCES ON TABLE public.teams TO {_MIGRATE_ROLE}")
        op.execute(f"GRANT REFERENCES ON TABLE public.pipeline_folders TO {_MIGRATE_ROLE}")
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")
    op.create_table(
        "run_daily_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=True),
        sa.Column("team_name", sa.String(length=255), nullable=True),
        sa.Column("pipeline_id", sa.Uuid(), nullable=True),
        sa.Column("pipeline_name", sa.String(length=255), nullable=True),
        sa.Column("folder_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("total_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["folder_id"], ["pipeline_folders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="SET NULL"),
    )
    if pg:
        op.execute("RESET ROLE")
        _assert_run_daily_facts_owner_is_migrate(bind)
        # Plain CREATE INDEX — no CONCURRENTLY (the 0066 precedent). See the
        # maintenance-window flag in the docstring.
        op.create_index("ix_run_daily_facts_org_date", "run_daily_facts", ["organisation_id", "run_date"])
        op.create_index("uq_run_daily_facts_run_id", "run_daily_facts", ["run_id"], unique=True)

    # 2. RLS enable + policy (the 0008_rls_pipeline_folders / 0066 pattern) and
    #    the direct PUBLIC table grant (Postgres-only; RLS is the confinement —
    #    the grant is role-agnostic by design and every write path calls
    #    set_rls_org).
    if pg:
        op.execute("ALTER TABLE run_daily_facts ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON run_daily_facts USING ({_STRICT_RLS})")
        op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON run_daily_facts TO PUBLIC")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("DROP POLICY IF EXISTS rls_org_isolation ON run_daily_facts")
        op.execute("ALTER TABLE run_daily_facts DISABLE ROW LEVEL SECURITY")
        op.drop_index("uq_run_daily_facts_run_id", table_name="run_daily_facts")
        op.drop_index("ix_run_daily_facts_org_date", table_name="run_daily_facts")

    op.drop_table("run_daily_facts")
