"""Merge all divergent migration heads into a single chain.

Revision ID: 0036_merge_all_heads
Revises: 0035_reconcile_schema_drift, 0023_add_error_forwarder_configs_deleted_at, 0023_add_triggers_deleted_at, 0023_add_variant_groups_deleted_at, 0024_add_library_primitives_deleted_at, 0030_add_error_events_window_index, 0030_fix_node_timeout_non_null
Create Date: 2026-08-03

Multiple parallel branches accumulated heads:

- The main chain ``0035_reconcile_schema_drift`` (0029_stale_run -> 0031_saq
  -> 0033 -> 0034 -> 0035).
- Four soft-delete ``deleted_at`` branches (0023_add_error_forwarder_configs_deleted_at,
  0023_add_triggers_deleted_at, 0023_add_variant_groups_deleted_at,
  0024_add_library_primitives_deleted_at). All four add a nullable ``deleted_at``
  column that the ORM's ``SoftDeleteMixin`` models require.
- Two 0030 fixes (0030_add_error_events_window_index, 0030_fix_node_timeout_non_null)
  that branch off ``0029_fix_expiry_fields_non_null``.

This is a pure graph merge: every branch's schema change is already additive and
independent, so ``upgrade()`` is a no-op. Joining them restores a single head so
``alembic upgrade head`` works again (previously it raised "Multiple head
revisions are present", blocking app startup and the deploy gate).
"""

from __future__ import annotations

import sqlalchemy as sa

revision = "0036_merge_all_heads"
down_revision: str | sa.Sequence[str] | None = (
    "0035_reconcile_schema_drift",
    "0023_add_error_forwarder_configs_deleted_at",
    "0023_add_triggers_deleted_at",
    "0023_add_variant_groups_deleted_at",
    "0024_add_library_primitives_deleted_at",
    "0030_add_error_events_window_index",
    "0030_fix_node_timeout_non_null",
)
branch_labels: str | sa.Sequence[str] | None = None
depends_on: str | sa.Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
