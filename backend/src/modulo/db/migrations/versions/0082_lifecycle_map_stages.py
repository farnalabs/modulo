"""Create lifecycle_map_stages junction table (journey/map-stage projection)

Revision ID: 0082_lifecycle_map_stages
Revises: 0081_pipeline_retry_policy
Create Date: 2026-08-11

The ``lifecycle_map_stages`` table is a READ projection derived from
``lifecycle_maps.content_json`` (see ``modulo.db.models.lifecycle_map_stage``).
Rows are replaced wholesale on every map-version save; ``content_json`` remains
the canonical source of truth.

Tenant model mirrors ``lifecycle_maps``: strict org RLS
(``rls_org_isolation``) plus ``enforce_same_organisation`` tenant triggers on
``map_id``, ``pipeline_id`` and ``account_id``.

The partial unique index ``uq_lifecycle_map_stages_active_pipeline`` enforces
"a pipeline may be a stage of at most one active map". Junction rows are
removed when a map is soft-deleted, so the partial index alone guarantees the
invariant among non-soft-deleted maps.

A best-effort backfill derives rows for existing maps whose content is
shape-compatible; incompatible maps are skipped (their rows are re-derived on
the next save, which validates).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082_lifecycle_map_stages"
down_revision: str | None = "0081_pipeline_retry_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "lifecycle_map_stages"

# (child_column, parent_table) — mirrors the lifecycle_maps tenant triggers.
_TENANT_TRIGGERS: tuple[tuple[str, str], ...] = (
    ("map_id", "lifecycle_maps"),
    ("pipeline_id", "pipelines"),
    ("account_id", "accounts"),
)


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


def _backfill_existing_maps() -> None:
    """Derive junction rows for existing non-soft-deleted maps' content_json.

    Only shape-compatible stage rows are inserted (id/name/type present and a
    valid type). Non-UUID pipeline ids are stored as NULL (re-derived on the
    next validated save). Incompatible maps are left untouched.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("lifecycle_maps") or not insp.has_table(_TABLE):
        return
    result = bind.execute(
        sa.text(
            """
            INSERT INTO lifecycle_map_stages (
                id, organisation_id, map_id, version, stage_id, stage_name,
                position, stage_type, pipeline_id, account_id, created_at, updated_at
            )
            SELECT DISTINCT ON (lm.id, st ->> 'id')
                gen_random_uuid(),
                lm.organisation_id,
                lm.id,
                lm.version,
                st ->> 'id',
                st ->> 'name',
                t.ord,
                st ->> 'type',
                CASE
                    WHEN st ->> 'pipeline_id' ~*
                        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                    THEN (st ->> 'pipeline_id')::uuid
                    ELSE NULL
                END,
                lm.account_id,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM lifecycle_maps lm
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(lm.content_json::jsonb -> 'stages') = 'array'
                     THEN lm.content_json::jsonb -> 'stages'
                     ELSE '[]'::jsonb
                END
            ) WITH ORDINALITY AS t(st, ord)
            WHERE lm.deleted_at IS NULL
              AND st ->> 'id' IS NOT NULL
              AND st ->> 'id' <> ''
              AND st ->> 'name' IS NOT NULL
              AND st ->> 'type' IN ('modulo', 'external', 'manual', 'placeholder')
            ORDER BY lm.id, st ->> 'id'
            ON CONFLICT (organisation_id, pipeline_id) WHERE pipeline_id IS NOT NULL DO NOTHING
            """
        )
    )
    _log_rows = result.rowcount if result is not None else 0
    if _log_rows:
        print(f"[migration 0082] backfilled {_log_rows} lifecycle_map_stages row(s)")  # noqa: T201


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("map_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("stage_id", sa.String(length=255), nullable=False),
        sa.Column("stage_name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stage_type", sa.String(length=20), nullable=False),
        sa.Column("pipeline_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "stage_type IN ('modulo', 'external', 'manual', 'placeholder')",
            name="ck_lifecycle_map_stages_type",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["map_id"], ["lifecycle_maps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("map_id", "version", "stage_id", name="uq_lifecycle_map_stages_map_version_stage"),
    )
    op.create_index(op.f("ix_lifecycle_map_stages_organisation_id"), _TABLE, ["organisation_id"], unique=False)
    op.create_index(op.f("ix_lifecycle_map_stages_map_id"), _TABLE, ["map_id"], unique=False)
    op.create_index(
        "uq_lifecycle_map_stages_active_pipeline",
        _TABLE,
        ["organisation_id", "pipeline_id"],
        unique=True,
        postgresql_where=sa.text("pipeline_id IS NOT NULL"),
    )
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    # Literal DDL so the RLS-coverage architecture test can detect this table
    # (it scans for `ALTER TABLE "<table>" ENABLE ROW LEVEL SECURITY`).
    op.execute(sa.text('ALTER TABLE "lifecycle_map_stages" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{_TABLE}" USING ({strict})'))
    _create_tenant_triggers()
    _backfill_existing_maps()


def downgrade() -> None:
    for child_column, _parent_table in _TENANT_TRIGGERS:
        trigger = f"trg_{_TABLE}_{child_column}_tenant"
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS "{trigger}" ON "{_TABLE}"'))
    op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{_TABLE}"'))
    op.execute(sa.text(f'ALTER TABLE "{_TABLE}" DISABLE ROW LEVEL SECURITY'))
    op.drop_index("uq_lifecycle_map_stages_active_pipeline", table_name=_TABLE)
    op.drop_index(op.f("ix_lifecycle_map_stages_map_id"), table_name=_TABLE)
    op.drop_index(op.f("ix_lifecycle_map_stages_organisation_id"), table_name=_TABLE)
    op.drop_table(_TABLE)
