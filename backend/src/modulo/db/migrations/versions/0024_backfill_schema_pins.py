"""Backfill per-node schema pins from old schema_pins_json format.

Revision ID: 0024_backfill_schema_pins
Revises: 0023_rls_snapshot_schema_pins
Create Date: 2026-07-22
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0024_backfill_schema_pins"
down_revision: str | None = "0023_rls_snapshot_schema_pins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    rows = conn.execute(
        text("""
            SELECT ps.id AS snapshot_id, ps.organisation_id, ps.graph_json, ps.schema_pins_json
            FROM pipeline_snapshots ps
            ORDER BY ps.id
        """)
    ).fetchall()

    for row in rows:
        snapshot_id = row.snapshot_id
        org_id = row.organisation_id
        graph_json = row.graph_json or {}
        schema_pins_json = row.schema_pins_json or []

        nodes = graph_json.get("nodes") or []

        new_pins: list[dict] = []

        node_agent_ids: set[str] = set()
        for node in nodes:
            agent_id = node.get("agent_id")
            if agent_id:
                node_agent_ids.add(str(agent_id))

        old_pin_schema_ids: set[str] = set()
        for pin in schema_pins_json:
            sid = pin.get("schema_id")
            if sid:
                old_pin_schema_ids.add(str(sid))

        unmatched_schema_ids = old_pin_schema_ids.copy()

        for node in nodes:
            node_id = node.get("id")
            node_type = node.get("node_type", "agent")
            agent_id = node.get("agent_id")

            output_schema_id = node.get("output_schema_id")
            output_schema_version = node.get("output_schema_version", "")

            if output_schema_id:
                new_pins.append(
                    {
                        "snapshot_id": str(snapshot_id),
                        "organisation_id": str(org_id),
                        "node_id": str(node_id),
                        "direction": "output",
                        "schema_id": str(output_schema_id),
                        "schema_version": str(output_schema_version),
                    }
                )
                unmatched_schema_ids.discard(str(output_schema_id))

            if node_type == "agent" and agent_id:
                agent_row = conn.execute(
                    text("""
                        SELECT input_schema_id, input_schema_version
                        FROM agents WHERE id = :aid
                    """),
                    {"aid": agent_id},
                ).fetchone()

                if agent_row and agent_row.input_schema_id:
                    input_sid = str(agent_row.input_schema_id)
                    input_sver = agent_row.input_schema_version or ""
                    new_pins.append(
                        {
                            "snapshot_id": str(snapshot_id),
                            "organisation_id": str(org_id),
                            "node_id": str(node_id),
                            "direction": "input",
                            "schema_id": input_sid,
                            "schema_version": input_sver,
                        }
                    )
                    unmatched_schema_ids.discard(input_sid)

        for unmatched_sid in unmatched_schema_ids:
            first_agent_node = next((n for n in nodes if n.get("node_type") == "agent"), None)
            if first_agent_node is not None:
                new_pins.append(
                    {
                        "snapshot_id": str(snapshot_id),
                        "organisation_id": str(org_id),
                        "node_id": str(first_agent_node["id"]),
                        "direction": "output",
                        "schema_id": unmatched_sid,
                        "schema_version": "",
                    }
                )

        if new_pins:
            conn.execute(
                text("""
                    DELETE FROM snapshot_schema_pins
                    WHERE snapshot_id = :sid
                """),
                {"sid": snapshot_id},
            )

            for pin in new_pins:
                conn.execute(
                    text("""
                        INSERT INTO snapshot_schema_pins
                            (id, organisation_id, snapshot_id, node_id, direction, schema_id, schema_version)
                        VALUES (gen_random_uuid(), :org_id, :snap_id, :node_id, :dir, :schema_id, :schema_ver)
                    """),
                    {
                        "org_id": pin["organisation_id"],
                        "snap_id": pin["snapshot_id"],
                        "node_id": pin["node_id"],
                        "dir": pin["direction"],
                        "schema_id": pin["schema_id"],
                        "schema_ver": pin["schema_version"],
                    },
                )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DELETE FROM snapshot_schema_pins"))
