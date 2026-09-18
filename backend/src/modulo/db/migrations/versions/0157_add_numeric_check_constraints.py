"""Add numeric-range and XOR CHECK constraints (improve-database lenses).

Revision ID: 0157_add_numeric_check_constraints
Revises: 0156_add_soft_delete_partial_uniques
Create Date: 2026-08-30

Adds defensive DB-level CHECK constraints for values the application already
assumes but never enforced:

* ``pass_threshold`` / ``minimum_delta`` are fractions in [0, 1].
* ``variant_groups.run_count`` >= 0, ``max_concurrent_runs`` > 0.
* ``team.daily_spend_limit`` and ``suite_runs.claimed_cost`` >= 0 (NULL ok).
* ``spend_anomalies.percent_above`` >= 0.
* ``eval_results`` must reference exactly one of ``run_id`` / ``suite_run_id``.

These mirror the range CHECKs added in 0153 and are validated against the
live table (rows that already violate will make the ALTER fail, surfacing
bad data rather than silently accepting it).

Each ADD CONSTRAINT is guarded by a ``pg_constraint`` existence check (the same
idempotency property 0153 gets from its ``DO $$ IF NOT EXISTS`` blocks), so a
partially-applied run — one constraint added, a later one rejected by
pre-existing bad data — can be re-run without failing on the constraints that
already exist.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0157_add_numeric_check_constraints"
down_revision: str | None = "0156_add_soft_delete_partial_uniques"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_CHECKS = [
    (
        "ck_eval_definitions_pass_threshold",
        "eval_definitions",
        "pass_threshold IS NULL OR pass_threshold BETWEEN 0 AND 1",
    ),
    (
        "ck_eval_suites_minimum_delta",
        "eval_suites",
        "minimum_delta IS NULL OR minimum_delta BETWEEN 0 AND 1",
    ),
    (
        "ck_variant_groups_run_count",
        "variant_groups",
        "run_count >= 0",
    ),
    (
        "ck_variant_groups_max_concurrent_runs",
        "variant_groups",
        "max_concurrent_runs > 0",
    ),
    (
        "ck_teams_daily_spend_limit",
        "teams",
        "daily_spend_limit IS NULL OR daily_spend_limit >= 0",
    ),
    (
        "ck_eval_suite_runs_claimed_cost",
        "suite_runs",
        "claimed_cost IS NULL OR claimed_cost >= 0",
    ),
    (
        "ck_spend_anomalies_percent_above",
        "spend_anomalies",
        "percent_above >= 0",
    ),
    (
        "ck_eval_results_run_xor_suite",
        "eval_results",
        "(run_id IS NULL) <> (suite_run_id IS NULL)",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    for name, table, condition in _CHECKS:
        # Idempotent like 0153's pg_constraint guard: a partial re-run (an earlier
        # constraint added, a later one rejected by pre-existing bad data) must not
        # then fail with "constraint already exists".
        already_present = bind.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        ).scalar_one_or_none()
        if already_present is not None:
            continue
        bind.execute(text(f'ALTER TABLE public."{table}" ADD CONSTRAINT {name} CHECK ({condition});'))


def downgrade() -> None:
    bind = op.get_bind()
    for name, table, _condition in _CHECKS:
        bind.execute(text(f'ALTER TABLE public."{table}" DROP CONSTRAINT IF EXISTS {name};'))
