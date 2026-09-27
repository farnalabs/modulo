"""Rename HITL gate vocabulary to review (FAR-1104 chunk 5b).

Renames all HITL uses of "gate" to "review" to free the word "gate" for the
Policy Gate entity.  Changes:

1. Column renames (PostgreSQL: RENAME COLUMN; SQLite: batch mode):
   - pipeline_edges.hitl_gate_config -> hitl_review_config
   - hitl_claims.gate_id -> review_id
   - feedback_records.gate_id -> review_id

2. Constraint rename:
   - hitl_claims.uq_hitl_claims_run_gate -> uq_hitl_claims_run_review

3. Graph-JSON data migration:
   - hitl_gate_config key in pipeline_edges JSON -> hitl_review_config
   - hitl_gate_<src>_<tgt> synthetic node IDs -> hitl_review_<src>_<tgt>
   - gate_id key inside config dicts -> review_id

Revision ID: 0262_hitl_gate_to_review_vocabulary
Revises: 0261_decision_record_payload
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0262_hitl_gate_to_review_vocabulary"
down_revision = "0261_decision_record_payload"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    # --- Phase 1: Column renames ---
    if _is_postgres():
        op.execute("ALTER TABLE pipeline_edges RENAME COLUMN hitl_gate_config TO hitl_review_config")
        op.execute("ALTER TABLE hitl_claims RENAME COLUMN gate_id TO review_id")
        op.execute("ALTER TABLE feedback_records RENAME COLUMN gate_id TO review_id")
    elif _is_sqlite():
        # SQLite batch mode for column renames
        with op.batch_alter_table("pipeline_edges") as batch_op:
            batch_op.alter_column("hitl_gate_config", new_column_name="hitl_review_config")
        with op.batch_alter_table("hitl_claims") as batch_op:
            batch_op.alter_column("gate_id", new_column_name="review_id")
        with op.batch_alter_table("feedback_records") as batch_op:
            batch_op.alter_column("gate_id", new_column_name="review_id")

    # --- Phase 2: Constraint rename (after column rename) ---
    if _is_postgres():
        op.execute("ALTER TABLE hitl_claims DROP CONSTRAINT uq_hitl_claims_run_gate")
        op.execute("ALTER TABLE hitl_claims ADD CONSTRAINT uq_hitl_claims_run_review UNIQUE (run_id, review_id)")
    elif _is_sqlite():
        # SQLite: drop and recreate unique constraint via batch mode
        with op.batch_alter_table("hitl_claims") as batch_op:
            batch_op.drop_constraint("uq_hitl_claims_run_gate", type_="unique")
            batch_op.create_unique_constraint("uq_hitl_claims_run_review", ["run_id", "review_id"])

    # --- Phase 3: Graph-JSON data migration (batched ctid loop for PG, straight UPDATE for SQLite) ---
    _migrate_graph_json()


def _migrate_graph_json() -> None:
    """Rewrite hitl_gate_* keys and prefixes in committed graph JSON.

    Covers three storage locations:
    1. pipeline_edges.hitl_review_config — the column stores the config dict
       directly (keys: human_only, description, label, gate_id, etc.). There
       is NO top-level ``hitl_gate_config`` key in this dict — only the
       ``gate_id`` key needs renaming.
    2. pipelines.graph_nodes_json — synthetic node IDs prefixed
       ``hitl_gate_<src>_<tgt>`` become ``hitl_review_<src>_<tgt>``.
    3. pipeline_snapshots.graph_json.edges — each edge may carry a
       ``hitl_gate_config`` key (the whole config dict) whose inner ``gate_id``
       key also needs renaming, plus the same synthetic node ID prefix rename
       in the edges' source/target-derived IDs.
    """
    bind = op.get_bind()

    if _is_postgres():
        # --- pipeline_edges: rename gate_id key inside config dict ---
        # The column stores the config dict directly; only gate_id needs
        # renaming (no top-level hitl_gate_config key exists here).
        while True:
            result = bind.execute(
                sa.text(
                    """
                    UPDATE pipeline_edges
                    SET hitl_review_config = (
                        SELECT jsonb_object_agg(
                            CASE WHEN key = 'gate_id' THEN 'review_id'
                                 ELSE key END,
                            value
                        )
                        FROM jsonb_each(hitl_review_config) AS kv(key, value)
                    )
                    WHERE ctid IN (
                        SELECT ctid FROM pipeline_edges
                        WHERE hitl_review_config IS NOT NULL
                        AND hitl_review_config ? 'gate_id'
                        LIMIT 1000
                    )
                    """
                )
            )
            if result.rowcount == 0:
                break

        # --- pipelines.graph_nodes_json: synthetic node ID prefix ---
        while True:
            result = bind.execute(
                sa.text(
                    """
                    UPDATE pipelines
                    SET graph_nodes_json = (
                        SELECT jsonb_agg(
                            CASE WHEN value::text LIKE '"hitl_gate_%"'
                                 THEN to_jsonb(replace(value::text, '"hitl_gate_', '"hitl_review_')::jsonb)
                                 ELSE value END
                        )
                        FROM jsonb_array_elements(graph_nodes_json) AS value
                    )
                    WHERE ctid IN (
                        SELECT ctid FROM pipelines
                        WHERE graph_nodes_json IS NOT NULL
                        AND graph_nodes_json::text LIKE '%hitl_gate_%'
                        LIMIT 1000
                    )
                    """
                )
            )
            if result.rowcount == 0:
                break

        # --- pipeline_snapshots.graph_json: edge-level hitl_gate_config key
        #     + gate_id inside it + synthetic node ID prefix ---
        while True:
            result = bind.execute(
                sa.text(
                    """
                    UPDATE pipeline_snapshots
                    SET graph_json = (
                        SELECT jsonb_set(
                            jsonb_set(
                                jsonb_set(
                                    graph_json,
                                    '{edges}',
                                    (
                                        SELECT jsonb_agg(
                                            CASE WHEN edge ? 'hitl_gate_config' THEN
                                                jsonb_set(
                                                    jsonb_set(
                                                        edge,
                                                        '{hitl_review_config}',
                                                        (edge->'hitl_gate_config') - 'gate_id' || jsonb_build_object('review_id', edge->'hitl_gate_config'->'gate_id')
                                                    ),
                                                    '{hitl_gate_config}',
                                                    to_jsonb(null)
                                                ) - 'hitl_gate_config'
                                            ELSE edge END
                                        )
                                        FROM jsonb_array_elements(graph_json->'edges') AS edge
                                    )
                                ),
                                '{nodes}',
                                (
                                    SELECT jsonb_agg(
                                        CASE WHEN value::text LIKE '"hitl_gate_%"'
                                             THEN to_jsonb(replace(value::text, '"hitl_gate_', '"hitl_review_')::jsonb)
                                             ELSE value END
                                    )
                                    FROM jsonb_array_elements(graph_json->'nodes') AS value
                                )
                            )
                        )
                    )
                    WHERE ctid IN (
                        SELECT ctid FROM pipeline_snapshots
                        WHERE graph_json IS NOT NULL
                        AND (
                            graph_json->'edges' IS NOT NULL
                            AND graph_json::text LIKE '%hitl_gate_%'
                        )
                        LIMIT 1000
                    )
                    """
                )
            )
            if result.rowcount == 0:
                break

    elif _is_sqlite():
        # SQLite: simpler approach — string REPLACE
        # pipeline_edges: only gate_id needs renaming (no top-level hitl_gate_config)
        bind.execute(
            sa.text(
                """
                UPDATE pipeline_edges
                SET hitl_review_config = REPLACE(hitl_review_config, '"gate_id"', '"review_id"')
                WHERE hitl_review_config IS NOT NULL
                AND hitl_review_config LIKE '%gate_id%'
                """
            )
        )
        # pipelines graph_nodes_json
        bind.execute(
            sa.text(
                """
                UPDATE pipelines
                SET graph_nodes_json = REPLACE(graph_nodes_json, 'hitl_gate_', 'hitl_review_')
                WHERE graph_nodes_json IS NOT NULL
                AND graph_nodes_json LIKE '%hitl_gate_%'
                """
            )
        )
        # pipeline_snapshots: edge key rename + node prefix
        bind.execute(
            sa.text(
                """
                UPDATE pipeline_snapshots
                SET graph_json = REPLACE(
                    REPLACE(
                        REPLACE(
                            graph_json,
                            '"hitl_gate_config"',
                            '"hitl_review_config"'
                        ),
                        '"gate_id"',
                        '"review_id"'
                    ),
                    'hitl_gate_',
                    'hitl_review_'
                )
                WHERE graph_json IS NOT NULL
                AND graph_json LIKE '%hitl_gate_%'
                """
            )
        )


def downgrade() -> None:
    # --- Reverse data migration first ---
    _reverse_graph_json()

    # --- Reverse column renames (BEFORE constraint reverse — constraint refs gate_id) ---
    if _is_postgres():
        op.execute("ALTER TABLE pipeline_edges RENAME COLUMN hitl_review_config TO hitl_gate_config")
        op.execute("ALTER TABLE hitl_claims RENAME COLUMN review_id TO gate_id")
        op.execute("ALTER TABLE feedback_records RENAME COLUMN review_id TO gate_id")
    elif _is_sqlite():
        with op.batch_alter_table("pipeline_edges") as batch_op:
            batch_op.alter_column("hitl_review_config", new_column_name="hitl_gate_config")
        with op.batch_alter_table("hitl_claims") as batch_op:
            batch_op.alter_column("review_id", new_column_name="gate_id")
        with op.batch_alter_table("feedback_records") as batch_op:
            batch_op.alter_column("review_id", new_column_name="gate_id")

    # --- Reverse constraint rename (now gate_id column exists) ---
    if _is_postgres():
        op.execute("ALTER TABLE hitl_claims DROP CONSTRAINT uq_hitl_claims_run_review")
        op.execute("ALTER TABLE hitl_claims ADD CONSTRAINT uq_hitl_claims_run_gate UNIQUE (run_id, gate_id)")
    elif _is_sqlite():
        with op.batch_alter_table("hitl_claims") as batch_op:
            batch_op.drop_constraint("uq_hitl_claims_run_review", type_="unique")
            batch_op.create_unique_constraint("uq_hitl_claims_run_gate", ["run_id", "gate_id"])


def _reverse_graph_json() -> None:
    """Reverse the graph-JSON key renames for rollback."""
    bind = op.get_bind()

    if _is_postgres():
        # --- pipeline_edges: reverse gate_id → gate_id (only gate_id was renamed) ---
        while True:
            result = bind.execute(
                sa.text(
                    """
                    UPDATE pipeline_edges
                    SET hitl_review_config = (
                        SELECT jsonb_object_agg(
                            CASE WHEN key = 'review_id' THEN 'gate_id'
                                 ELSE key END,
                            value
                        )
                        FROM jsonb_each(hitl_review_config) AS kv(key, value)
                    )
                    WHERE ctid IN (
                        SELECT ctid FROM pipeline_edges
                        WHERE hitl_review_config IS NOT NULL
                        AND hitl_review_config ? 'review_id'
                        LIMIT 1000
                    )
                    """
                )
            )
            if result.rowcount == 0:
                break

        # --- pipelines.graph_nodes_json: reverse synthetic node ID prefix ---
        while True:
            result = bind.execute(
                sa.text(
                    """
                    UPDATE pipelines
                    SET graph_nodes_json = (
                        SELECT jsonb_agg(
                            CASE WHEN value::text LIKE '"hitl_review_%"'
                                 THEN to_jsonb(replace(value::text, '"hitl_review_', '"hitl_gate_')::jsonb)
                                 ELSE value END
                        )
                        FROM jsonb_array_elements(graph_nodes_json) AS value
                    )
                    WHERE ctid IN (
                        SELECT ctid FROM pipelines
                        WHERE graph_nodes_json IS NOT NULL
                        AND graph_nodes_json::text LIKE '%hitl_review_%'
                        LIMIT 1000
                    )
                    """
                )
            )
            if result.rowcount == 0:
                break

        # --- pipeline_snapshots.graph_json: reverse edge key + node prefix ---
        while True:
            result = bind.execute(
                sa.text(
                    """
                    UPDATE pipeline_snapshots
                    SET graph_json = (
                        SELECT jsonb_set(
                            jsonb_set(
                                jsonb_set(
                                    graph_json,
                                    '{edges}',
                                    (
                                        SELECT jsonb_agg(
                                            CASE WHEN edge ? 'hitl_review_config' THEN
                                                jsonb_set(
                                                    jsonb_set(
                                                        edge,
                                                        '{hitl_gate_config}',
                                                        (edge->'hitl_review_config') - 'review_id' || jsonb_build_object('gate_id', edge->'hitl_review_config'->'review_id')
                                                    ),
                                                    '{hitl_review_config}',
                                                    to_jsonb(null)
                                                ) - 'hitl_review_config'
                                            ELSE edge END
                                        )
                                        FROM jsonb_array_elements(graph_json->'edges') AS edge
                                    )
                                ),
                                '{nodes}',
                                (
                                    SELECT jsonb_agg(
                                        CASE WHEN value::text LIKE '"hitl_review_%"'
                                             THEN to_jsonb(replace(value::text, '"hitl_review_', '"hitl_gate_')::jsonb)
                                             ELSE value END
                                    )
                                    FROM jsonb_array_elements(graph_json->'nodes') AS value
                                )
                            )
                        )
                    )
                    WHERE ctid IN (
                        SELECT ctid FROM pipeline_snapshots
                        WHERE graph_json IS NOT NULL
                        AND graph_json->'edges' IS NOT NULL
                        AND graph_json::text LIKE '%hitl_review_%'
                        LIMIT 1000
                    )
                    """
                )
            )
            if result.rowcount == 0:
                break

    elif _is_sqlite():
        # --- pipeline_edges: reverse only gate_id ---
        bind.execute(
            sa.text(
                """
                UPDATE pipeline_edges
                SET hitl_review_config = REPLACE(hitl_review_config, '"review_id"', '"gate_id"')
                WHERE hitl_review_config IS NOT NULL
                AND hitl_review_config LIKE '%review_id%'
                """
            )
        )
        bind.execute(
            sa.text(
                """
                UPDATE pipelines
                SET graph_nodes_json = REPLACE(graph_nodes_json, 'hitl_review_', 'hitl_gate_')
                WHERE graph_nodes_json IS NOT NULL
                AND graph_nodes_json LIKE '%hitl_review_%'
                """
            )
        )
        # pipeline_snapshots: reverse edge key + node prefix
        bind.execute(
            sa.text(
                """
                UPDATE pipeline_snapshots
                SET graph_json = REPLACE(
                    REPLACE(
                        REPLACE(
                            graph_json,
                            '"hitl_review_config"',
                            '"hitl_gate_config"'
                        ),
                        '"review_id"',
                        '"gate_id"'
                    ),
                    'hitl_review_',
                    'hitl_gate_'
                )
                WHERE graph_json IS NOT NULL
                AND graph_json LIKE '%hitl_review_%'
                """
            )
        )
