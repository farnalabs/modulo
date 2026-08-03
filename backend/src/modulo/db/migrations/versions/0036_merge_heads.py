"""Schema-less merge of the seven concurrent migration heads.

Revision ID: 0036_merge_heads
Revises: (seven heads)
Create Date: 2026-08-02

Alembic currently reports SEVEN heads (``alembic heads``):
  0023_add_error_forwarder_configs_deleted_at
  0023_add_triggers_deleted_at
  0023_add_variant_groups_deleted_at
  0024_add_library_primitives_deleted_at
  0030_add_error_events_window_index
  0030_fix_node_timeout_non_null
  0035_reconcile_schema_drift

This hand-written merge revision (no schema changes) collapses them into a
single head so 0036 can chain on deterministically. ``alembic heads`` MUST
report exactly one head after this migration lands (deploy gate).
"""

from collections.abc import Sequence

revision: str = "0036_merge_heads"
down_revision: str | Sequence[str] | None = (
    "0023_add_error_forwarder_configs_deleted_at",
    "0023_add_triggers_deleted_at",
    "0023_add_variant_groups_deleted_at",
    "0024_add_library_primitives_deleted_at",
    "0030_add_error_events_window_index",
    "0030_fix_node_timeout_non_null",
    "0035_reconcile_schema_drift",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
