"""Consolidated: add unique constraint, create snapshot_schema_pins table, backfill data

Revision ID: 0025_consolidated_schema_pins_fix
Revises: 0020_add_missing_performance_indexes
"""
import contextlib

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0025_consolidated_schema_pins_fix"
down_revision = "0020_add_missing_performance_indexes"

def upgrade() -> None:
    conn = op.get_bind()

    # Step 1: Add system bool column (from 0021)
    op.add_column("schemas", sa.Column("system", sa.Boolean(), server_default=sa.text("false"), nullable=False))

    # Step 2: Add unique constraint to schema_versions (needed by FK below)
    try:
        op.create_unique_constraint("uq_schema_versions_schema_version", "schema_versions", ["schema_id", "version"])
    except Exception:
        conn.execute(text("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_schema_versions_schema_version ON schema_versions(schema_id, version)"))
        conn.execute(text("ALTER TABLE schema_versions ADD CONSTRAINT uq_schema_versions_schema_version UNIQUE USING INDEX ix_schema_versions_schema_version"))

    # Step 3: Create snapshot_schema_pins table (from 0022, fixed FK)
    op.create_table(
        "snapshot_schema_pins",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.String(length=50), nullable=False),
        sa.CheckConstraint("direction IN ('input', 'output')", name="ck_ssp_direction"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["pipeline_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["schema_id"], ["schemas.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["schema_id", "schema_version"], ["schema_versions.schema_id", "schema_versions.version"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_ssp_snapshot", "snapshot_schema_pins", ["snapshot_id"])
    op.create_index("idx_ssp_schema", "snapshot_schema_pins", ["schema_id", "schema_version"])

    # Step 4: Enable RLS (from 0023)
    op.execute(text("ALTER TABLE snapshot_schema_pins ENABLE ROW LEVEL SECURITY"))

    # Step 5: Backfill data from old schema_pins_json (from 0024, fixed empty version)
    rows = conn.execute(text("""
        SELECT ps.id, ps.organisation_id, ps.graph_json, ps.schema_pins_json
        FROM pipeline_snapshots ps ORDER BY ps.id
    """)).fetchall()

    for row in rows:
        sid = row[0]
        org_id = row[1]
        graph_json = row[2] or {}
        pins_json = row[3] or []
        nodes = graph_json.get("nodes") or []
        {str(n.get("agent_id", "")) for n in nodes if n.get("agent_id")}
        pin_schema_ids = {str(p.get("schema_id", "")) for p in pins_json}

        insert_sql = text("""
            INSERT INTO snapshot_schema_pins (id, organisation_id, snapshot_id, node_id, direction, schema_id, schema_version)
            VALUES (gen_random_uuid(), :org_id, :snap_id, :node_id, :dir, :schema_id, :schema_ver)
            ON CONFLICT DO NOTHING
        """)

        for n in nodes:
            agent_id = str(n.get("agent_id", ""))
            if not agent_id or agent_id not in pin_schema_ids:
                continue
            for pin in pins_json:
                schema_id = str(pin.get("schema_id", ""))
                version = str(pin.get("schema_version", "") or "")
                if not schema_id or not version:
                    continue
                direction = pin.get("direction", "input")
                with contextlib.suppress(Exception):
                    conn.execute(insert_sql, {
                        "org_id": org_id, "snap_id": sid,
                        "node_id": agent_id, "dir": direction,
                        "schema_id": schema_id, "schema_ver": version
                    })

    # Step 6: Add RLS policies (from 0023)
    conn.execute(text("""
        CREATE POLICY ssp_org_isolation ON snapshot_schema_pins
        FOR ALL USING (organisation_id = current_setting('app.organisation_id')::uuid)
    """))

def downgrade() -> None:
    op.execute(text("DROP POLICY IF EXISTS ssp_org_isolation ON snapshot_schema_pins"))
    op.drop_index("idx_ssp_schema", table_name="snapshot_schema_pins")
    op.drop_index("idx_ssp_snapshot", table_name="snapshot_schema_pins")
    op.drop_table("snapshot_schema_pins")
    op.drop_constraint("uq_schema_versions_schema_version", "schema_versions", type_="unique")
    op.drop_column("schemas", "system")
