"""Add CHECK constraints, FORCE RLS, and tenant trigger on agents.

Revision ID: 0225_agents_constraints_rls
Revises: 0224_agents_add_indexes
Create Date: 2026-09-13

Constraints added:
  - ``ck_agents_token_budget`` — token_budget must be NULL or positive.
  - ``ck_agents_max_input_length`` — max_input_length must be NULL or
    positive.

Security:
  - ``ALTER TABLE agents FORCE ROW LEVEL SECURITY`` — prevents the table
    owner from bypassing RLS.
  - ``trg_agents_parameter_schema_id_tenant`` — enforces that
    ``parameter_schema_id`` belongs to the same organisation.
"""

import sqlalchemy as sa
from alembic import op

revision = "0225_agents_constraints_rls"
down_revision = "0224_agents_add_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_agents_token_budget",
        "agents",
        sa.text("token_budget IS NULL OR token_budget > 0"),
    )
    op.create_check_constraint(
        "ck_agents_max_input_length",
        "agents",
        sa.text("max_input_length IS NULL OR max_input_length > 0"),
    )
    op.execute("ALTER TABLE public.agents FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE TRIGGER trg_agents_parameter_schema_id_tenant
        BEFORE INSERT OR UPDATE OF parameter_schema_id, organisation_id
        ON public.agents
        FOR EACH ROW
        EXECUTE FUNCTION public.enforce_same_organisation('parameter_schemas', 'parameter_schema_id');
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_agents_parameter_schema_id_tenant ON public.agents;")
    op.execute("ALTER TABLE public.agents NO FORCE ROW LEVEL SECURITY;")
    op.drop_constraint("ck_agents_max_input_length", "agents", type_="check")
    op.drop_constraint("ck_agents_token_budget", "agents", type_="check")
