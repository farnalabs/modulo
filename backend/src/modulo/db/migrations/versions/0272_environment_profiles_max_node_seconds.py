"""Environment profiles: provider wall-clock capability column (FAR-1359).

Revision ID: 0272_environment_profiles_max_node_seconds
Revises: 0271_org_api_keys_revocation_sweep_indexes
Create Date: 2026-09-30

One additive column on ``environment_profiles``:

``max_node_seconds`` — the PROVIDER's per-node wall-clock capability, in
seconds. The GraphValidator used to reject a sandbox node's
``timeout_seconds`` above a hardcoded 3300 (E2B's 1-hour platform cap plus
provisioning headroom) — an assumption that E2B's limit is universal. The value
now lives on the profile so a provider that can host long-running agents
declares its own capability, while an E2B customer still sees the same 1-hour
limit, now explained as the provider's limit rather than the product's.

NOT NULL with ``server_default 3300`` so every existing row keeps exactly the
pre-FAR-1359 behaviour and the add is a metadata-only operation on Postgres 11+
(no table rewrite): the literal default backfills the existing rows at the same
moment the constant stops being hardcoded in the validator. The default stays on
the column permanently (new rows written by a writer that omits the field still
get the shipped behaviour), matching ``persistence_policy`` / ``status``.

The CHECK (``ck_env_profiles_max_node_seconds``) mirrors the model's
``CheckConstraint``: 60..604800, the same envelope the API Pydantic fields
enforce, so an out-of-envelope value is impossible at the storage layer.
Postgres adds it NOT VALID then VALIDATEs (lock-safety pattern from
0176/0255/0256); SQLite has no ALTER TABLE ADD CONSTRAINT, so it goes through
batch mode (the 0262 pattern). Columns are existence-gated (``IF NOT EXISTS``)
so re-running is a no-op.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0272_environment_profiles_max_node_seconds"
down_revision: str | None = "0271_org_api_keys_revocation_sweep_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "environment_profiles"
_COLUMN = "max_node_seconds"
_CHECK = "ck_env_profiles_max_node_seconds"

# Mirrors DEFAULT_MAX_NODE_SECONDS / MIN_MAX_NODE_SECONDS in
# ``modulo.db.models.environment_profile`` — the shipped E2B headroom default
# and the model CHECK's envelope. Kept literal here: this directory's
# migrations use literal DDL (the S608 f-string-SQL rule applies to it).
_DEFAULT = 3300
_MIN = 60
_MAX = 604800


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


_ADD_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_env_profiles_max_node_seconds' "
    "AND conrelid = 'public.environment_profiles'::regclass) "
    "THEN ALTER TABLE public.environment_profiles ADD CONSTRAINT "
    "ck_env_profiles_max_node_seconds CHECK ("
    "max_node_seconds BETWEEN 60 AND 604800"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_env_profiles_max_node_seconds' "
    "AND conrelid = 'public.environment_profiles'::regclass AND NOT convalidated) "
    "THEN ALTER TABLE public.environment_profiles VALIDATE CONSTRAINT "
    "ck_env_profiles_max_node_seconds; END IF; END $$;"
)
_DROP_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_env_profiles_max_node_seconds' "
    "AND conrelid = 'public.environment_profiles'::regclass) "
    "THEN ALTER TABLE public.environment_profiles DROP CONSTRAINT "
    "ck_env_profiles_max_node_seconds; END IF; END $$;"
)
_ADD_COLUMN = (
    "ALTER TABLE public.environment_profiles ADD COLUMN IF NOT EXISTS max_node_seconds integer NOT NULL DEFAULT 3300;"
)
_DROP_COLUMN = "ALTER TABLE public.environment_profiles DROP COLUMN IF EXISTS max_node_seconds;"


def upgrade() -> None:
    # --- Column (existence-gated, both dialects) ---
    if _is_postgres():
        op.execute(_ADD_COLUMN)
    elif _is_sqlite():
        # SQLite has no ADD COLUMN IF NOT EXISTS — batch mode recreates the
        # table from the reflected schema (the 0262 pattern).
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.add_column(sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default=str(_DEFAULT)))

    # --- CHECK constraint (same envelope as the model's CheckConstraint) ---
    if _is_postgres():
        op.execute(_ADD_CHECK_NOT_VALID)
        op.execute(_VALIDATE_CHECK)
    elif _is_sqlite():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_check_constraint(
                _CHECK,
                f"max_node_seconds BETWEEN {_MIN} AND {_MAX}",
            )


def downgrade() -> None:
    if _is_postgres():
        op.execute(_DROP_CHECK)
        op.execute(_DROP_COLUMN)
    elif _is_sqlite():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_constraint(_CHECK, type_="check")
            batch_op.drop_column(_COLUMN)
