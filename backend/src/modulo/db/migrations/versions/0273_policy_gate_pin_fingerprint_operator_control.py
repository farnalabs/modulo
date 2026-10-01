"""FAR-967 chunk 10: policy-gate pin fingerprint + operator enabled/disabled control.

Revision ID: 0273_policy_gate_pin_fingerprint_operator_control
Revises: 0272_oauth_client_revoke_lookup_indexes
Create Date: 2026-09-30

Two additive schema changes shipped in one migration (§6.2):

1. ``policy_gates`` — operator safety control (§4.3):
   - ``enabled BOOLEAN NOT NULL DEFAULT true``
   - ``enabled_at TIMESTAMPTZ``
   - ``disabled_at TIMESTAMPTZ``
   - Symmetric CHECK ``ck_policy_gates_enabled_timestamps`` (§4.3):
     enabled ⟹ enabled_at NOT NULL AND disabled_at NULL;
     NOT enabled ⟹ disabled_at NOT NULL AND enabled_at NULL.

   Backfill: existing rows get ``enabled_at = created_at`` (all are enabled
   by default). The CHECK is added AFTER the backfill so existing rows
   satisfy the constraint.

2. ``pipeline_snapshots`` — pin-set integrity fingerprint (§3.3):
   - ``policy_gate_pins_json JSON`` (nullable, legacy compat)
   - ``policy_gate_pins_fingerprint VARCHAR(64)`` (nullable, legacy compat)

Dialect-guarded: CHECK added NOT VALID then VALIDATED on Postgres (0265
pattern); SQLite uses batch mode. Columns are existence-gated (IF NOT EXISTS).
Downgrade removes all added columns and the CHECK constraint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0273_policy_gate_pin_fingerprint_operator_control"
down_revision: str | None = "0272_oauth_client_revoke_lookup_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


# Existence-gated CHECK constraint DDL (§4.3) — Postgres only.
# The symmetric CHECK ensures: enabled ⟹ enabled_at NOT NULL AND disabled_at NULL,
# and NOT enabled ⟹ disabled_at NOT NULL AND enabled_at NULL.
_ADD_ENABLED_CHECK_NOT_VALID = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_policy_gates_enabled_timestamps' "
    "AND conrelid = 'public.policy_gates'::regclass) "
    "THEN ALTER TABLE public.policy_gates ADD CONSTRAINT "
    "ck_policy_gates_enabled_timestamps CHECK ("
    "(enabled AND enabled_at IS NOT NULL AND disabled_at IS NULL) "
    "OR "
    "(NOT enabled AND disabled_at IS NOT NULL AND enabled_at IS NULL)"
    ") NOT VALID; END IF; END $$;"
)
_VALIDATE_ENABLED_CHECK = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_policy_gates_enabled_timestamps' "
    "AND conrelid = 'public.policy_gates'::regclass "
    "AND NOT convalidated) "
    "THEN ALTER TABLE public.policy_gates "
    "VALIDATE CONSTRAINT ck_policy_gates_enabled_timestamps; END IF; END $$;"
)


def upgrade() -> None:
    # ── policy_gates: operator safety control (§4.3) ──────────────────────
    # 1. Add columns (existence-gated for idempotency). ``enabled_at`` gets a
    #    server-side ``DEFAULT now()`` so a ROLLING deploy is safe: OLD
    #    containers still INSERT policy_gates rows through the pre-0272 model,
    #    which omits ``enabled_at`` entirely. Without the server default that
    #    insert lands NULL and ck_policy_gates_enabled_timestamps rejects it
    #    (gate create/replace 500s until the rollout completes). NOTE: the
    #    default is attached AFTER the backfill below — ADD COLUMN with an
    #    inline DEFAULT fills existing rows with the ALTER-time stamp, which
    #    would defeat the created_at backfill; ALTER COLUMN SET DEFAULT only
    #    affects future inserts, preserving both guarantees.
    op.execute(sa.text("ALTER TABLE policy_gates ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT true"))
    op.execute(sa.text("ALTER TABLE policy_gates ADD COLUMN IF NOT EXISTS enabled_at TIMESTAMPTZ"))
    op.execute(sa.text("ALTER TABLE policy_gates ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ"))

    # 2. Backfill existing rows: all are enabled (DEFAULT true), so set
    #    enabled_at = created_at to satisfy the CHECK constraint.
    op.execute(sa.text("UPDATE policy_gates SET enabled_at = created_at WHERE enabled_at IS NULL"))

    # 3. Rolling-deploy safety: future inserts (incl. OLD containers that
    #    omit enabled_at) default to now(), satisfying the symmetric CHECK.
    if _is_postgres():
        op.execute(sa.text("ALTER TABLE policy_gates ALTER COLUMN enabled_at SET DEFAULT now()"))

    # 4. CHECK constraint — Postgres: NOT VALID + VALIDATE; SQLite: batch.
    if _is_postgres():
        op.execute(sa.text(_ADD_ENABLED_CHECK_NOT_VALID))
        op.execute(sa.text(_VALIDATE_ENABLED_CHECK))
    elif _is_sqlite():
        with op.batch_alter_table("policy_gates") as batch_op:
            batch_op.create_check_constraint(
                "ck_policy_gates_enabled_timestamps",
                "(enabled AND enabled_at IS NOT NULL AND disabled_at IS NULL) "
                "OR "
                "(NOT enabled AND disabled_at IS NOT NULL AND enabled_at IS NULL)",
            )

    # ── pipeline_snapshots: pin-set integrity fingerprint (§3.3) ──────────
    op.execute(sa.text("ALTER TABLE pipeline_snapshots ADD COLUMN IF NOT EXISTS policy_gate_pins_json JSON"))
    op.execute(
        sa.text("ALTER TABLE pipeline_snapshots ADD COLUMN IF NOT EXISTS policy_gate_pins_fingerprint VARCHAR(64)")
    )


def downgrade() -> None:
    # ── pipeline_snapshots: remove fingerprint columns ─────────────────────
    if _is_sqlite():
        with op.batch_alter_table("pipeline_snapshots") as batch_op:
            batch_op.drop_column("policy_gate_pins_fingerprint")
            batch_op.drop_column("policy_gate_pins_json")
    else:
        op.execute(sa.text("ALTER TABLE pipeline_snapshots DROP COLUMN IF EXISTS policy_gate_pins_fingerprint"))
        op.execute(sa.text("ALTER TABLE pipeline_snapshots DROP COLUMN IF EXISTS policy_gate_pins_json"))

    # ── policy_gates: remove CHECK + columns ───────────────────────────────
    if _is_postgres():
        op.execute(
            sa.text(
                "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conname='ck_policy_gates_enabled_timestamps' "
                "AND conrelid = 'public.policy_gates'::regclass) "
                "THEN ALTER TABLE public.policy_gates "
                "DROP CONSTRAINT ck_policy_gates_enabled_timestamps; END IF; END $$;"
            )
        )
    elif _is_sqlite():
        with op.batch_alter_table("policy_gates") as batch_op:
            batch_op.drop_constraint("ck_policy_gates_enabled_timestamps", type_="check")

    if _is_sqlite():
        with op.batch_alter_table("policy_gates") as batch_op:
            batch_op.drop_column("disabled_at")
            batch_op.drop_column("enabled_at")
            batch_op.drop_column("enabled")
    else:
        op.execute(sa.text("ALTER TABLE policy_gates DROP COLUMN IF EXISTS disabled_at"))
        op.execute(sa.text("ALTER TABLE policy_gates DROP COLUMN IF EXISTS enabled_at"))
        op.execute(sa.text("ALTER TABLE policy_gates DROP COLUMN IF EXISTS enabled"))
