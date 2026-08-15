"""Add the ``updated_by`` actor column to lifecycle_maps (version actor stamping).

Revision ID: 0103_lifecycle_map_version_actor
Revises: 0102_ongoing_streak_epoch
Create Date: 2026-08-15

The map row carries ``account_id`` (the ORIGINAL creator, set at create) but no
column tracking WHO LAST SAVED a version, so the version-entry ``created_by``
surfaced by the version API always came back null. v1 keeps no immutable version
history — the map row IS the active version — so the version entry's
``created_by`` should reflect the account that last produced the current version
state. This migration adds ``updated_by``: a plain nullable UUID reference to
the account that last saved a version (no FK constraint, consistent with how
audit payloads reference actors loosely).

The column uses ``sa.Uuid()`` (the codebase's cross-backend pattern for UUID
columns) so the migration renders on Postgres, SQLite, and MariaDB. Additive +
nullable + never backfilled — safe to downgrade by dropping the column.

Renumbered from ``0102_lifecycle_map_version_actor`` to clear the numeric-prefix
collision with FAR-190's ``0102_ongoing_streak_epoch`` (merged to main while this
PR was open); this migration now sits on top of it as the chain head.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0103_lifecycle_map_version_actor"
down_revision: str | None = "0102_ongoing_streak_epoch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("lifecycle_maps", sa.Column("updated_by", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("lifecycle_maps", "updated_by")
