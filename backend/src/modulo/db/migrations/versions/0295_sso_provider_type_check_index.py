"""Add pre-auth lookup index + provider_type CHECK for sso_providers.

Schema legs:

1. ``ix_sso_providers_type_enabled`` — partial index on
   ``(provider_type) WHERE enabled``. The pre-auth flows resolve IdP
   config globally with no org scope on every login
   (``crud.get_enabled_saml_provider``,
   ``crud.list_enabled_oidc|saml_providers``, ``auth.sso`` and
   ``routes.sso`` filter exactly ``WHERE provider_type = ? AND
   enabled``). No index previously covered the ``provider_type`` term;
   the partial predicate matches the shared enabled-only read shape so
   disabled rows never enter the index.

2. ``ck_sso_providers_provider_type`` — the provider_type vocabulary
   (oidc/saml) was enforced only at the API boundary
   (``admin_sso`` pattern ``^(oidc|saml)$``); ``crud.create_provider``
   itself does not validate and the startup env-import path hardcodes
   its value without a check, so a bad value would persist unchecked
   and — because auth branches on exact equality — silently disable
   SSO. Now enforced at the DB level. NULL-safe: legacy rows predate
   the column as NULL and CHECK passes on NULL.

The index is created with ``IF NOT EXISTS`` (the guarded style used by
0177) so a re-run never fails. Both legs are Postgres-only DDL: the
``public.``-qualified index SQL has no SQLite equivalent, and
``op.create_check_constraint`` emits a bare ``ALTER TABLE ... ADD
CONSTRAINT`` which SQLite cannot execute. SQLite unit-test schemas get
the same partial index and the same CHECK rule from the
``SsoProvider`` model's ``__table_args__`` via ``create_all`` (the
0246/0291 precedent), so the SQLite-ported claim holds at the
model/``create_all`` layer, not by running this migration's DDL there.

Downgrade: drops the index and the constraint.

Revision ID: 0295_sso_provider_type_check_index
Revises: 0294_eval_results_org_fk
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0295_sso_provider_type_check_index"
down_revision: str | None = "0294_eval_results_org_fk"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_sso_providers_type_enabled "
            "ON public.sso_providers (provider_type) "
            "WHERE enabled"
        )
    )
    if not _is_postgres():
        return
    op.create_check_constraint(
        "ck_sso_providers_provider_type",
        "sso_providers",
        sa.text("provider_type IN ('oidc', 'saml')"),
    )


def downgrade() -> None:
    if _is_postgres():
        op.drop_constraint("ck_sso_providers_provider_type", "sso_providers", type_="check")
    op.execute(sa.text("DROP INDEX IF EXISTS public.ix_sso_providers_type_enabled"))
