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

Deploy-safety (PostgreSQL 16): a plain ``ALTER TABLE ... ADD CONSTRAINT ... UNIQUE``
takes an ``ACCESS EXCLUSIVE`` lock and a full table scan, and hard-fails if any
duplicate ``(organisation_id, name)`` rows already exist — a deploy-blocking failure
mode.  Alembic wraps every revision in a single transaction
(``Will assume transactional DDL``), so ``CREATE UNIQUE INDEX CONCURRENTLY`` is
unavailable here (``CONCURRENTLY`` cannot run inside a transaction, and the
AUTOCOMMIT isolation-level switch fails with ``This connection has already
initialized a SQLAlchemy Transaction()``) — consistent with the pattern used for
unique indexes in ``0197_runs_index_and_constraint_fixes`` / ``0154_`` /
``0171_`` / ``0182_`` / ``0187_`` / ``0193_`` / ``0200_`` / ``0218_``.  We therefore
build the unique index in-transaction with a plain ``CREATE UNIQUE INDEX``, then
promote it to a named ``UNIQUE`` constraint with a brief, non-scanning ``ALTER
TABLE ... ADD CONSTRAINT ... UNIQUE USING INDEX``.  A duplicate pre-check raises a
loud, actionable error (rather than a bare unique-violation) if legacy duplicate
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

    # Build the unique index IN-TRANSACTION (plain CREATE UNIQUE INDEX — CONCURRENTLY
    # is unavailable inside Alembic's transactional DDL). Then promote that index to
    # a named UNIQUE constraint matching the ORM UniqueConstraint name.
    op.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {_CONSTRAINT_NAME} ON {_TABLE} ({', '.join(_COLUMNS)})"))
    op.execute(text(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT_NAME} UNIQUE USING INDEX {_CONSTRAINT_NAME}"))


def downgrade() -> None:
    op.execute(text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"))
