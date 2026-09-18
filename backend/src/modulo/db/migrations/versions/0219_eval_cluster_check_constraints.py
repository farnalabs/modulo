"""Add CHECK constraints for eval cluster domain integrity.

Revision ID: 0219_eval_cluster_check_constraints
Revises: 0218_eval_cluster_indexes
Create Date: 2026-09-12

Adds database-level domain-integrity CHECK constraints that the application
layer currently assumes but the DB does not enforce. These cover ONLY
constraints not already enforced by an earlier migration:

* ``0157_add_numeric_check_constraints`` already enforces
  ``pass_threshold BETWEEN 0 AND 1`` (``ck_eval_definitions_pass_threshold``),
  ``minimum_delta BETWEEN 0 AND 1`` (``ck_eval_suites_minimum_delta``), and
  ``claimed_cost >= 0`` (``ck_eval_suite_runs_claimed_cost``). Those rules are
  deliberately NOT re-added here — re-adding them under new names would make
  the same columns pay for redundant checks on every INSERT/UPDATE.

The constraints added here are the remaining gaps:

1. eval_definitions.version >= 1 (monotonically increasing).
2. eval_suites.baseline_window >= 1 when non-NULL.
3. eval_suites.cooldown >= 0 when non-NULL.
4. eval_suites.version >= 1.
5. eval_datasets.version >= 1.
6. suite_runs.version >= 0 (optimistic-lock guard).
7. suite_runs.dataset_version >= 1.
8. suite_runs.total_cost_usd >= 0 (NULL ok). ``claimed_cost`` is covered by
   0157, so it is intentionally excluded here.
9. suite_runs case-count consistency:
   passed_cases + failed_cases + excluded_case_count <= total_cases.

Idempotency: each ADD CONSTRAINT is guarded by a ``pg_constraint`` existence
check (the same property ``0157`` / ``0153`` / ``0110`` get from their
DO-block existence guards). A partially-applied run — one constraint
added, a later one rejected by pre-existing bad data — can be re-run without
failing on the constraints that already exist. ``op.create_check_constraint``
emits a bare ``ALTER TABLE ... ADD CONSTRAINT`` with no existence guard, so we
use the explicit ``pg_constraint`` guard instead. Downgrade drops every
constraint by name with ``DROP CONSTRAINT IF EXISTS``.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0219_eval_cluster_check_constraints"
down_revision: str | None = "0218_eval_cluster_indexes"
branch_labels: str | None = None
depends_on: str | None = None

# (constraint_name, table, check_expression)
# Only constraints NOT already enforced by 0157_add_numeric_check_constraints.
_CONSTRAINTS: list[tuple[str, str, str]] = [
    ("ck_eval_definitions_version_gte_1", "eval_definitions", "version >= 1"),
    (
        "ck_eval_suites_baseline_window_gte_1",
        "eval_suites",
        "baseline_window IS NULL OR baseline_window >= 1",
    ),
    ("ck_eval_suites_cooldown_gte_0", "eval_suites", "cooldown IS NULL OR cooldown >= 0"),
    ("ck_eval_suites_version_gte_1", "eval_suites", "version >= 1"),
    ("ck_eval_datasets_version_gte_1", "eval_datasets", "version >= 1"),
    ("ck_suite_runs_version_gte_0", "suite_runs", "version >= 0"),
    ("ck_suite_runs_dataset_version_gte_1", "suite_runs", "dataset_version >= 1"),
    (
        "ck_suite_runs_total_cost_non_negative",
        "suite_runs",
        "total_cost_usd IS NULL OR total_cost_usd >= 0",
    ),
    (
        "ck_suite_runs_case_counts_consistent",
        "suite_runs",
        "passed_cases + failed_cases + excluded_case_count <= total_cases",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    for name, table, expr in _CONSTRAINTS:
        # Idempotent like 0157's pg_constraint guard: a partial re-run (an
        # earlier constraint added, a later one rejected by pre-existing bad
        # data) must not then fail with "constraint already exists".
        already_present = bind.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        ).scalar_one_or_none()
        if already_present is not None:
            continue
        bind.execute(text(f'ALTER TABLE public."{table}" ADD CONSTRAINT {name} CHECK ({expr});'))


def downgrade() -> None:
    bind = op.get_bind()
    for name, table, _ in reversed(_CONSTRAINTS):
        bind.execute(text(f'ALTER TABLE public."{table}" DROP CONSTRAINT IF EXISTS {name};'))
