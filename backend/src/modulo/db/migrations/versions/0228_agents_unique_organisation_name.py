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

Deploy-safety (PostgreSQL 16): a plain ``op.create_unique_constraint`` / ``ALTER
TABLE ... ADD CONSTRAINT ... UNIQUE`` takes an ``ACCESS EXCLUSIVE`` lock and a full
table scan, and hard-fails if any duplicate ``(organisation_id, name)`` rows
already exist — a deploy-blocking failure mode.  Instead we build the unique
index ONLINE with ``CREATE UNIQUE INDEX CONCURRENTLY`` (issued under AUTOCOMMIT
isolation because ``CONCURRENTLY`` cannot run inside a transaction) so it takes
only a ``SHARE UPDATE EXCLUSIVE`` lock and never blocks writers, then promote that
index to a named ``UNIQUE`` constraint with a brief, non-scanning ``ALTER TABLE ...
ADD CONSTRAINT ... UNIQUE USING INDEX``.  This is the PostgreSQL-16 equivalent of
the ``NOT VALID`` + ``VALIDATE`` pattern used for CHECK/FK constraints in
``0151_fix_constraints`` / ``0164_add_missing_foreign_keys`` — unique constraints
cannot be marked ``NOT VALID`` before PostgreSQL 18.  A duplicate pre-check raises
a loud, actionable error (rather than a bare unique-violation) if legacy duplicate
rows are present.
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

    # Build the unique index ONLINE (CONCURRENTLY) — AUTOCOMMIT isolation is
    # required because CONCURRENTLY cannot run inside the migration transaction.
    op.execute(
        text(f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_CONSTRAINT_NAME} ON {_TABLE} ({', '.join(_COLUMNS)})"),
        execution_options={"isolation_level": "AUTOCOMMIT"},
    )
    # Promote the online-built index to a named UNIQUE constraint matching the
    # ORM UniqueConstraint name (brief, non-scanning ALTER).
    op.execute(text(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT_NAME} UNIQUE USING INDEX {_CONSTRAINT_NAME}"))


def downgrade() -> None:
    op.execute(text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"))
