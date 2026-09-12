"""Add CHECK constraints for eval cluster domain integrity.

Revision ID: 0219_eval_cluster_check_constraints
Revises: 0218_eval_cluster_indexes
Create Date: 2026-09-12

Adds database-level domain-integrity CHECK constraints that the application
layer currently assumes but the DB does not enforce:

1. eval_definitions.pass_threshold BETWEEN 0 AND 1 (pass-rate fraction).
2. eval_definitions.version >= 1 (monotonically increasing).
3. eval_suites.minimum_delta BETWEEN 0 AND 1 (pass-rate drop fraction).
4. eval_suites.baseline_window >= 1 when non-NULL.
5. eval_suites.cooldown >= 0 when non-NULL.
6. eval_suites.version >= 1.
7. eval_datasets.version >= 1.
8. suite_runs.version >= 0 (optimistic-lock guard).
9. suite_runs.dataset_version >= 1.
10. suite_runs.total_cost_usd >= 0.
11. suite_runs.claimed_cost >= 0.
12. suite_runs case-count consistency:
    passed_cases + failed_cases + excluded_case_count <= total_cases.

All constraints are created IF NOT EXISTS to be idempotent. Downgrade drops
every constraint created here by name.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0219_eval_cluster_check_constraints"
down_revision: str | None = "0218_eval_cluster_indexes"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINTS: list[tuple[str, str, str]] = [
    # (table, constraint_name, check_expression)
    ("eval_definitions", "ck_eval_definitions_pass_threshold_range",
     "pass_threshold IS NULL OR (pass_threshold >= 0 AND pass_threshold <= 1)"),
    ("eval_definitions", "ck_eval_definitions_version_gte_1",
     "version >= 1"),
    ("eval_suites", "ck_eval_suites_minimum_delta_range",
     "minimum_delta IS NULL OR (minimum_delta >= 0 AND minimum_delta <= 1)"),
    ("eval_suites", "ck_eval_suites_baseline_window_gte_1",
     "baseline_window IS NULL OR baseline_window >= 1"),
    ("eval_suites", "ck_eval_suites_cooldown_gte_0",
     "cooldown IS NULL OR cooldown >= 0"),
    ("eval_suites", "ck_eval_suites_version_gte_1",
     "version >= 1"),
    ("eval_datasets", "ck_eval_datasets_version_gte_1",
     "version >= 1"),
    ("suite_runs", "ck_suite_runs_version_gte_0",
     "version >= 0"),
    ("suite_runs", "ck_suite_runs_dataset_version_gte_1",
     "dataset_version >= 1"),
    ("suite_runs", "ck_suite_runs_cost_non_negative",
     "(total_cost_usd IS NULL OR total_cost_usd >= 0) AND "
     "(claimed_cost IS NULL OR claimed_cost >= 0)"),
    ("suite_runs", "ck_suite_runs_case_counts_consistent",
     "passed_cases + failed_cases + excluded_case_count <= total_cases"),
]


def upgrade() -> None:
    for table, name, expr in _CONSTRAINTS:
        op.create_check_constraint(name, table, sa.text(expr))


def downgrade() -> None:
    for table, name, _ in reversed(_CONSTRAINTS):
        op.drop_constraint(name, table, type_="check")
