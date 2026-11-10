"""Promote agents JSON columns to JSONB for indexable containment queries.

Revision ID: 0226_agents_json_to_jsonb
Revises: 0225_agents_constraints_rls
Create Date: 2026-09-13

Columns converted (``JSON`` → ``JSONB``):
  - agent_commands
  - prompt_version_history
  - connector_type_refs
  - required_environment_capabilities
  - evals
  - retry_policy

On PostgreSQL this rewrites the column storage from text to binary JSONB,
enabling GIN indexing, ``@>`` containment, and ``?``/``?|``/``?&``
key-existence operators.
"""

from alembic import op

revision = "0226_agents_json_to_jsonb"
down_revision = "0225_agents_constraints_rls"
branch_labels = None
depends_on = None

_columns = [
    "agent_commands",
    "prompt_version_history",
    "connector_type_refs",
    "required_environment_capabilities",
    "evals",
    "retry_policy",
]


def upgrade() -> None:
    for col in _columns:
        op.execute(f'ALTER TABLE public.agents ALTER COLUMN "{col}" TYPE jsonb USING "{col}"::jsonb;')


def downgrade() -> None:
    for col in _columns:
        op.execute(f'ALTER TABLE public.agents ALTER COLUMN "{col}" TYPE json USING "{col}"::json;')
