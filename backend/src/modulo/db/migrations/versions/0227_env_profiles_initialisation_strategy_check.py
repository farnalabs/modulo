"""CHECK constraint for environment_profiles.initialisation_strategy.

Revision ID: 0227_env_profiles_initialisation_strategy_check
Revises: 0226_agents_json_to_jsonb
Create Date: 2026-09-13

Adds a database-level CHECK constraint enforcing the allowed vocabulary for
``environment_profiles.initialisation_strategy``.  The constraint is added
with ``NOT VALID`` on PostgreSQL so existing rows are not scanned during DDL
(the validation pass is a separate step), and via Alembic batch mode on SQLite.

Allowed vocabulary (FAR-802, ADR 033):

* ``git_clone``       — clone the repo into the workspace (legacy default).
* ``blank``           — start with an empty workspace.
* ``worktree``        — use a git worktree for isolation.
* ``managed_inputs``  — ADR 033 Managed Workspace Inputs opt-in.

Every value currently present in the frontend form dropdown, seed data, and
raw-SQL migrations is included.  ``managed_inputs`` is the ADR 033 MWI
opt-in declared in the ticket but not yet wired in application code; it is
included proactively to avoid a migration when that wiring lands.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0227_env_profiles_initialisation_strategy_check"
down_revision: str | None = "0226_agents_json_to_jsonb"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT_NAME = "ck_env_profiles_initialisation_strategy"
_TABLE = "environment_profiles"
# Sorted for determinism — matches the model's _check_sql helper pattern.
_VOCABULARY = ("blank", "git_clone", "managed_inputs", "worktree")
_CHECK_EXPR = "initialisation_strategy IN ({})".format(", ".join(f"'{v}'" for v in _VOCABULARY))


def upgrade() -> None:
    # --- PostgreSQL path ---------------------------------------------------
    # When running against a real Postgres (not batch_alter_table mode), add
    # the constraint with NOT VALID so existing rows are not scanned.  Then
    # validate in a separate step (the Alembic + op.execute path).
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Idempotent: skip if the constraint already exists (partial re-run
        # safety, same pattern as 0219).
        already = bind.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :name AND conrelid = :table::regclass"),
            {"name": _CONSTRAINT_NAME, "table": _TABLE},
        ).scalar_one_or_none()
        if already is None:
            op.execute(
                text(f'ALTER TABLE public."{_TABLE}" ADD CONSTRAINT {_CONSTRAINT_NAME} CHECK ({_CHECK_EXPR}) NOT VALID')
            )
            op.execute(text(f'ALTER TABLE public."{_TABLE}" VALIDATE CONSTRAINT {_CONSTRAINT_NAME}'))
    else:
        # SQLite / other dialects — batch mode handles it.
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_check_constraint(
                constraint_name=_CONSTRAINT_NAME,
                condition=text(_CHECK_EXPR),
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(text(f'ALTER TABLE public."{_TABLE}" DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}'))
    else:
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_constraint(
                constraint_name=_CONSTRAINT_NAME,
                type_="check",
            )
