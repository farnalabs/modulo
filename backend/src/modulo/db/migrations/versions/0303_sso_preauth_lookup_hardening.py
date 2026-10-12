"""Harden sso_providers pre-auth lookups + provider_type vocabulary.

Schema legs (all on ``sso_providers``):

1. ``ix_sso_providers_type_enabled`` — composite index on
   ``(provider_type, enabled)``. The pre-auth hot path resolves IdP config
   globally (no org scope, ``modulo_system`` BYPASSRLS session) on exactly
   this shape at every login: ``get_enabled_saml_provider``,
   ``list_enabled_oidc_providers`` and ``list_enabled_saml_providers`` in
   ``db/crud/sso_provider.py`` all filter ``WHERE provider_type = ? AND
   enabled``. The only pre-existing index is the single-column
   ``ix_sso_providers_organisation_id`` (migration 0001), which cannot serve
   an org-less global scan, so those queries fall back to a sequential scan
   past it on every SSO login/callback.

2. ``ck_sso_providers_type`` — ``provider_type IN ('oidc', 'saml')``. The
   vocabulary has been API-enforced since the column's introduction
   (``admin_sso`` create schema: ``^(oidc|saml)$``); every ORM fixture, BDD
   step and raw-SQL integration seed uses only these two values, so no
   existing row can violate the constraint. Now enforced at the DB level so
   a bad write outside the API (raw SQL, manual insert) cannot persist.

Deliberately NOT included: a CHECK on ``preset`` (extensible registry — a
new preset must not require a DDL migration) and one on ``default_role``
(wider in practice than the create API suggests — integration seeds use
``'viewer'``). The ``(organisation_id, name)`` duplicate guard stays
app-level (``crud.create_provider``): promoting it to a UNIQUE constraint
would need a data-risky dedupe of user-visible names, so it is logged as a
deferred gap instead.

The ``uq_sso_providers_provider_id`` global partial unique index (migration
0158, FAR-464 option a) already exists in Postgres; this migration does NOT
re-create it — the model now declares it in ``__table_args__`` so ORM
metadata matches the DB (0158 remains the migration owner).

Both legs are Postgres-only: the ``public.``-qualified index SQL has no
SQLite equivalent, and ``op.create_check_constraint`` emits a bare
``ALTER TABLE ... ADD CONSTRAINT`` which SQLite cannot execute. SQLite
unit-test schemas get the same composite index, the same partial unique
index and the same CHECK rule from the ``SsoProvider`` model's
``__table_args__`` via ``create_all`` (the 0246 precedent), so the
SQLite-ported claim holds at the model/``create_all`` layer, not by running
this migration's DDL there. The CHECK creation is additionally gated on
``pg_constraint`` so a re-run after a partial failure never errors on a
duplicate constraint name.

Downgrade: drops the CHECK then the index.

Revision ID: 0303_sso_preauth_lookup_hardening
Revises: 0302_runs_run_grouping
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0303_sso_preauth_lookup_hardening"
down_revision: str | None = "0302_runs_run_grouping"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_CHECK = "ck_sso_providers_type"


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_sso_providers_type_enabled ON public.sso_providers (provider_type, enabled)"
        )
    )
    if not _is_postgres():
        return
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
        {"name": _CHECK},
    ).first()
    if exists is None:
        op.create_check_constraint(
            _CHECK,
            "sso_providers",
            sa.text("provider_type IN ('oidc', 'saml')"),
        )


def downgrade() -> None:
    if _is_postgres():
        op.drop_constraint(_CHECK, "sso_providers", type_="check")
    op.execute(sa.text("DROP INDEX IF EXISTS public.ix_sso_providers_type_enabled"))
