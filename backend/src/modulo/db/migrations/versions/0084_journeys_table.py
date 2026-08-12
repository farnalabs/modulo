"""Create journeys table (work-item journey rows, FAR-142)

Revision ID: 0084_journeys_table
Revises: 0083_journey_work_item_columns
Create Date: 2026-08-12

The ``journeys`` table is the canonical record of one piece of work (a GitHub
issue, a Linear ticket, ...) across every Modulo run that touches it. Rows are
MINTED at create time from the run's canonicalised work-item refs
(``INSERT ... ON CONFLICT (organisation_id, kind, ref) DO NOTHING``);
``latest_*`` and ``run_count`` are owned by the finalise path (FAR-143) and are
never written here.

``canonical_work_item_id`` IS the deterministic journey id
(``uuid5(org, kind, ref)``) — derivable at create and again at finalise/query,
so there is no mint race and no overwrite.

Tenant model mirrors ``lifecycle_map_stages`` (FAR-141): strict org RLS
(``rls_org_isolation``) plus an ``enforce_same_organisation`` tenant trigger on
``owner_team_id``.

``latest_terminal_run_id`` is deliberately NOT a FK (the ``run_daily_facts``
precedent): the journey must survive the 90-day run purge.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0084_journeys_table"
down_revision: str | None = "0083_journey_work_item_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "journeys"

# (child_column, parent_table) — mirrors the lifecycle_map_stages tenant triggers.
_TENANT_TRIGGERS: tuple[tuple[str, str], ...] = (("owner_team_id", "teams"),)


def _create_tenant_triggers() -> None:
    for child_column, parent_table in _TENANT_TRIGGERS:
        trigger = f"trg_{_TABLE}_{child_column}_tenant"
        op.execute(
            sa.text(
                f'CREATE TRIGGER "{trigger}" '
                f'BEFORE INSERT OR UPDATE OF "{child_column}", "organisation_id" ON "{_TABLE}" '
                f"FOR EACH ROW EXECUTE FUNCTION enforce_same_organisation('{parent_table}', '{child_column}')"
            )
        )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=False),
        sa.Column("canonical_work_item_id", sa.Uuid(), nullable=False),
        sa.Column("latest_terminal_run_id", sa.Uuid(), nullable=True),
        sa.Column("map_id", sa.Uuid(), nullable=True),
        sa.Column("map_version", sa.Integer(), nullable=True),
        sa.Column("stage_id", sa.String(length=255), nullable=True),
        sa.Column("stage_name", sa.String(length=255), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("latest_status", sa.String(length=30), nullable=True),
        sa.Column("latest_provenance", sa.String(length=30), nullable=True),
        sa.Column("run_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organisation_id", "kind", "ref", name="uq_journeys_org_kind_ref"),
    )
    op.create_index(op.f("ix_journeys_organisation_id"), _TABLE, ["organisation_id"], unique=False)
    op.create_index("ix_journeys_canonical_work_item_id", _TABLE, ["canonical_work_item_id"], unique=False)
    # Literal DDL so the RLS-coverage architecture test can detect this table
    # (it scans for `ALTER TABLE "<table>" ENABLE ROW LEVEL SECURITY`).
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    op.execute(sa.text('ALTER TABLE "journeys" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{_TABLE}" USING ({strict})'))
    _create_tenant_triggers()


def downgrade() -> None:
    for child_column, _parent_table in _TENANT_TRIGGERS:
        trigger = f"trg_{_TABLE}_{child_column}_tenant"
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS "{trigger}" ON "{_TABLE}"'))
    op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{_TABLE}"'))
    op.execute(sa.text(f'ALTER TABLE "{_TABLE}" DISABLE ROW LEVEL SECURITY'))
    op.drop_index("ix_journeys_canonical_work_item_id", table_name=_TABLE)
    op.drop_index(op.f("ix_journeys_organisation_id"), table_name=_TABLE)
    op.drop_table(_TABLE)
