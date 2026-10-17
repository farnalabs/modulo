"""FAR-944 chunk 9b §2.4: create evidence_redaction_rules + RLS + catch-all seed.

The redaction boundary's configuration table (see
``modulo.db.models.evidence_redaction_rule``). A small per-org table mapping
``(role, key_pattern)`` to the set of redaction CLASSES whose evidence-value
fields are replaced with the ``"[REDACTED]"`` sentinel for that role.

Upgrade:
1. Create the table.
2. Seed the MANDATORY catch-all row for every existing organisation:
   ``key_pattern='*'``, ``role='viewer'``, ``redacted_fields='{sensitive_output, internal_metadata}'``.
   This ensures a newly-added evidence key carrying sensitive content is
   redacted for viewers before an admin explicitly classifies it. Redaction is
   name-based: a class covers a fixed set of JSON key NAMES, so the catch-all
   only hides keys whose names are in a class — it is not a default-deny
   allowlist. The seed runs BEFORE RLS is enabled (the migration role would
   otherwise be subject to the FORCE-RLS policy).
3. Apply the mandatory four-step RLS pattern (ownership transfer to
   modulo_migrate, ENABLE, FORCE, rls_org_isolation policy) mirrored from
   ``0263_evidence_layer``.

Orgs created AFTER this migration are seeded with the same catch-all row by the
org-creation path (``modulo.db.crud.organisation.create_organisation`` →
``_seed_evidence_redaction_catch_all``). If a rule is nevertheless absent, the
redaction service (``modulo.core.evidence_redaction.resolve_redaction_classes_from``)
fails CLOSED: a trusted role (``admin``/``operator``) with no matching rule is
unredacted, and EVERY other role — including the default org role ``runner`` —
falls back to BOTH classes. (This deliberately narrows acceptance criterion 3's
"no matching rule -> no redaction" to trusted roles only.)

Revision ID: 0318_evidence_redaction_rules
Revises: 0317_drop_retired_pipeline_diff_rollback_flag
Create Date: 2026-10-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0318_evidence_redaction_rules"
down_revision = "0317_drop_retired_pipeline_diff_rollback_flag"
branch_labels = None
depends_on = None

_TABLE = "evidence_redaction_rules"

# Style 2 for the RLS-coverage architecture test (test_rls_coverage.py): it
# imports migration modules and reads any module-level tuple/list whose
# elements are all strings. Every table that receives ENABLE ROW LEVEL
# SECURITY below must appear here.
_ORG_SCOPED_TABLES = (_TABLE,)

_ORG_ISOLATION_POLICY = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_INDEX_NAME = "ix_evidence_redaction_rules_organisation_id"
_NOW_SERVER_DEFAULT = "now()"

# Shared DDL fragments (S1192) — byte-identical to the inline literals.
_RLS_ALTER_PREFIX = 'ALTER TABLE public."'

_OWNER_TRANSFER_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'modulo_migrate') "
    "THEN ALTER TABLE public.{table} OWNER TO modulo_migrate; END IF; END $$;"
)

_GRANT_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'modulo_app') "
    "THEN GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO modulo_app; END IF; END $$;"
)

# Mandatory catch-all seed (§2.4): viewer redacts both classes for any key
# before an admin classifies it. Guarded by NOT EXISTS so a re-run is a no-op.
_SEED_CATCH_ALL_SQL = (
    "INSERT INTO public.evidence_redaction_rules "
    "(organisation_id, key_pattern, role, redacted_fields) "
    "SELECT o.id, '*', 'viewer', ARRAY['sensitive_output', 'internal_metadata']::text[] "
    "FROM organisations o "
    "WHERE NOT EXISTS ("
    "SELECT 1 FROM public.evidence_redaction_rules r "
    "WHERE r.organisation_id = o.id AND r.key_pattern = '*' AND r.role = 'viewer'"
    ")"
)


def _enable_rls() -> None:
    """Enable + FORCE RLS with the org-isolation policy on all new tables."""
    for table in _ORG_SCOPED_TABLES:
        # Step 1: ownership transfer to modulo_migrate.
        op.execute(text(_OWNER_TRANSFER_SQL.format(table=table)))
        # Step 2: enable RLS.
        op.execute(text(_RLS_ALTER_PREFIX + table + '" ENABLE ROW LEVEL SECURITY'))
        # Step 3: force RLS.
        op.execute(text(_RLS_ALTER_PREFIX + table + '" FORCE ROW LEVEL SECURITY'))
        # Step 4: org-isolation policy.
        op.execute(text('DROP POLICY IF EXISTS rls_org_isolation ON public."' + table + '"'))
        op.execute(
            text('CREATE POLICY rls_org_isolation ON public."' + table + '" USING (' + _ORG_ISOLATION_POLICY + ")")
        )


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column(
            "organisation_id",
            sa.Uuid(),
            sa.ForeignKey("organisations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text(_NOW_SERVER_DEFAULT),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text(_NOW_SERVER_DEFAULT),
            nullable=False,
        ),
        sa.Column("key_pattern", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("redacted_fields", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "role IN ('admin', 'operator', 'viewer')",
            name="ck_evidence_redaction_rules_role",
        ),
        sa.UniqueConstraint(
            "organisation_id",
            "key_pattern",
            "role",
            name="uq_evidence_redaction_rules_org_key_role",
        ),
    )
    op.create_index(_INDEX_NAME, _TABLE, ["organisation_id"])

    # Seed the catch-all row for every existing organisation BEFORE RLS is
    # enabled (a FORCE-RLS table would subject the migration role to the policy).
    op.execute(text(_SEED_CATCH_ALL_SQL))

    # Four-step RLS pattern (mirroring 0263_evidence_layer).
    _enable_rls()

    # Grant DML to the runtime role when the role exists.
    op.execute(text(_GRANT_SQL.format(table=_TABLE)))


def downgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    # Drop RLS policy first.
    op.execute(text('DROP POLICY IF EXISTS rls_org_isolation ON public."' + _TABLE + '"'))
    op.execute(text(_RLS_ALTER_PREFIX + _TABLE + '" DISABLE ROW LEVEL SECURITY'))

    op.drop_index(_INDEX_NAME, table_name=_TABLE)
    op.drop_table(_TABLE)
