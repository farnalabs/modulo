"""Add CHECK constraints for non-negative counters and enum vocabularies.

Revision ID: 0165_add_check_constraints
Revises: 0164_add_missing_foreign_keys
Create Date: 2026-08-29

Several columns are counters / bounded enums that should be enforced at the DB
level but currently are not:

* Non-negative counters (``>= 0``) — drift or a bug could write a negative
  count, which would corrupt rate-limit / billing math.
* ``error_notification_rule`` thresholds — ``condition_min_count`` and
  ``condition_window_seconds`` must be strictly positive; ``cooldown_seconds``
  must be ``>= 0``.
* ``eval_suite_run`` case tallies must be non-negative and the passed/failed
  tallies cannot exceed ``total_cases``.
* ``run_daily_facts`` ``trigger_type`` / ``status`` mirror the existing
  ``ck_runs_*`` vocabularies so the analytics fact table cannot hold a value
  the runs table rejects. ``run_daily_facts.status`` uses the *full* run-status
  vocabulary (including ``unknown`` and ``compensation_failed``) so historical
  fact rows are never rejected by the VALIDATE pass.

Nullable columns are safe: a NULL row satisfies every CHECK below.

Note: ``organisations.org_cumulative_spend_cents >= 0`` (0151),
``library_primitives.average_rating`` BETWEEN 1 AND 5 (0109) and
``connector_profiles.response_max_bytes > 0`` (0153) are already enforced by
those migrations, so they are intentionally NOT re-added here to avoid duplicate
/ conflicting constraint definitions.

Deploy-safety: every CHECK is added ``NOT VALID`` then ``VALIDATE``-d, mirroring
``0151_fix_constraints``. ``ADD CONSTRAINT ... CHECK (...) NOT VALID`` takes only
a brief ``AccessExclusive`` lock and does NOT scan existing rows, so a populated
table never aborts the upgrade if a historical row violates the constraint (the
exact bug class this PR prevents could otherwise already exist in prod data);
the ``VALIDATE`` is online. The app only ever writes in-set values, so VALIDATE
succeeds in practice.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0165_add_check_constraints"
down_revision: str | None = "0164_add_missing_foreign_keys"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_RUN_TRIGGER_TYPE = (
    "trigger_type IN ('manual','webhook','cron','polling','agent_signal','ongoing','correction','slack_app_mention')"
)
_RUN_STATUS = (
    "status IN ('pending','running','awaiting_human','claimed','unknown','complete',"
    "'failed','cancelled','eval_failed','stalled','budget_exceeded',"
    "'router_no_match','compensation_failed','cost_ceiling_exceeded')"
)


# (table, constraint_name, check_expression) — added NOT VALID then VALIDATE-d.
_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("runs", "ck_run_claim_count", "claim_count >= 0"),
    ("runs", "ck_run_node_attempt_count", "node_attempt_count >= 0"),
    ("notification_delivery_log", "ck_notification_delivery_attempt_count", "attempt_count >= 0"),
    ("notification_endpoints", "ck_notification_endpoint_dead_letter", "consecutive_dead_letter_count >= 0"),
    ("library_primitives", "ck_library_primitive_download_count", "download_count >= 0"),
    ("library_primitives", "ck_library_primitive_review_count", "review_count >= 0"),
    ("error_notification_rules", "ck_error_notification_rule_min_count", "condition_min_count > 0"),
    ("error_notification_rules", "ck_error_notification_rule_window", "condition_window_seconds > 0"),
    ("error_notification_rules", "ck_error_notification_rule_cooldown", "cooldown_seconds >= 0"),
    ("error_groups", "ck_error_group_count", "count >= 0"),
    ("org_daily_run_counts", "ck_daily_run_count_run_count", "run_count >= 0"),
    ("audit_chain_heads", "ck_audit_event_event_count", "event_count >= 0"),
    ("journeys", "ck_journey_run_count", "run_count >= 0"),
    ("cost_components", "ck_cost_component_sort_order", "sort_order >= 0"),
    ("suite_runs", "ck_eval_suite_run_total_cases", "total_cases >= 0"),
    ("suite_runs", "ck_eval_suite_run_passed_cases", "passed_cases >= 0"),
    ("suite_runs", "ck_eval_suite_run_failed_cases", "failed_cases >= 0"),
    ("suite_runs", "ck_eval_suite_run_excluded_cases", "excluded_case_count >= 0"),
    ("suite_runs", "ck_eval_suite_run_passed_le_total", "passed_cases <= total_cases"),
    ("suite_runs", "ck_eval_suite_run_failed_le_total", "failed_cases <= total_cases"),
    ("run_daily_facts", "ck_run_daily_facts_trigger_type", _RUN_TRIGGER_TYPE),
    ("run_daily_facts", "ck_run_daily_facts_status", _RUN_STATUS),
)


def upgrade() -> None:
    # CHECK constraints: add NOT VALID (no row scan at ADD time) then VALIDATE
    # online, so a populated table never aborts the upgrade.
    for table, name, expr in _CHECKS:
        op.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID"))
        op.execute(text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))


def downgrade() -> None:
    op.drop_constraint("ck_run_daily_facts_status", "run_daily_facts", type_="check")
    op.drop_constraint("ck_run_daily_facts_trigger_type", "run_daily_facts", type_="check")
    op.drop_constraint("ck_eval_suite_run_failed_le_total", "suite_runs", type_="check")
    op.drop_constraint("ck_eval_suite_run_passed_le_total", "suite_runs", type_="check")
    op.drop_constraint("ck_eval_suite_run_excluded_cases", "suite_runs", type_="check")
    op.drop_constraint("ck_eval_suite_run_failed_cases", "suite_runs", type_="check")
    op.drop_constraint("ck_eval_suite_run_passed_cases", "suite_runs", type_="check")
    op.drop_constraint("ck_eval_suite_run_total_cases", "suite_runs", type_="check")
    op.drop_constraint("ck_cost_component_sort_order", "cost_components", type_="check")
    op.drop_constraint("ck_journey_run_count", "journeys", type_="check")
    op.drop_constraint("ck_audit_event_event_count", "audit_chain_heads", type_="check")
    op.drop_constraint("ck_daily_run_count_run_count", "org_daily_run_counts", type_="check")
    op.drop_constraint("ck_error_group_count", "error_groups", type_="check")
    op.drop_constraint(
        "ck_error_notification_rule_cooldown",
        "error_notification_rules",
        type_="check",
    )
    op.drop_constraint(
        "ck_error_notification_rule_window",
        "error_notification_rules",
        type_="check",
    )
    op.drop_constraint(
        "ck_error_notification_rule_min_count",
        "error_notification_rules",
        type_="check",
    )
    op.drop_constraint("ck_library_primitive_review_count", "library_primitives", type_="check")
    op.drop_constraint("ck_library_primitive_download_count", "library_primitives", type_="check")
    op.drop_constraint(
        "ck_notification_endpoint_dead_letter",
        "notification_endpoints",
        type_="check",
    )
    op.drop_constraint(
        "ck_notification_delivery_attempt_count",
        "notification_delivery_log",
        type_="check",
    )
    op.drop_constraint("ck_run_node_attempt_count", "runs", type_="check")
    op.drop_constraint("ck_run_claim_count", "runs", type_="check")
