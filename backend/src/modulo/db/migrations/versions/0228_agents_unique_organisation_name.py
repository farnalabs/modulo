"""Add UNIQUE(organisation_id, name) constraint on agents.

Revision ID: 0228_agents_unique_organisation_name
Revises: 0227_env_profiles_initialisation_strategy_check
Create Date: 2026-09-14

PR #458 (migrations 0223/0225) intended to enforce per-organisation agent-name
uniqueness but only declared the constraint on the ORM model (Agent.__table_args__
``uq_agents_organisation_name``) in the follow-up fix #462 — no migration was ever
written to create it on the table.  As a result ``test_initial_migration.py ::
test_migrated_schema_matches_orm_metadata`` reports permanent schema drift: the
ORM metadata wants to ``add_constraint`` the missing unique constraint.

This migration creates the constraint so the migrated schema matches the ORM
metadata.  It is added with ``NOT VALID`` on PostgreSQL so the DDL does not scan
existing rows; a validation pass follows.  Per-tenant uniqueness is enforced by
the model's ``UniqueConstraint("organisation_id", "name")`` and is consistent with
the existing RLS tenant isolation on ``agents``.
"""

from __future__ import annotations

from alembic import op

revision: str = "0228_agents_unique_organisation_name"
down_revision: str | None = "0227_env_profiles_initialisation_strategy_check"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT_NAME = "uq_agents_organisation_name"
_TABLE = "agents"
_COLUMNS = ["organisation_id", "name"]


def upgrade() -> None:
    op.create_unique_constraint(_CONSTRAINT_NAME, _TABLE, _COLUMNS)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE, type_="unique")
