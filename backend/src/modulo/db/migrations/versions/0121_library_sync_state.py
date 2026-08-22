"""Create the library_sync_state singleton table (FAR-363).

Revision ID: 0121_library_sync_state
Revises: 0120_org_fk_hardening
Create Date: 2026-08-22

Instance-global cache of the community-library sync (NOT org-scoped): a
single-row table holding the last-good signed manifest and the
revocation-filtered catalog, written by the SAQ ``library_sync`` cron. No RLS —
the table carries no ``organisation_id`` column and the app role is granted DML
directly.

The modulo_app DML grant is conditional on the role existing (fresh dev/BDD
databases where ``alembic upgrade heads`` runs before role bootstrap skip it),
matching the 0115 ceremony.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0121_library_sync_state"
down_revision: str | None = "0120_org_fk_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "modulo_app"


def upgrade() -> None:
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    if pg:
        op.execute("SET search_path TO public")

    op.create_table(
        "library_sync_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("catalog_json", sa.JSON(), nullable=False),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=2000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    if pg:
        app_role_exists = (
            bind.execute(
                sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": _APP_ROLE}
            ).scalar_one_or_none()
            is not None
        )
        if app_role_exists:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.library_sync_state TO {_APP_ROLE}")


def downgrade() -> None:
    op.drop_table("library_sync_state")
