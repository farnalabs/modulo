"""FAR-853: add SSO provider presets.

Adds ``preset`` (String(32), NOT NULL, default ``custom``) and
``tenant_domain`` (String(255), nullable) to ``sso_providers``.

Schema legs (Postgres only; SQLite/ORM-created test schemas get the columns
and server_defaults from the ``SsoProvider`` model's ``create_all``):

1. ``sso_providers.preset`` — String(32), NOT NULL, server_default ``'custom'``.
   Identifies a native preset (google, auth0, okta, azure-ad, onelogin) or
   ``custom`` (admin-supplied discovery/scopes).
2. ``sso_providers.tenant_domain`` — String(255), nullable.  The tenant
   identifier interpolated into a preset's discovery URL template.

No data legs: existing rows get the ``'custom'`` server_default, which is
correct — pre-existing providers are custom-configured.

Downgrade: drops both columns.

Revision ID: 0247_sso_presets
Revises: 0246_sso_join_gate
Create Date: 2026-09-16
"""

from alembic import op
from sqlalchemy import inspect, text

revision = "0247_sso_presets"
down_revision = "0246_sso_join_gate"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns("sso_providers")}
    if "preset" not in columns:
        bind.execute(  # nosec B608
            text("ALTER TABLE public.sso_providers ADD COLUMN preset varchar(32) NOT NULL DEFAULT 'custom'")
        )
    if "tenant_domain" not in columns:
        bind.execute(  # nosec B608
            text("ALTER TABLE public.sso_providers ADD COLUMN tenant_domain varchar(255)")
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns("sso_providers")}
    if "tenant_domain" in columns:
        bind.execute(text("ALTER TABLE public.sso_providers DROP COLUMN IF EXISTS tenant_domain"))
    if "preset" in columns:
        bind.execute(text("ALTER TABLE public.sso_providers DROP COLUMN IF EXISTS preset"))
