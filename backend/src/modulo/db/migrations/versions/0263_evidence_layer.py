"""FAR-966 chunk 7: create generic Evidence table + RLS (§3.1).

Append-only fact store for the generic Evidence layer.  Every org-scoped
row carries a composite unique constraint that supports DISTINCT ON lookups
and the two covering indexes spec'd in §3.1.

Row Level Security follows the mandatory four-step pattern (ownership
transfer to modulo_migrate, ENABLE, FORCE, rls_org_isolation policy)
mirrored from migration 0250_eval_policy_gate.

Revision ID: 0263_evidence_layer
Revises: 0262_hitl_gate_to_review_vocabulary
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import JSONB

revision = "0263_evidence_layer"
down_revision = "0262_hitl_gate_to_review_vocabulary"
branch_labels = None
depends_on = None

# CHECK-constraint vocabulary — authoritative for valid producer_type values.
_PRODUCER_TYPE_VALUES = ("eval", "policy_gate", "run", "system_state", "derived")
_producer_type_sql = ", ".join(f"'{v}'" for v in _PRODUCER_TYPE_VALUES)

# Row Level Security — org isolation policy (mirrors 0250_eval_policy_gate).
_ORG_ISOLATION_POLICY = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_TABLE = "evidence"

_OWNER_TRANSFER_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'modulo_migrate') "
    "THEN ALTER TABLE public.{table} OWNER TO modulo_migrate; END IF; END $$;"
)

_GRANT_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'modulo_app') "
    "THEN GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO modulo_app; END IF; END $$;"
)


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if _TABLE in existing_tables:
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
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("value", JSONB(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "producer_type",
            sa.Text(),
            nullable=False,
        ),
        sa.Column("producer_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            f"producer_type IN ({_producer_type_sql})",
            name="ck_evidence_producer_type",
        ),
        # Composite unique supporting DISTINCT ON lookup (§3.1).
        sa.UniqueConstraint(
            "organisation_id",
            "subject_type",
            "subject_id",
            "key",
            "created_at",
            "id",
            name="uq_evidence_distinct_on",
        ),
    )

    # Indexes per §3.1.
    op.create_index(
        "ix_evidence_org_subject",
        _TABLE,
        ["organisation_id", "subject_type", "subject_id", "key"],
    )
    # DESC index via raw SQL — op.create_index doesn't support text() columns.
    op.execute(
        text(
            "CREATE INDEX ix_evidence_created_at ON public.evidence "
            "(organisation_id, subject_type, subject_id, key, created_at DESC, id DESC)"
        )
    )

    # Four-step RLS pattern (§3.1, mirroring 0250_eval_policy_gate).
    # Step 1: ownership transfer to modulo_migrate.
    op.execute(text(_OWNER_TRANSFER_SQL.format(table=_TABLE)))
    # Step 2: enable RLS.
    op.execute(text('ALTER TABLE public."' + _TABLE + '" ENABLE ROW LEVEL SECURITY'))
    # Step 3: force RLS.
    op.execute(text('ALTER TABLE public."' + _TABLE + '" FORCE ROW LEVEL SECURITY'))
    # Step 4: org-isolation policy.
    op.execute(text('DROP POLICY IF EXISTS rls_org_isolation ON public."' + _TABLE + '"'))
    op.execute(text('CREATE POLICY rls_org_isolation ON public."' + _TABLE + '" USING (' + _ORG_ISOLATION_POLICY + ")"))

    # Grant DML to runtime role when the role exists.
    op.execute(text(_GRANT_SQL.format(table=_TABLE)))


def downgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if _TABLE not in existing_tables:
        return

    # Drop RLS policy first.
    op.execute(text('DROP POLICY IF EXISTS rls_org_isolation ON public."' + _TABLE + '"'))
    op.execute(text('ALTER TABLE public."' + _TABLE + '" DISABLE ROW LEVEL SECURITY'))

    # Drop indexes.
    op.drop_index("ix_evidence_created_at", table_name=_TABLE)
    op.drop_index("ix_evidence_org_subject", table_name=_TABLE)

    # Drop table (indexes and constraints drop with it).
    op.drop_table(_TABLE)
