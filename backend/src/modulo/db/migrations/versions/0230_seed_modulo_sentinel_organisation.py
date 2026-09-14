"""Seed the Modulo sentinel organisation backing built-in library primitives.

Revision ID: 0230_seed_modulo_sentinel_organisation
Revises: 0229_add_workspace_inputs_count
Create Date: 2026-09-14

Collection install (FAR-826) persists the shipped in-code registry collection
under a fixed sentinel organisation (00000000-0000-0000-0000-000000000001) so
``collection_install.collection_id`` has an FK target. The sentinel org used to
be created lazily INSIDE the install transaction with slug ``modulo`` — but
``organisations.slug`` carries a partial-unique index, so any self-hosted
instance that already had an org with slug ``modulo`` got an opaque
IntegrityError (409) on EVERY install, and the lazily-created row also leaked
into org listings. The sentinel is infrastructure, so it is seeded HERE —
deterministically present at upgrade time — and the install path only reads it.

Collision safety (three layers):

1. Reserved slug: underscore-prefixed ``_modulo_sentinel`` slugs are reserved
   for system sentinels by convention (tenant orgs are provisioned with
   human-facing slugs), so a live tenant collision is not expected.
2. Pre-existing id is a no-op: ``ON CONFLICT (id) DO NOTHING`` — an instance
   that already created the sentinel row (via the old lazy path, keeping its
   ``modulo`` slug) is left untouched; the slug is not rewritten.
3. Pre-existing slug never fails the upgrade: if a live org already holds a
   candidate slug, the next candidate in the deterministic chain is used
   instead of aborting (unlike migration 0172, whose sentinel slug is
   load-bearing for lookups — this one is not: the install path looks the
   sentinel up BY ID, so the chosen slug value is irrelevant to behaviour).

RLS precision: the ``organisations`` table has NO row-level security (migration
0001 enables RLS only on ``sso_providers`` and the identity-bootstrap tables),
so the sentinel row is readable by every role — including the non-superuser
``modulo_app`` runtime role under any org's RLS context. The install path's
``session.get(Organisation, sentinel_id)`` therefore works unchanged under the
app role; this is proven by the RLS-visibility assertion in
``tests/integration/test_library_collection_lifecycle.py``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0230_seed_modulo_sentinel_organisation"
down_revision: str | None = "0229_add_workspace_inputs_count"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Hardcoded twin of modulo.core.library_service._seed_data.MODULO_ORG_ID —
# migrations never import application constants.
_MODULO_ORG_ID = "00000000-0000-0000-0000-000000000001"

# Deterministic reserved-slug chain. The first candidate not held by a live
# (deleted_at IS NULL) org wins; the chain is walked, never branched, so every
# environment converges on the same slug unless a collision forces the fallback.
_SLUG_CANDIDATES: tuple[str, ...] = (
    "_modulo_sentinel",
    "_modulo_sentinel_2",
    "_modulo_sentinel_3",
    "_modulo_sentinel_4",
    "_modulo_sentinel_5",
    "_modulo_sentinel_6",
    "_modulo_sentinel_7",
    "_modulo_sentinel_8",
    "_modulo_sentinel_9",
    "_modulo_sentinel_10",
)

_INSERT_SENTINEL_ORG = (
    "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
    "VALUES (:id, :name, :slug, '{}', '{}') "
    "ON CONFLICT (id) DO NOTHING"
)


def upgrade() -> None:
    bind = op.get_bind()

    # Already-existing id → no-op (layer 2). An instance that ran the old lazy
    # creation path already has the row; its slug (possibly the legacy
    # ``modulo``) is left exactly as-is.
    exists = bind.execute(
        sa.text("SELECT 1 FROM organisations WHERE id = :id"),
        {"id": _MODULO_ORG_ID},
    ).scalar_one_or_none()
    if exists is not None:
        return

    # Pick the first candidate slug not held by a LIVE org (layer 3). The
    # partial-unique index (uq_organisations_slug, WHERE deleted_at IS NULL)
    # only constrains live rows, so soft-deleted holders are ignored. Failing
    # after exhausting the chain is deliberate: ten reserved-slug collisions
    # indicate a corrupted naming state where a silent wrong-slug insert would
    # be worse than a loud, actionable upgrade failure.
    chosen_slug: str | None = None
    for candidate in _SLUG_CANDIDATES:
        holder = bind.execute(
            sa.text("SELECT id FROM organisations WHERE slug = :slug AND deleted_at IS NULL AND id <> :id LIMIT 1"),
            {"slug": candidate, "id": _MODULO_ORG_ID},
        ).scalar_one_or_none()
        if holder is None:
            chosen_slug = candidate
            break
    if chosen_slug is None:
        raise RuntimeError(
            f"Migration {revision}: every reserved sentinel slug "
            f"({_SLUG_CANDIDATES[0]}..{_SLUG_CANDIDATES[-1]}) is held by live "
            "organisations — this indicates a corrupted naming state. Free one "
            "of the reserved slugs (rename or soft-delete the conflicting org) "
            "and re-run the migration."
        )

    bind.execute(
        sa.text(_INSERT_SENTINEL_ORG),
        {"id": _MODULO_ORG_ID, "name": "Modulo", "slug": chosen_slug},
    )

    # Post-insert existence assertion: this row is load-bearing infrastructure
    # (the FK target for collection installs of shipped registry collections).
    # If the INSERT was swallowed by anything other than the id-conflict no-op,
    # fail the migration loudly rather than leave installs silently broken.
    seeded = bind.execute(
        sa.text("SELECT id FROM organisations WHERE id = :id"),
        {"id": _MODULO_ORG_ID},
    ).scalar_one_or_none()
    if seeded is None:
        raise RuntimeError(
            f"Migration {revision}: Modulo sentinel organisation {_MODULO_ORG_ID} "
            "missing after INSERT — collection installs would fail their "
            "library_primitives FK target lookup."
        )


def downgrade() -> None:
    # Documented no-op: the sentinel org row is PERMANENT infrastructure, not
    # seed data. It is the FK target for collection_install rows referencing
    # shipped registry collections, so DELETE would cascade-destroy install
    # provenance (or fail outright once installs exist). Reverting this
    # migration therefore does not remove the row.
    pass
