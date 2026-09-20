"""FAR-1060 chunk 1: create Eval, PolicyGate, PolicyGateDecision tables.

Net-new tables for the eval/policy-gate taxonomy.  Nothing reads or writes
these tables in production yet; the migration creates the schema surface only.

After creation the tables are enabled with Row Level Security (FORCE) and
owned by ``modulo_migrate`` so the runtime ``modulo_app`` role (non-owner)
is filtered by the org-isolation policy.

Downgrade drops the RLS policies and the three tables (and their indexes)
in reverse dependency order.

Revision ID: 0250_eval_policy_gate
Revises: 0249_validation_level
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision = "0250_eval_policy_gate"
down_revision = "0249_validation_level"
branch_labels = None
depends_on = None

# Centralised eval_type vocabulary — MUST match the EvalDefinition CHECK constraint.
_VALID_EVAL_TYPES = (
    "llm_judge",
    "regex",
    "json_schema",
    "custom_function",
    "guardrail",
    "human_set",
)
_eval_type_sql = ", ".join(f"'{v}'" for v in _VALID_EVAL_TYPES)

# Row Level Security — org isolation policy (mirrors 0130_eval_suite_entity).
_ORG_ISOLATION_POLICY = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

# Ownership transfer to migration role so modulo_app (non-owner) is RLS-filtered.
_NEW_TABLES = ("evals", "policy_gates", "policy_gate_decisions")

_OWNER_TRANSFER_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'modulo_migrate') "
    "THEN ALTER TABLE public.{table} OWNER TO modulo_migrate; END IF; END $$;"
)


def _transfer_ownership() -> None:
    for table in _NEW_TABLES:
        op.execute(text(_OWNER_TRANSFER_SQL.format(table=table)))


def _enable_rls() -> None:
    """Enable + FORCE RLS with the org-isolation policy on all new tables."""
    for table in _NEW_TABLES:
        op.execute(text('ALTER TABLE "' + table + '" ENABLE ROW LEVEL SECURITY'))
        op.execute(text('ALTER TABLE "' + table + '" FORCE ROW LEVEL SECURITY'))
        op.execute(text('DROP POLICY IF EXISTS rls_org_isolation ON "' + table + '"'))
        op.execute(text('CREATE POLICY rls_org_isolation ON "' + table + '" USING (' + _ORG_ISOLATION_POLICY + ")"))


def _drop_rls() -> None:
    """Drop the org-isolation policy from all new tables."""
    for table in _NEW_TABLES:
        op.execute(text('DROP POLICY IF EXISTS rls_org_isolation ON "' + table + '"'))


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. evals
    if "evals" not in existing_tables:
        op.create_table(
            "evals",
            sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
            sa.Column(
                "organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False
            ),
            sa.Column("version", sa.Integer(), server_default="1", nullable=False),
            sa.Column("pre_version_raw", sa.JSON(), nullable=True),
            sa.Column("pipeline_id", sa.Uuid(), sa.ForeignKey("pipelines.id", ondelete="CASCADE"), nullable=False),
            sa.Column("node_id", sa.Uuid(), nullable=True),
            sa.Column("name", sa.Text(), nullable=True),
            sa.Column("suite_id", sa.Text(), nullable=True),
            sa.Column("eval_suite_id", sa.Uuid(), sa.ForeignKey("eval_suites.id", ondelete="SET NULL"), nullable=True),
            sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("eval_type", sa.Text(), nullable=False),
            sa.Column("config_json", sa.JSON(), server_default="{}", nullable=False),
            sa.Column("pass_threshold", sa.Numeric(), nullable=True),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", sa.Uuid(), nullable=True),
            sa.CheckConstraint(
                f"eval_type IN ({_eval_type_sql})",
                name="ck_evals_type",
            ),
            sa.CheckConstraint(
                "pass_threshold IS NULL OR pass_threshold BETWEEN 0 AND 1",
                name="ck_evals_pass_threshold",
            ),
            sa.UniqueConstraint("id", "organisation_id", name="uq_evals_id_organisation_id"),
        )
        op.create_index("ix_evals_pipeline_id", "evals", ["pipeline_id"])
        op.create_index("ix_evals_organisation_id", "evals", ["organisation_id"])
        op.create_index("ix_evals_account_id", "evals", ["account_id"])

    # 2. policy_gates
    if "policy_gates" not in existing_tables:
        op.create_table(
            "policy_gates",
            sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
            sa.Column(
                "organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False
            ),
            sa.Column("version", sa.Integer(), server_default="1", nullable=False),
            sa.Column("pre_version_raw", sa.JSON(), nullable=True),
            sa.Column("eval_id", sa.Uuid(), nullable=False),
            sa.Column("node_id", sa.Uuid(), nullable=False),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", sa.Uuid(), nullable=True),
            sa.CheckConstraint(
                "action IN ('warn', 'block')",
                name="ck_policy_gates_action",
            ),
            sa.ForeignKeyConstraint(
                ["eval_id", "organisation_id"],
                ["evals.id", "evals.organisation_id"],
                ondelete="CASCADE",
                name="fk_policy_gates_eval_org",
            ),
            sa.UniqueConstraint("id", "organisation_id", name="uq_policy_gates_id_organisation_id"),
        )
        op.create_index("ix_policy_gates_organisation_id", "policy_gates", ["organisation_id"])
        # Partial unique index: at most one live gate per Eval.
        op.create_index(
            "uq_policy_gates_eval_id_live",
            "policy_gates",
            ["eval_id"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )

    # 3. policy_gate_decisions
    if "policy_gate_decisions" not in existing_tables:
        op.create_table(
            "policy_gate_decisions",
            sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
            sa.Column(
                "organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False
            ),
            sa.Column("policy_gate_id", sa.Uuid(), nullable=False),
            sa.Column("eval_id", sa.Uuid(), nullable=False),
            sa.ForeignKeyConstraint(
                ["policy_gate_id", "organisation_id"],
                ["policy_gates.id", "policy_gates.organisation_id"],
                ondelete="RESTRICT",
                name="fk_policy_gate_decisions_gate_org",
            ),
            sa.ForeignKeyConstraint(
                ["eval_id", "organisation_id"],
                ["evals.id", "evals.organisation_id"],
                ondelete="RESTRICT",
                name="fk_policy_gate_decisions_eval_org",
            ),
            sa.UniqueConstraint("id", "organisation_id", name="uq_policy_gate_decisions_id_organisation_id"),
        )
        op.create_index("ix_policy_gate_decisions_organisation_id", "policy_gate_decisions", ["organisation_id"])

    # 4. Row Level Security (org isolation) on all three new tables.
    _enable_rls()

    # Ensure the runtime modulo_app role is a non-owner (filtered by RLS).
    _transfer_ownership()


def downgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # Drop RLS policies before dropping tables.
    _drop_rls()

    # Reverse dependency order.
    if "policy_gate_decisions" in existing_tables:
        op.drop_table("policy_gate_decisions")

    if "policy_gates" in existing_tables:
        op.drop_index("uq_policy_gates_eval_id_live", table_name="policy_gates")
        op.drop_table("policy_gates")

    if "evals" in existing_tables:
        op.drop_index("ix_evals_pipeline_id", table_name="evals")
        op.drop_table("evals")
