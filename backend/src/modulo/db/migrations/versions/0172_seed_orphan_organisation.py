"""Seed the orphan organisation row backing public error ingest.

Revision ID: 0172_seed_orphan_organisation
Revises: 0171_runs_list_performance_indexes
Create Date: 2026-09-01

The public error-ingest endpoint (``POST /api/v1/errors/ingest/public``) pins
its transaction to the nil-UUID organisation
(00000000-0000-0000-0000-000000000000) so the org-only RLS policies on
``error_events``/``error_groups`` pass WITH CHECK for unattributed frontend
errors. ``error_events.organisation_id`` carries a HARD foreign key to
``organisations.id``, so that nil-UUID org must exist as a real row — or every
public-ingest INSERT fails the FK while ``ingest_batch`` swallows the
per-event errors, yielding a false-success 201 with nothing persisted.

RLS precision: the ``organisations`` table has NO RLS — the orphan org ROW
itself is a visible sentinel row (filtered from admin org listings; see
``admin_list_orgs``). What keeps the partition invisible to tenant sessions is
the org-only RLS on ``error_events``/``error_groups``: rows written under the
nil-UUID org never match another org's RLS predicate. Nothing (users, teams,
pipelines) links to the sentinel.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0172_seed_orphan_organisation"
down_revision: str | None = "0171_runs_list_performance_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_ORPHAN_ORG_ID = "00000000-0000-0000-0000-000000000000"
_ORPHAN_ORG_SLUG = "orphan-unattributed-errors"

# NOT NULL columns without server defaults (name, slug, settings_json) are
# supplied explicitly; status / created_at / authz_enforce / triggers_paused /
# guardrails_kill_switch / org_cumulative_spend_cents take their server
# defaults. The conflict target is the PK (id) so the seed is idempotent on
# re-runs; the partial-unique slug index is handled by the explicit pre-check
# below (an ON CONFLICT clause cannot target a partial index, so a slug
# collision there would otherwise abort the migration with a raw
# IntegrityError — or, with no target, silently no-op past it).
_INSERT_ORPHAN_ORG = (
    "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
    "VALUES (:id, :name, :slug, '{}', '{}') "
    "ON CONFLICT (id) DO NOTHING"
)


def upgrade() -> None:
    bind = op.get_bind()

    # Pre-check the partial-unique slug index (uq_organisations_slug, WHERE
    # deleted_at IS NULL). A live org holding the slug would abort the INSERT
    # with an opaque IntegrityError — raise a clear, actionable error naming
    # the collision instead. A silent no-op here would re-break public ingest
    # (the sentinel row is its FK target).
    slug_holder = bind.execute(
        sa.text("SELECT id FROM organisations WHERE slug = :slug AND deleted_at IS NULL AND id <> :id LIMIT 1"),
        {"slug": _ORPHAN_ORG_SLUG, "id": _ORPHAN_ORG_ID},
    ).scalar_one_or_none()
    if slug_holder is not None:
        raise RuntimeError(
            f"Migration {revision}: cannot seed the orphan organisation — slug "
            f"'{_ORPHAN_ORG_SLUG}' is held by organisation {slug_holder}. "
            "Free the slug (rename or soft-delete the conflicting org) and re-run."
        )

    bind.execute(
        sa.text(_INSERT_ORPHAN_ORG),
        {
            "id": _ORPHAN_ORG_ID,
            "name": "Orphan (unattributed errors)",
            "slug": _ORPHAN_ORG_SLUG,
        },
    )

    # Post-insert existence assertion: this row is load-bearing infrastructure
    # (the FK target for every public-error-ingest event). If the INSERT was
    # swallowed by anything other than the id-conflict no-op, fail the
    # migration loudly rather than leave public ingest silently broken.
    seeded = bind.execute(
        sa.text("SELECT id FROM organisations WHERE id = :id"),
        {"id": _ORPHAN_ORG_ID},
    ).scalar_one_or_none()
    if seeded is None:
        raise RuntimeError(
            f"Migration {revision}: orphan organisation {_ORPHAN_ORG_ID} missing "
            "after INSERT — the public error-ingest FK target would not exist."
        )


def downgrade() -> None:
    # Documented no-op: the sentinel org row is PERMANENT infrastructure, not
    # seed data. It is the FK target for orphan error_events/error_groups
    # (rows that carry no customer org), so DELETE would cascade-destroy that
    # error data — or fail outright once ingest has run under append-only
    # protections. Reverting this migration therefore does not remove the row.
    pass
