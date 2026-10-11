"""Harden sso_providers enablement gates: lookup index + vocabulary CHECKs + NOT NULL.

Schema legs:

1. ``ix_sso_providers_type_enabled`` — partial index on
    ``(provider_type) WHERE enabled``. The three pre-auth global lookups
    (``crud.get_enabled_saml_provider`` / ``list_enabled_oidc_providers`` /
    ``list_enabled_saml_providers``) filter exactly
    ``WHERE provider_type = ? AND enabled`` with no org predicate, so the
    OrgScoped ``organisation_id`` index cannot serve them and every
    login-path resolution seq-scans. The partial predicate matches the
    shared "enabled" filter exactly.

2. ``ck_sso_providers_type`` — the provider_type vocabulary (oidc/saml)
    was enforced only at the API boundary (``admin_sso``
    ``^(oidc|saml)$``); ``crud.create_provider`` and the startup
    env-seed write it unchecked. Now enforced at the DB level.

3. ``ck_sso_providers_default_role`` — ``default_role`` is granted as the
    org membership role at join time but had no DB guard. The full
    org-role vocabulary (admin/operator/runner/viewer, mirroring
    ``ck_org_memberships_role``) is enforced rather than the current API
    subset (operator/runner): the SSO callback flow legitimately uses
    viewer and the startup seed takes a free-form setting, so the subset
    would reject live rows.

4. ``ck_sso_providers_preset`` — the preset vocabulary mirrors
    ``core.sso_presets.PRESETS`` (custom/google/auth0/okta/azure-ad/
    onelogin). ``_coerce_preset`` only coerces non-strings, so an unknown
    preset string previously persisted unchecked. Keep in sync when adding
    a preset (same hardcoded-vocabulary convention as
    ``trigger_events.validation_result``).

5. NOT NULL on ``enabled`` / ``auto_provision`` / ``default_role``
    (migration 0108 created them nullable with defaults; 0247 already made
    ``preset`` NOT NULL). NULL booleans sit in the pre-auth enablement
    gate and the FAR-855 join gate. Backfills are behavior-preserving:
    NULL-enabled rows are excluded from the ``WHERE enabled`` lookups
    (same as false); NULL auto_provision fails closed in Python
    truthiness checks (same as false); default_role falls back to the
    'runner' default.

The index is created with ``IF NOT EXISTS`` (the guarded style used by
0177/0291) so a re-run never fails. The backfill + index legs are written
as ``public.``-qualified raw SQL with no SQLite equivalent and the
NOT NULL / CHECK legs are Postgres-only: ``op.create_check_constraint``
emits a bare ``ALTER TABLE ... ADD CONSTRAINT`` which SQLite cannot
execute. SQLite unit-test schemas get the same partial index and the same
three CHECK rules from the ``SsoProvider`` model's ``__table_args__``
(``sqlite_where`` predicate + ``CheckConstraint``s) via ``create_all``
(the 0246/0291 precedent), so the SQLite-ported claim holds at the
model/``create_all`` layer, not by running this migration's DDL there.

Downgrade: drops the index and the three constraints and restores
nullability. Backfilled values are not un-backfilled (the pre-migration
NULLs are unrecoverable by design).

Revision ID: 0296_sso_provider_gates
Revises: 0295_decision_record_event_identity
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0296_sso_provider_gates"
down_revision: str | None = "0295_decision_record_event_identity"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    op.execute(sa.text("UPDATE public.sso_providers SET enabled = false WHERE enabled IS NULL"))
    op.execute(sa.text("UPDATE public.sso_providers SET auto_provision = false WHERE auto_provision IS NULL"))
    op.execute(sa.text("UPDATE public.sso_providers SET default_role = 'runner' WHERE default_role IS NULL"))
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_sso_providers_type_enabled "
            "ON public.sso_providers (provider_type) "
            "WHERE enabled"
        )
    )
    if not _is_postgres():
        return
    op.execute(sa.text("ALTER TABLE public.sso_providers ALTER COLUMN enabled SET NOT NULL"))
    op.execute(sa.text("ALTER TABLE public.sso_providers ALTER COLUMN auto_provision SET NOT NULL"))
    op.execute(sa.text("ALTER TABLE public.sso_providers ALTER COLUMN default_role SET NOT NULL"))
    op.create_check_constraint(
        "ck_sso_providers_type",
        "sso_providers",
        sa.text("provider_type IN ('oidc', 'saml')"),
    )
    op.create_check_constraint(
        "ck_sso_providers_default_role",
        "sso_providers",
        sa.text("default_role IN ('admin', 'operator', 'runner', 'viewer')"),
    )
    op.create_check_constraint(
        "ck_sso_providers_preset",
        "sso_providers",
        sa.text("preset IN ('custom', 'google', 'auth0', 'okta', 'azure-ad', 'onelogin')"),
    )


def downgrade() -> None:
    if _is_postgres():
        op.drop_constraint("ck_sso_providers_preset", "sso_providers", type_="check")
        op.drop_constraint("ck_sso_providers_default_role", "sso_providers", type_="check")
        op.drop_constraint("ck_sso_providers_type", "sso_providers", type_="check")
        op.execute(sa.text("ALTER TABLE public.sso_providers ALTER COLUMN default_role DROP NOT NULL"))
        op.execute(sa.text("ALTER TABLE public.sso_providers ALTER COLUMN auto_provision DROP NOT NULL"))
        op.execute(sa.text("ALTER TABLE public.sso_providers ALTER COLUMN enabled DROP NOT NULL"))
    op.execute(sa.text("DROP INDEX IF EXISTS public.ix_sso_providers_type_enabled"))
