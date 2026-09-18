"""Drop scalar agent_command column, migrate data into agent_commands array (FAR-828).

Option B: a single command is just a one-item agent_commands list. This
migration migrates existing scalar ``agent_command`` values into the
``agent_commands`` JSONB array and then drops the column from ``agents``.

Graph JSON node objects in ``pipelines.graph_nodes_json`` (JSON array of
node dicts — this column stays plain ``json`` after the 0147 standardization),
``pipeline_snapshots.graph_json`` (JSONB object with a
``nodes`` array), and ``composite_templates.sub_pipeline_graph_json``
(JSONB object with a ``nodes`` array) are also updated: each node that
has a non-blank scalar ``agent_command`` and no (or empty) ``agent_commands``
gets ``agent_commands`` set to ``[agent_command]``; the ``agent_command``
key is always removed when present.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0228_drop_scalar_agent_command"
down_revision: str | None = "0227_env_profiles_initialisation_strategy_check"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --- 1. agents table: migrate scalar into array, then drop column ---
    op.execute(
        """
        UPDATE public.agents
        SET agent_commands = to_jsonb(ARRAY[trim(both from agent_command)])
        WHERE agent_command IS NOT NULL
          AND trim(both from agent_command) <> ''
          AND (agent_commands IS NULL OR jsonb_typeof(agent_commands) <> 'array' OR agent_commands = '[]'::jsonb OR jsonb_array_length(agent_commands) = 0)
        """
    )
    op.drop_column("agents", "agent_command")

    # --- 2. pipelines.graph_nodes_json (JSON array of node objects) ---
    # NOTE: ``graph_nodes_json`` is still typed plain ``json`` in Postgres (it is
    # deliberately *not* in the 0147 json->jsonb standardization list), so the
    # jsonb helpers below must read it via ``::jsonb`` and the result is cast
    # back to ``json`` on write.
    op.execute(
        """
        UPDATE public.pipelines
        SET graph_nodes_json = (
            SELECT jsonb_agg(
                CASE
                    WHEN elem->>'agent_command' IS NOT NULL
                         AND trim(both from elem->>'agent_command') <> ''
                         AND (elem->'agent_commands' IS NULL
                              OR elem->'agent_commands' = '[]'::jsonb
                              OR jsonb_typeof(elem->'agent_commands') <> 'array'
                                 OR jsonb_array_length(elem->'agent_commands') = 0)
                    THEN elem - 'agent_command' || jsonb_build_object(
                        'agent_commands',
                        to_jsonb(ARRAY[trim(both from elem->>'agent_command')])
                    )
                    WHEN elem->>'agent_command' IS NOT NULL
                    THEN elem - 'agent_command'
                    ELSE elem
                END
            )::json
            FROM jsonb_array_elements(graph_nodes_json::jsonb) AS elem
        )
        WHERE graph_nodes_json IS NOT NULL
          AND jsonb_typeof(graph_nodes_json::jsonb) = 'array' AND jsonb_array_length(graph_nodes_json::jsonb) > 0
          AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(graph_nodes_json::jsonb) AS e
              WHERE e ? 'agent_command'
          )
        """
    )

    # --- 3. pipeline_snapshots.graph_json (JSONB object with 'nodes' array) ---
    op.execute(
        """
        UPDATE public.pipeline_snapshots
        SET graph_json = jsonb_set(
            graph_json,
            '{nodes}',
            (
                SELECT jsonb_agg(
                    CASE
                        WHEN elem->>'agent_command' IS NOT NULL
                             AND trim(both from elem->>'agent_command') <> ''
                             AND (elem->'agent_commands' IS NULL
                                  OR elem->'agent_commands' = '[]'::jsonb
                                  OR jsonb_typeof(elem->'agent_commands') <> 'array'
                                 OR jsonb_array_length(elem->'agent_commands') = 0)
                        THEN elem - 'agent_command' || jsonb_build_object(
                            'agent_commands',
                            to_jsonb(ARRAY[trim(both from elem->>'agent_command')])
                        )
                        WHEN elem->>'agent_command' IS NOT NULL
                        THEN elem - 'agent_command'
                        ELSE elem
                    END
                )
                FROM jsonb_array_elements(graph_json -> 'nodes') AS elem
            )
        )
        WHERE graph_json IS NOT NULL
          AND jsonb_typeof(graph_json -> 'nodes') = 'array'
          AND graph_json -> 'nodes' IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(graph_json -> 'nodes') AS e
              WHERE e ? 'agent_command'
          )
        """
    )

    # --- 4. composite_templates.sub_pipeline_graph_json (JSONB object with 'nodes' array) ---
    op.execute(
        """
        UPDATE public.composite_templates
        SET sub_pipeline_graph_json = jsonb_set(
            sub_pipeline_graph_json,
            '{nodes}',
            (
                SELECT jsonb_agg(
                    CASE
                        WHEN elem->>'agent_command' IS NOT NULL
                             AND trim(both from elem->>'agent_command') <> ''
                             AND (elem->'agent_commands' IS NULL
                                  OR elem->'agent_commands' = '[]'::jsonb
                                  OR jsonb_typeof(elem->'agent_commands') <> 'array'
                                 OR jsonb_array_length(elem->'agent_commands') = 0)
                        THEN elem - 'agent_command' || jsonb_build_object(
                            'agent_commands',
                            to_jsonb(ARRAY[trim(both from elem->>'agent_command')])
                        )
                        WHEN elem->>'agent_command' IS NOT NULL
                        THEN elem - 'agent_command'
                        ELSE elem
                    END
                )
                FROM jsonb_array_elements(sub_pipeline_graph_json -> 'nodes') AS elem
            )
        )
        WHERE sub_pipeline_graph_json IS NOT NULL
          AND jsonb_typeof(sub_pipeline_graph_json -> 'nodes') = 'array'
          AND sub_pipeline_graph_json -> 'nodes' IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(sub_pipeline_graph_json -> 'nodes') AS e
              WHERE e ? 'agent_command'
          )
        """
    )


def downgrade() -> None:
    # Best-effort: re-add the column and populate from agent_commands[0].
    try:
        op.add_column(
            "agents",
            sa.Column("agent_command", sa.String(500), nullable=True),
        )
        op.execute(
            """
            UPDATE public.agents
            SET agent_command = agent_commands ->> 0
            WHERE agent_commands IS NOT NULL
              AND jsonb_array_length(agent_commands) > 0
            """
        )
    except Exception:  # noqa: S110 — best-effort downgrade, non-critical
        pass
