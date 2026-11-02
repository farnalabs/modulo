"""System Maps version-aware read model (FAR-2288).

Revision ID: 0346_system_maps_version_aware
Revises: 0345_system_maps_name_integrity

Adds effective_from / effective_to to system_map_revisions so runs and
metrics can be interpreted against the map revision active at a point in
time.

- effective_from: when this revision became active (set on publish)
- effective_to: when this revision was superseded (NULL = current)

Backfills existing published AND archived revisions: the earliest revision
gets effective_from = its published_at (or created_at), and each subsequent
revision gets effective_from = its published_at and the previous one's
effective_to = this one's effective_from. The latest published revision has
effective_to = NULL. Archived revisions must be backfilled too because a
re-publish archives the superseded revision, and a by-time read has to find
it inside its historical window.

The downgrade restores the ``rls_org_isolation`` policy installed by 0341:
the table persists across this migration, so RLS must stay enabled.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0346_system_maps_version_aware"
down_revision: str | None = "0345_system_maps_name_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_REVISIONS = "system_map_revisions"
_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

# Verbatim org-only policy installed by 0341_system_maps for
# system_map_revisions. Restored (not dropped) by the downgrade because the
# table outlives this migration.
_ORG_POLICY = "((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid))"
_RESTORE_RLS_STATEMENTS: tuple[str, ...] = (
    'DROP POLICY IF EXISTS rls_org_isolation ON "' + _TABLE_REVISIONS + '"',
    (
        'CREATE POLICY rls_org_isolation ON "'
        + _TABLE_REVISIONS
        + '" USING ('
        + _ORG_POLICY
        + ") WITH CHECK ("
        + _ORG_POLICY
        + ")"
    ),
    'ALTER TABLE "' + _TABLE_REVISIONS + '" ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE "' + _TABLE_REVISIONS + '" FORCE ROW LEVEL SECURITY',
)


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _role_exists(bind: sa.Connection, role: str) -> bool:
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def _grant_dml(role: str) -> None:
    # role is a known constant from _APP_ROLE / _SYSTEM_ROLE, validated by _role_exists.
    statement = "GRANT SELECT, INSERT, UPDATE, DELETE ON " + _TABLE_REVISIONS + " TO " + role
    op.execute(sa.text(statement))


def _prepare_roles(bind: sa.Connection) -> tuple[bool, bool, bool]:
    migrate_role = _role_exists(bind, _MIGRATE_ROLE)
    app_role = _role_exists(bind, _APP_ROLE)
    system_role = _role_exists(bind, _SYSTEM_ROLE)
    if migrate_role:
        op.execute("GRANT CREATE ON SCHEMA public TO " + _MIGRATE_ROLE)
        op.execute("GRANT REFERENCES ON TABLE public.organisations TO " + _MIGRATE_ROLE)
        op.execute("GRANT REFERENCES ON TABLE public.accounts TO " + _MIGRATE_ROLE)
    return migrate_role, app_role, system_role


def _apply_rls(app_role: bool, system_role: bool) -> None:
    # The table already has RLS from 0341; we just need to grant DML to new roles
    if app_role:
        _grant_dml(_APP_ROLE)
    if system_role:
        _grant_dml(_SYSTEM_ROLE)


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    migrate_role = False
    app_role = False
    system_role = False
    if pg:
        op.execute("SET search_path TO public")
        migrate_role, app_role, system_role = _prepare_roles(bind)

    if migrate_role:
        op.execute("SET ROLE " + _MIGRATE_ROLE)

    # Add effective_from and effective_to columns
    op.add_column(
        _TABLE_REVISIONS,
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE_REVISIONS,
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
    )

    # Create indexes for time-range queries
    op.create_index(
        "ix_system_map_revisions_effective_from",
        _TABLE_REVISIONS,
        ["effective_from"],
    )
    op.create_index(
        "ix_system_map_revisions_effective_to",
        _TABLE_REVISIONS,
        ["effective_to"],
    )
    # Composite index for time-range lookups
    op.create_index(
        "ix_system_map_revisions_map_effective_range",
        _TABLE_REVISIONS,
        ["system_map_id", "effective_from", "effective_to"],
    )

    if migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE_REVISIONS)

    if pg:
        _apply_rls(app_role, system_role)

    # Backfill existing published revisions
    _backfill_effective_dates(bind, pg)


def _assert_owner_is_migrate(bind: sa.Connection, table: str) -> None:
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass(:tbl)").bindparams(
            tbl="public." + table
        )
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            table + " owner is " + repr(owner) + ", expected '" + _MIGRATE_ROLE + "' "
            "(the app role must NOT own it — owner bypasses RLS)"
        )


def _backfill_effective_dates(bind: sa.Connection, pg: bool) -> None:
    """Backfill effective_from/effective_to for existing published and archived revisions.

    Archived revisions must be included: publishing a new revision archives the
    previous one, so excluding archived rows would leave every superseded
    revision with NULL effective dates and make it unreachable by a by-time
    read.

    Strategy:
    1. For each system_map, get all published + archived revisions ordered by
       COALESCE(published_at, created_at)
    2. First revision: effective_from = published_at (or created_at),
       effective_to = next's effective_from
    3. Last published revision: effective_to = NULL
    """
    if pg:
        # Use CTE for clean backfill on Postgres
        bind.execute(
            sa.text("""
            WITH ranked AS (
                SELECT
                    id,
                    system_map_id,
                    published_at,
                    created_at,
                    ROW_NUMBER() OVER (PARTITION BY system_map_id ORDER BY COALESCE(published_at, created_at) ASC) as rn,
                    LEAD(COALESCE(published_at, created_at)) OVER (PARTITION BY system_map_id ORDER BY COALESCE(published_at, created_at) ASC) as next_effective_from
                FROM system_map_revisions
                WHERE status IN ('published', 'archived') AND deleted_at IS NULL
            )
            UPDATE system_map_revisions r
            SET
                effective_from = ranked.effective_from,
                effective_to = ranked.effective_to
            FROM (
                SELECT
                    id,
                    COALESCE(published_at, created_at) as effective_from,
                    next_effective_from as effective_to
                FROM ranked
            ) ranked
            WHERE r.id = ranked.id
        """)
        )
    else:
        # SQLite fallback: simpler approach
        result = bind.execute(
            sa.text("""
            SELECT id, system_map_id, published_at, created_at
            FROM system_map_revisions
            WHERE status IN ('published', 'archived') AND deleted_at IS NULL
            ORDER BY system_map_id, COALESCE(published_at, created_at)
        """)
        )
        rows = result.fetchall()

        # Group by system_map_id
        from collections import defaultdict

        grouped = defaultdict(list)
        for row in rows:
            grouped[row.system_map_id].append(row)

        for revs in grouped.values():
            for i, rev in enumerate(revs):
                effective_from = rev.published_at or rev.created_at
                effective_to = revs[i + 1].published_at or revs[i + 1].created_at if i + 1 < len(revs) else None

                bind.execute(
                    sa.text("UPDATE system_map_revisions SET effective_from = :ef, effective_to = :et WHERE id = :id"),
                    {"ef": effective_from, "et": effective_to, "id": rev.id},
                )


def downgrade() -> None:
    bind = op.get_bind()
    if _is_postgres(bind):
        op.execute("SET search_path TO public")
        # The table persists across this migration (only columns are dropped),
        # so restore 0341's org-isolation policy instead of dropping it and
        # disabling RLS.
        for statement in _RESTORE_RLS_STATEMENTS:
            op.execute(statement)

    # Drop indexes
    op.drop_index("ix_system_map_revisions_map_effective_range", table_name=_TABLE_REVISIONS)
    op.drop_index("ix_system_map_revisions_effective_to", table_name=_TABLE_REVISIONS)
    op.drop_index("ix_system_map_revisions_effective_from", table_name=_TABLE_REVISIONS)

    # Drop columns
    op.drop_column(_TABLE_REVISIONS, "effective_to")
    op.drop_column(_TABLE_REVISIONS, "effective_from")
