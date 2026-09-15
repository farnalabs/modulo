"""FAR-855: gate SSO JIT provisioning — allowed_domains + auto_provision default.

Schema legs (Postgres only; SQLite/ORM-created test schemas get the column and
server_default from the ``SsoProvider`` model's ``create_all``):

1. ``sso_providers.allowed_domains`` — JSON list of strings, NOT NULL,
   server_default ``'[]'``. Case-insensitive EXACT-match allowlist of verified
   email domains consulted when ``auto_provision`` is True; an empty list in
   that mode means "anyone who authenticates" which additionally requires the
   operator-level ``sso_unrestricted_provisioning`` feature flag (default OFF).
2. ``sso_providers.auto_provision`` — server_default flipped to ``false``
   so NEW providers default to "invitation only" (FAR-855 policy).

No data legs: existing row VALUES are deliberately left untouched (existing
providers keep ``auto_provision=true`` and now read as an empty allowlist =
mode 3, which is harmless while ``sso_unrestricted_provisioning`` is OFF —
sign-ins fail CLOSED until the operator sets ``allowed_domains``).

Downgrade: drops the column and restores the ``'true'`` server_default.

Revision ID: 0246_sso_join_gate
Revises: 0245_drop_organisations_created_by_fk
Create Date: 2026-09-15
"""

from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

revision = "0246_sso_join_gate"
down_revision = "0245_drop_organisations_created_by_fk"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns("sso_providers")}
    if "allowed_domains" not in columns:
        # Existence-guarded (idempotent re-runs); NOT NULL with a default so
        # populated tables are backfilled in the same statement.
        bind.execute(  # nosec B608
            text("ALTER TABLE public.sso_providers ADD COLUMN allowed_domains jsonb NOT NULL DEFAULT '[]'::jsonb")
        )
    bind.execute(text("ALTER TABLE public.sso_providers ALTER COLUMN auto_provision SET DEFAULT false"))


def downgrade() -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    bind.execute(text("ALTER TABLE public.sso_providers ALTER COLUMN auto_provision SET DEFAULT true"))
    columns = {c["name"] for c in inspect(bind).get_columns("sso_providers")}
    if "allowed_domains" in columns:
        bind.execute(text("ALTER TABLE public.sso_providers DROP COLUMN IF EXISTS allowed_domains"))
