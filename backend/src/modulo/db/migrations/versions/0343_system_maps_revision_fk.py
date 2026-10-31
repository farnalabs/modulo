"""Make the System Map revision parent FK tenant-safe (FAR-2236).

Revision ID: 0343_system_maps_revision_fk
Revises: 0342_system_maps_integrity
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0343_system_maps_revision_fk"
down_revision: str | None = "0342_system_maps_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "system_map_revisions"
_PLAIN_FK = "system_map_revisions_system_map_id_fkey"
_COMPOSITE_FK = "fk_system_map_revisions_map_org"
_RESERVATIONS_TABLE = "system_map_loop_reservations"
_RESERVATION_RUN_FK = "system_map_loop_reservations_run_id_fkey"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    """Replace the unscoped revision parent FK with an organisation-scoped FK."""
    if not _is_postgres():
        return
    op.execute(sa.text(f'ALTER TABLE "{_TABLE}" DROP CONSTRAINT IF EXISTS "{_PLAIN_FK}"'))
    op.execute(
        sa.text(
            f'ALTER TABLE "{_TABLE}" ADD CONSTRAINT "{_COMPOSITE_FK}" '
            "FOREIGN KEY (system_map_id, organisation_id) "
            "REFERENCES system_maps (id, organisation_id) ON DELETE CASCADE"
        )
    )
    op.execute(
        sa.text(
            f'ALTER TABLE "{_RESERVATIONS_TABLE}" ADD CONSTRAINT "{_RESERVATION_RUN_FK}" '
            "FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE RESTRICT"
        )
    )


def downgrade() -> None:
    """Restore the historical unscoped revision parent FK."""
    if not _is_postgres():
        return
    op.execute(sa.text(f'ALTER TABLE "{_RESERVATIONS_TABLE}" DROP CONSTRAINT IF EXISTS "{_RESERVATION_RUN_FK}"'))
    op.execute(sa.text(f'ALTER TABLE "{_TABLE}" DROP CONSTRAINT IF EXISTS "{_COMPOSITE_FK}"'))
    op.execute(
        sa.text(
            f'ALTER TABLE "{_TABLE}" ADD CONSTRAINT "{_PLAIN_FK}" '
            "FOREIGN KEY (system_map_id) REFERENCES system_maps (id) ON DELETE CASCADE"
        )
    )
