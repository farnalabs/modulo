"""Reconcile pre-squash staging schema drift that blocks ``alembic upgrade heads``.

Revision ID: 0064_reconcile_staging_schema
Revises: 0037_break_glass_enforcement, 0037_add_scheduled_reports_created_by
Create Date: 2026-08-03

The staging database (``staging-modulo`` on Fly) is a pre-squash schema stamped
at ``0010_fix_enforce_same_organisation_non_uuid`` whose actual tables come from
the OLD pre-squash migration chain. Three drift items made ``alembic upgrade
heads`` fail at ``0011_database_review_fixes`` (now drift-tolerant), which hard
-blocked the entire deploy pipeline:

1. ``mcp_setup_tokens`` is MISSING entirely (0011's FK
   ``fk_mcp_setup_tokens_created_by`` had no target table).
2. ``lifecycle_maps`` is MISSING entirely (0011's index
   ``ix_lifecycle_maps_account_id`` had no target table).
3. ``scheduled_reports`` is a LEGACY pre-squash shape (columns
   ``id, organisation_id, period, group_by, format, recipients,
   schedule_type, account_id, next_run_at, created_at, updated_at``, 0 rows) —
   it lacks the current ``created_by`` column that 0011's index
   ``ix_scheduled_reports_created_by`` needs.

This migration repairs all three, mirroring the exact DDL from
``0005_v2_features_system`` (so the migrated schema matches the SQLAlchemy ORM
metadata exactly) plus the pieces 0011 adds on a healthy DB (the ``created_by``
FK/indexes). Because it (re)creates these tables, it also reinstalls the same
tenant-isolation triggers 0005 installs on a fresh schema
(``trg_<table>_<column>_tenant`` calling ``enforce_same_organisation()``) —
otherwise the repaired schema would permanently lack that cross-org FK
enforcement (defense-in-depth on top of RLS) that fresh/prod have.

IDEMPOTENCY: every step is guarded by an existence check. On a healthy DB the
three tables already exist in their current shape, so each branch is a no-op —
``upgrade()`` creates nothing and the schema-parity canary
(``test_migrated_schema_matches_orm_metadata``) is unaffected. Only on the
drifted staging DB does it (re)create.

The ``scheduled_reports`` legacy shape is detected by the presence of the
``period`` column OR the absence of ``created_by``. The legacy table is dropped
and recreated with the current ORM schema ONLY when that legacy shape is
detected AND the table is empty (0 rows, verified on staging); a populated
legacy table is left untouched with a warning rather than risking data loss.

Interaction with ``0037_add_scheduled_reports_created_by`` (PR #615): that
migration adds the ``created_by`` column + partial index to whatever
``scheduled_reports`` shape exists. On the legacy staging table this leaves the
table still legacy-shaped (``period`` etc. remain), so this migration's
``period``-based detection still fires and the drop+recreate reconciles the full
table. A healthy table is unaffected by both. This migration is a graph merge
point (tuple ``down_revision``) resolving the parallel heads #612 and #615.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_reconcile_staging_schema"
down_revision: str | Sequence[str] | None = (
    "0037_break_glass_enforcement",
    "0037_add_scheduled_reports_created_by",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRICT_RLS = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_TENANT_TRIGGERS: dict[str, tuple[tuple[str, str], ...]] = {
    "mcp_setup_tokens": (("created_by", "accounts"),),
    "lifecycle_maps": (("account_id", "accounts"), ("owner_team_id", "teams")),
    "scheduled_reports": (("created_by", "accounts"),),
}


def _table_exists(bind: object, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _column_exists(bind: object, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def _warn(message: str) -> None:
    print(message)  # noqa: T201 - printed warning surfaces in deploy logs


def _enable_rls(table: str) -> None:
    """Mirror 0005 ``_enable_rls`` for a single strict-RLS table."""
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{table}" USING ({_STRICT_RLS})'))


def _create_tenant_triggers(table: str) -> None:
    """Install the 0005 tenant-isolation triggers for a (re)created table.

    0005's ``_create_triggers`` installs ``trg_<table>_<column>_tenant`` triggers
    (calling ``enforce_same_organisation()``) on a fresh schema. 0064 (re)created
    these tables here, so it must reinstall the exact same triggers or the
    repaired schema would permanently lack the cross-org FK enforcement that
    fresh/prod have. Only called from the create/recreate branches, so on a
    healthy DB (tables already present) this emits nothing.
    """
    for child_column, parent_table in _TENANT_TRIGGERS[table]:
        trigger = f"trg_{table}_{child_column}_tenant"
        _warn(f"CREATE trigger {trigger}: restoring 0005 tenant-isolation enforcement")
        op.execute(
            sa.text(
                f'CREATE TRIGGER "{trigger}" '
                f'BEFORE INSERT OR UPDATE OF "{child_column}", "organisation_id" ON "{table}" '
                f"FOR EACH ROW EXECUTE FUNCTION enforce_same_organisation('{parent_table}', '{child_column}')"
            )
        )


def _create_mcp_setup_tokens() -> None:
    """Create mcp_setup_tokens if missing (0005 lines 536-557 + 0011 FK)."""
    bind = op.get_bind()
    if _table_exists(bind, "mcp_setup_tokens"):
        _warn("SKIP mcp_setup_tokens: table exists (drift-tolerant)")
        return
    _warn("CREATE mcp_setup_tokens: table missing (reconciling staging drift)")
    op.create_table(
        "mcp_setup_tokens",
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_mcp_setup_tokens_organisation_id"), "mcp_setup_tokens", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_mcp_setup_tokens_resource_id"), "mcp_setup_tokens", ["resource_id"], unique=False)
    op.create_foreign_key(
        "fk_mcp_setup_tokens_created_by",
        "mcp_setup_tokens",
        "accounts",
        ["created_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    _enable_rls("mcp_setup_tokens")
    _create_tenant_triggers("mcp_setup_tokens")


def _create_lifecycle_maps() -> None:
    """Create lifecycle_maps if missing (0005 lines 146-172 + deleted_at + 0011 index)."""
    bind = op.get_bind()
    if _table_exists(bind, "lifecycle_maps"):
        _warn("SKIP lifecycle_maps: table exists (drift-tolerant)")
        return
    _warn("CREATE lifecycle_maps: table missing (reconciling staging drift)")
    op.create_table(
        "lifecycle_maps",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(length=10), server_default="org", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("visibility IN ('org', 'team')", name="ck_lifecycle_maps_visibility"),
        sa.CheckConstraint("visibility = 'org' OR owner_team_id IS NOT NULL", name="ck_lifecycle_maps_team_owner"),
        sa.CheckConstraint("version > 0", name="ck_lifecycle_maps_version"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_lifecycle_maps_organisation_id"), "lifecycle_maps", ["organisation_id"], unique=False)
    op.create_index(op.f("ix_lifecycle_maps_account_id"), "lifecycle_maps", ["account_id"], unique=False)
    _enable_rls("lifecycle_maps")
    _create_tenant_triggers("lifecycle_maps")


def _reconcile_scheduled_reports() -> None:
    """Drop+recreate legacy scheduled_reports with the current ORM schema.

    Only fires when the table has the legacy pre-squash shape (``period``
    column OR missing ``created_by``) AND is empty. A healthy table is left
    untouched; a populated legacy table is left untouched with a warning.
    """
    bind = op.get_bind()
    if not _table_exists(bind, "scheduled_reports"):
        _warn("SKIP scheduled_reports: table missing (drift-tolerant; nothing to reconcile)")
        return
    is_legacy = _column_exists(bind, "scheduled_reports", "period") or not _column_exists(
        bind, "scheduled_reports", "created_by"
    )
    if not is_legacy:
        _warn("SKIP scheduled_reports: current schema detected (drift-tolerant)")
        return
    rows = bind.execute(sa.text('SELECT count(*) FROM "scheduled_reports"')).scalar_one()
    if rows > 0:
        _warn(f"SKIP scheduled_reports: legacy shape but {rows} rows present — refusing to drop (manual review needed)")
        return
    _warn("RECONCILE scheduled_reports: legacy pre-squash shape, dropping and recreating")
    op.drop_table("scheduled_reports")
    op.create_table(
        "scheduled_reports",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("report_type", sa.String(length=50), nullable=False),
        sa.Column("cron_expression", sa.String(length=100), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("recipient_config", sa.JSON(), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_scheduled_reports_organisation_id"), "scheduled_reports", ["organisation_id"], unique=False
    )
    op.create_index(op.f("ix_scheduled_reports_report_type"), "scheduled_reports", ["report_type"], unique=False)
    op.create_index(
        op.f("ix_scheduled_reports_created_by"),
        "scheduled_reports",
        ["created_by"],
        unique=False,
        postgresql_where=sa.text("created_by IS NOT NULL"),
    )
    _enable_rls("scheduled_reports")
    _create_tenant_triggers("scheduled_reports")


def upgrade() -> None:
    _create_mcp_setup_tokens()
    _create_lifecycle_maps()
    _reconcile_scheduled_reports()


def downgrade() -> None:
    pass
