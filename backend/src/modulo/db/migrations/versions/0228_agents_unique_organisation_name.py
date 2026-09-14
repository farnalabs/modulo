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
metadata.  Per-tenant uniqueness is enforced by the model's
``UniqueConstraint("organisation_id", "name")`` and is consistent with the
existing RLS tenant isolation on ``agents``.

Deploy-safety (PostgreSQL 16): ``CONCURRENTLY`` cannot be used here — ``env.py``
wraps every revision in a single ``engine.begin()`` transaction and ``CREATE [UNIQUE]
INDEX CONCURRENTLY`` cannot run inside a transaction block (repo precedent in
``0200`` / ``0193`` / ``0218`` / ``0154`` / ``0155``).  So we add the named
``UNIQUE`` constraint directly with ``ALTER TABLE ... ADD CONSTRAINT ... UNIQUE``
(PostgreSQL names the backing index after the constraint, so it lands as
``uq_agents_organisation_name`` to match the ORM ``UniqueConstraint``).  This takes
an ``ACCESS EXCLUSIVE`` lock and a table scan, and hard-fails if any duplicate
``(organisation_id, name)`` rows already exist — a deploy-blocking failure mode.
The duplicate pre-check below raises a loud, actionable error (rather than a bare
unique-violation) if legacy duplicate rows are present, so a conflict fails with a
clear message instead of a bare constraint violation.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0228_agents_unique_organisation_name"
down_revision: str | None = "0227_env_profiles_initialisation_strategy_check"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT_NAME = "uq_agents_organisation_name"
_TABLE = "agents"
_COLUMNS = ["organisation_id", "name"]


def upgrade() -> None:
    # Surface any pre-existing duplicate (organisation_id, name) rows BEFORE
    # building the unique index, so a conflict fails loudly with an actionable
    # message instead of a bare unique-violation.
    op.execute(
        text(
            "DO $$\n"
            "DECLARE dup_count int;\n"
            "BEGIN\n"
            "  SELECT count(*) INTO dup_count FROM (\n"
            "    SELECT organisation_id, name FROM agents\n"
            "    GROUP BY organisation_id, name HAVING count(*) > 1\n"
            "  ) d;\n"
            "  IF dup_count > 0 THEN\n"
            "    RAISE EXCEPTION 'uq_agents_organisation_name: % duplicate (organisation_id, name) pairs exist; de-duplicate before adding the unique constraint', dup_count;\n"
            "  END IF;\n"
            "END $$;"
        )
    )

    # Add the named UNIQUE constraint directly.  CONCURRENTLY is unavailable here
    # because env.py wraps the migration in a single transaction; PostgreSQL names
    # the backing index after the constraint (uq_agents_organisation_name), matching
    # the ORM UniqueConstraint.  Runs inside the migration transaction (brief
    # ACCESS EXCLUSIVE lock + table scan); the duplicate pre-check above fails loud
    # if any legacy (organisation_id, name) duplicates exist.
    #
    # NOTE: PostgreSQL does NOT support ``ADD CONSTRAINT IF NOT EXISTS`` (that clause
    # only exists for ``CREATE INDEX`` / ``CREATE TABLE`` and ``DROP CONSTRAINT IF
    # EXISTS``).  Alembic applies each revision exactly once, so the guard is
    # unnecessary as well as invalid syntax.
    op.execute(text(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT_NAME} UNIQUE ({', '.join(_COLUMNS)})"))


def downgrade() -> None:
    op.execute(text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"))
