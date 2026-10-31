"""FAR-2236: close System Maps graph and tenant-integrity gaps.

Revision ID: 0342_system_maps_integrity
Revises: 0341_system_maps
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0342_system_maps_integrity"
down_revision: str | None = "0341_system_maps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMPOSITE_FKS = (
    ("fk_system_map_nodes_revision", "system_map_nodes", "revision_id", "system_map_revisions"),
    ("fk_system_map_relationships_revision", "system_map_relationships", "revision_id", "system_map_revisions"),
    ("fk_system_map_loop_configs_revision", "system_map_loop_configs", "revision_id", "system_map_revisions"),
    ("fk_system_map_attribution_bindings_map", "system_map_attribution_bindings", "system_map_id", "system_maps"),
    (
        "fk_system_map_loop_admission_attempts_config",
        "system_map_loop_admission_attempts",
        "loop_config_id",
        "system_map_loop_configs",
    ),
    (
        "fk_system_map_loop_reservations_config",
        "system_map_loop_reservations",
        "loop_config_id",
        "system_map_loop_configs",
    ),
)

_UNIQUE_TARGETS = (
    ("uq_system_map_nodes_id_organisation_id", "system_map_nodes"),
    ("uq_system_map_relationships_id_organisation_id", "system_map_relationships"),
    ("uq_system_map_loop_configs_id_organisation_id", "system_map_loop_configs"),
    ("uq_system_map_attribution_bindings_id_organisation_id", "system_map_attribution_bindings"),
    ("uq_system_map_loop_admission_attempts_id_organisation_id", "system_map_loop_admission_attempts"),
    ("uq_system_map_loop_reservations_id_organisation_id", "system_map_loop_reservations"),
)

_TENANT_TRIGGER_DDL = (
    "CREATE TRIGGER trg_system_map_nodes_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON system_map_nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id')",
    "CREATE TRIGGER trg_system_map_nodes_connector_instance_id_tenant BEFORE INSERT OR UPDATE OF connector_instance_id, organisation_id ON system_map_nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('connector_instances', 'connector_instance_id')",
    "CREATE TRIGGER trg_system_map_nodes_owner_team_id_tenant BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id ON system_map_nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('teams', 'owner_team_id')",
    "CREATE TRIGGER trg_system_map_nodes_eval_id_tenant BEFORE INSERT OR UPDATE OF eval_id, organisation_id ON system_map_nodes FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('evals', 'eval_id')",
    "CREATE TRIGGER trg_system_map_loop_configs_action_pipeline_id_tenant BEFORE INSERT OR UPDATE OF action_pipeline_id, organisation_id ON system_map_loop_configs FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'action_pipeline_id')",
    "CREATE TRIGGER trg_system_map_attribution_bindings_pipeline_id_tenant BEFORE INSERT OR UPDATE OF pipeline_id, organisation_id ON system_map_attribution_bindings FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('pipelines', 'pipeline_id')",
    "CREATE TRIGGER trg_system_map_attribution_bindings_trigger_id_tenant BEFORE INSERT OR UPDATE OF trigger_id, organisation_id ON system_map_attribution_bindings FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('triggers', 'trigger_id')",
)

_TRIGGER_TABLES = (
    ("trg_system_map_nodes_pipeline_id_tenant", "system_map_nodes"),
    ("trg_system_map_nodes_connector_instance_id_tenant", "system_map_nodes"),
    ("trg_system_map_nodes_owner_team_id_tenant", "system_map_nodes"),
    ("trg_system_map_nodes_eval_id_tenant", "system_map_nodes"),
    ("trg_system_map_loop_configs_action_pipeline_id_tenant", "system_map_loop_configs"),
    ("trg_system_map_attribution_bindings_pipeline_id_tenant", "system_map_attribution_bindings"),
    ("trg_system_map_attribution_bindings_trigger_id_tenant", "system_map_attribution_bindings"),
)


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _drop_plain_fk(table: str, column: str) -> None:
    op.execute(sa.text(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{table}_{column}_fkey"'))


def upgrade() -> None:
    if not _is_postgres():
        return
    for name, table in _UNIQUE_TARGETS:
        op.execute(sa.text(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" UNIQUE (id, organisation_id)'))
    for name, table, column, target in _COMPOSITE_FKS:
        _drop_plain_fk(table, column)
        op.execute(
            sa.text(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" FOREIGN KEY ("{column}", organisation_id) '
                f'REFERENCES "{target}" (id, organisation_id) ON DELETE CASCADE'
            )
        )
    op.execute(sa.text("DROP INDEX IF EXISTS uq_attr_binding_trigger_per_pipeline_node"))
    op.execute(sa.text("DROP INDEX IF EXISTS uq_attr_binding_action_per_pipeline_node"))
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_attr_binding_trigger_per_pipeline_node ON system_map_attribution_bindings (organisation_id, pipeline_id, node_key) WHERE binding_kind = 'trigger' AND deleted_at IS NULL"
        )
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_attr_binding_action_per_pipeline_node ON system_map_attribution_bindings (organisation_id, pipeline_id, node_key) WHERE binding_kind = 'action_pipeline' AND deleted_at IS NULL"
        )
    )
    op.create_check_constraint(
        "ck_system_map_attribution_bindings_trigger_semantics",
        "system_map_attribution_bindings",
        "(binding_kind = 'trigger' AND trigger_id IS NOT NULL) OR (binding_kind = 'action_pipeline' AND trigger_id IS NULL)",
    )
    for ddl in _TENANT_TRIGGER_DDL:
        op.execute(sa.text(ddl))


def downgrade() -> None:
    if not _is_postgres():
        return
    for trigger, table in reversed(_TRIGGER_TABLES):
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS "{trigger}" ON "{table}"'))
    op.drop_constraint("ck_system_map_attribution_bindings_trigger_semantics", "system_map_attribution_bindings")
    op.execute(sa.text("DROP INDEX IF EXISTS uq_attr_binding_trigger_per_pipeline_node"))
    op.execute(sa.text("DROP INDEX IF EXISTS uq_attr_binding_action_per_pipeline_node"))
    for name, table, column, target in reversed(_COMPOSITE_FKS):
        op.execute(sa.text(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"'))
        op.execute(
            sa.text(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{table}_{column}_fkey" '
                f'FOREIGN KEY ("{column}") REFERENCES "{target}" (id)'
            )
        )
    for name, table in reversed(_UNIQUE_TARGETS):
        op.drop_constraint(name, table, type_="unique")
