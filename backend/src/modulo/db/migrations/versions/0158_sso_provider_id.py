"""Add provider_id slug column to sso_providers (FAR-457; FAR-464 option a).

Revision ID: 0158_sso_provider_id
Revises: 0157_add_numeric_check_constraints
Create Date: 2026-08-26

The admin SSO UI writes providers to the sso_providers table, but the runtime
OIDC/SAML flows only read IdP config from env vars. This adds a `provider_id`
slug column (the URL key used at /api/v1/auth/oidc/{provider_id}/login) and a
partial UNIQUE index on ``provider_id`` so the runtime can resolve IdP config
from the DB first, falling back to env vars for backward compat.

The index is GLOBAL (module 0158; FAR-464 option a), not per-org, because
pre-auth SSO provider resolution runs system-scoped and resolves a provider by
its ``provider_id`` slug with ``scalar_one_or_none()`` — a per-org unique index
permits two orgs to both define ``provider_id='okta'``, which would make that
global read return both rows and crash with ``MultipleResultsFound``. A single
globally-unique slug therefore must be enforced across ALL orgs so the
system-session resolution is deterministic.

Existing rows are backfilled: the slug is derived from `name`, trimmed of
non-alphanumerics, defaulted to `sso`, truncated to 58 chars, and de-duplicated
ACROSS ALL ORGS with a ``-N`` suffix so the global unique index can never abort
on a collision (the suffix always fits ``String(64)``). The column is nullable
so the unique index can stay partial.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0158_sso_provider_id"
down_revision: str | None = "0157_add_numeric_check_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _slugify(value: str) -> str:
    """Derive a URL-safe, 58-char max provider_id slug from a name.

    Mirrors ``crud.sso_provider._slugify_provider_id`` so the migration produces
    the same slugs the app would. 58 leaves room for a ``-N`` dedupe suffix
    inside ``String(64)``.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value or "").strip("-").lower() or "sso"
    return slug[:58]


def _backfill_sqlite(bind: Any) -> None:
    """Collision-safe provider_id backfill for SQLite (no regexp_replace/DO block).

    Walks the rows, deriving the slug from `name` and appending ``-N`` while a
    GLOBAL ``(provider_id)`` collision exists anywhere in the table (dedupe
    across ALL orgs, matching the global partial unique index).
    """
    rows = bind.execute(
        sa.text("SELECT id, organisation_id, name FROM sso_providers WHERE provider_id IS NULL")
    ).fetchall()
    for row in rows:
        base = _slugify(row.name)
        candidate = base
        n = 2
        while (
            bind.execute(
                sa.text("SELECT 1 FROM sso_providers WHERE provider_id = :pid AND id <> :row_id"),
                {"pid": candidate, "row_id": row.id},
            ).first()
            is not None
        ):
            candidate = f"{base}-{n}"
            n += 1
        bind.execute(
            sa.text("UPDATE sso_providers SET provider_id = :pid WHERE id = :row_id"),
            {"pid": candidate, "row_id": row.id},
        )


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("SET search_path TO public")

    op.add_column("sso_providers", sa.Column("provider_id", sa.String(length=64), nullable=True))

    if is_pg:
        op.execute("SET search_path TO public")
        op.execute(
            """
        DO $$
        DECLARE r RECORD; base text; cand text; n int;
        BEGIN
          FOR r IN SELECT id, organisation_id, name FROM sso_providers WHERE provider_id IS NULL LOOP
            base := lower(regexp_replace(r.name, '[^a-zA-Z0-9]+', '-', 'g'));
            base := trim(both '-' from base);
            IF base = '' THEN base := 'sso'; END IF;
            base := left(base, 58);
            cand := base; n := 2;
            WHILE EXISTS (SELECT 1 FROM sso_providers WHERE provider_id = cand AND id <> r.id) LOOP
              cand := base || '-' || n; n := n + 1;
            END LOOP;
            UPDATE sso_providers SET provider_id = cand WHERE id = r.id;
          END LOOP;
        END $$;
        """
        )
    else:
        _backfill_sqlite(bind)

    if is_pg:
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sso_providers_provider_id "
            "ON sso_providers (provider_id) WHERE provider_id IS NOT NULL"
        )
    else:
        op.create_index(
            "uq_sso_providers_provider_id",
            "sso_providers",
            ["provider_id"],
            unique=True,
            sqlite_where=sa.text("provider_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("SET search_path TO public")

    op.drop_index("uq_sso_providers_provider_id", table_name="sso_providers")
    op.drop_column("sso_providers", "provider_id")
