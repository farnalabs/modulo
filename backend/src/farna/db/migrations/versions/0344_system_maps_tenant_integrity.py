"""Enforce System Map owner-team tenant integrity (FAR-2236).

Revision ID: 0344_system_maps_tenant_integrity
Revises: 0343_system_maps_revision_fk
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0344_system_maps_tenant_integrity"
down_revision: str | None = "0343_system_maps_revision_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGGER = "trg_system_maps_owner_team_tenant"
_FUNCTION = "enforce_system_map_owner_team"
_PUBLISHED_INDEX = "uq_system_map_revisions_one_published"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    """Reject foreign/deleted owner teams and team maps without an owner."""
    if not _is_postgres():
        return

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION public.enforce_system_map_owner_team()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.visibility = 'team' AND NEW.owner_team_id IS NULL THEN
                    RAISE EXCEPTION 'team-visible system maps require an owner team'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.owner_team_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1
                    FROM public.teams
                    WHERE teams.id = NEW.owner_team_id
                      AND teams.organisation_id = NEW.organisation_id
                      AND teams.deleted_at IS NULL
                ) THEN
                    RAISE EXCEPTION 'system map owner team must belong to the same organisation'
                        USING ERRCODE = '23503';
                END IF;

                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_system_map_revisions_one_published "
            "ON public.system_map_revisions (system_map_id) WHERE status = 'published'"
        )
    )
    op.execute(sa.text(f'DROP TRIGGER IF EXISTS "{_TRIGGER}" ON public.system_maps'))
    op.execute(
        sa.text(
            f'CREATE TRIGGER "{_TRIGGER}" '
            "BEFORE INSERT OR UPDATE OF owner_team_id, organisation_id, visibility "
            "ON public.system_maps FOR EACH ROW "
            f"EXECUTE FUNCTION public.{_FUNCTION}()"
        )
    )


def downgrade() -> None:
    """Remove the owner-team integrity trigger and function."""
    if not _is_postgres():
        return
    op.execute(sa.text(f'DROP TRIGGER IF EXISTS "{_TRIGGER}" ON public.system_maps'))
    op.execute(sa.text("DROP FUNCTION IF EXISTS public.enforce_system_map_owner_team()"))
    op.execute(sa.text("DROP INDEX IF EXISTS uq_system_map_revisions_one_published"))
