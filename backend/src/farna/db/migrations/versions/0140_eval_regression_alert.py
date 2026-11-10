"""Eval regression alerting config on ``eval_suites`` (FAR-379).

Revision ID: 0140_eval_regression_alert
Revises: 0138_eval_versioning
Create Date: 2026-08-25

The SuiteRun comparison (FAR-376 Phase 3) already *detects* a pass-rate
regression and records it on the run (``regressed``, ``comparison_json``,
``notified_at``). This migration adds the per-suite **alerting configuration**
the Alerting layer (FAR-379) reads: a rolling baseline window, the minimum
pass-rate drop that qualifies as an alert, and the cooldown (silence window) so
a single sustained regression does not page on every run.

What this migration does (additive, reversible, non-breaking):

* ``eval_suites.baseline_window`` — rolling N-run baseline window (nullable,
  no server default; NULL = alerting disabled / requires an explicit baseline).
* ``eval_suites.minimum_delta`` — pass-rate drop threshold as a fraction 0..1
  (``Numeric(8,4)``, nullable; NULL = defer to the Phase 3 ``regressed`` flag).
* ``eval_suites.cooldown`` — silence window in minutes (nullable; NULL = no
  time-based rate limit, idempotency on ``suite_run_id`` still applies).

All three are NULL by default so the feature stays additive: an existing
``eval_suites`` row is unchanged until an admin configures alerting.

OWNERSHIP (the 0137 ceremony): ``eval_suites`` is owned by ``modulo_migrate``
(the 0130 migration transfers ownership there so the runtime ``modulo_app``
role is a non-owner filtered by RLS). ``ADD COLUMN`` requires table ownership,
so the column adds run under ``SET ROLE modulo_migrate`` — the migration
connection role (``DATABASE_ADMIN_URL``) is a role that can SET ROLE to
``modulo_migrate``, which is NOLOGIN. The ceremony is guarded on the role
existing (fresh dev/BDD DBs have none). On non-Postgres backends the role
guard is skipped and the adds run directly.

Reversible: downgrade drops the three columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0140_eval_regression_alert"
down_revision: str | None = "0138_eval_versioning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _role_exists(bind: sa.Connection, role: str) -> bool:
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)
    migrate_role = bool(pg and _role_exists(bind, _MIGRATE_ROLE))

    if pg and migrate_role:
        op.execute("SET search_path TO public")
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    # Column adds are owner-gated; under ``SET ROLE modulo_migrate`` they land
    # on an eval_suites table that role owns in every environment.
    op.add_column("eval_suites", sa.Column("baseline_window", sa.Integer(), nullable=True))
    op.add_column("eval_suites", sa.Column("minimum_delta", sa.Numeric(precision=8, scale=4), nullable=True))
    op.add_column("eval_suites", sa.Column("cooldown", sa.Integer(), nullable=True))

    if pg and migrate_role:
        op.execute("RESET ROLE")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)
    migrate_role = bool(pg and _role_exists(bind, _MIGRATE_ROLE))

    if pg and migrate_role:
        op.execute("SET search_path TO public")
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    op.drop_column("eval_suites", "cooldown")
    op.drop_column("eval_suites", "minimum_delta")
    op.drop_column("eval_suites", "baseline_window")

    if pg and migrate_role:
        op.execute("RESET ROLE")
