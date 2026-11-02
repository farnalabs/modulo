"""Guard live System Map names against concurrent duplicate drafts (FAR-2236).

Revision ID: 0345_system_maps_name_integrity
Revises: 0344_system_maps_tenant_integrity

The discovery draft path is check-then-insert: two concurrent
``POST /system-maps/discover`` calls with the same name could both pass the
SELECT and both insert, persisting duplicate live drafts silently. A partial
unique index on ``(organisation_id, name) WHERE deleted_at IS NULL`` makes the
database the arbiter, so the losing insert raises an IntegrityError that the
persistence adapter translates into the same 409 conflict the sequential path
returns. Soft-deleted maps keep their name out of the guard, so a retired map
can be recreated.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0345_system_maps_name_integrity"
down_revision: str | None = "0344_system_maps_tenant_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "system_maps"
_INDEX = "uq_system_maps_org_name_live"
_LIVE_PREDICATE = sa.text("deleted_at IS NULL")


def upgrade() -> None:
    """Create the partial unique index guarding live (org, name) pairs."""
    op.create_index(
        _INDEX,
        _TABLE,
        ["organisation_id", "name"],
        unique=True,
        postgresql_where=_LIVE_PREDICATE,
        sqlite_where=_LIVE_PREDICATE,
    )


def downgrade() -> None:
    """Drop the live (org, name) uniqueness guard."""
    op.drop_index(_INDEX, table_name=_TABLE)
