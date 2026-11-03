"""System Maps improvement-loop admission runtime columns (FAR-2349).

Revision ID: 0348_system_map_loop_admission_runtime
Revises: 0347_system_maps_version_aware
Create Date: 2026-11-03

The tables created by 0341 shipped the idempotency-key shape only. The
admission/dispatch runtime (``core.system_map_loop_admission``) needs the
decision and reservation facts the documented loop contract requires
(``docs/system-maps.md`` §3.3 / §4.4):

``system_map_loop_configs``
    * ``max_concurrent_runs`` — the per-loop concurrency ceiling the
      documented contract names (must be at least 1; schema default 1 is the
      conservative value, not an unbounded one).

``system_map_loop_admission_attempts``
    * ``run_id`` — the admitted run (NULL on a rejected-before-run decision).
    * ``rejection_reason`` — the closed, deterministic vocabulary for a
      rejected decision. A rejection with no reason is a silent no-op and is
      impossible to represent: the outcome CHECK requires one.
    * ``month_start`` — the UTC calendar month the attempt is accounted
      against.

``system_map_loop_reservations``
    * ``admission_attempt_id`` — the attempt that created the reservation.
    * ``month_start``, ``reserved_budget_usd``, ``actual_spend_usd`` — the
      monthly budget accounting the contract requires. ``actual_spend_usd``
      stays NULL when the run's cost was never observed (an unknown cost must
      never read as zero).
    * A supporting ``(organisation_id, loop_config_id, month_start, status)``
      index for the month-scoped budget sum (a query the due-loop sweep runs
      every tick).

Cooldown needs no new column: ``cooldown_until`` already exists and is never
mutated by a release, which is exactly the "cooldown survives cancellation"
guarantee.

All added columns are nullable or carry a server default, and every new CHECK
is ``NOT VALID`` then ``VALIDATEd`` (the 0265/0305 idiom), so the migration
takes no long write lock and is safe on a populated database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0348_system_map_loop_admission_runtime"
down_revision: str | None = "0347_system_maps_version_aware"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE_LOOP_CONFIGS = "system_map_loop_configs"
_TABLE_ATTEMPTS = "system_map_loop_admission_attempts"
_TABLE_RESERVATIONS = "system_map_loop_reservations"

_MONTH_START_DEFAULT = "(date_trunc('month', CURRENT_TIMESTAMP))::date"

#: Closed rejection vocabulary — MUST stay in step with
#: ``core.system_map_loop_admission.REJECTION_REASONS``. Literal strings only:
#: a format-built SQL statement is blocked by the migration-fstring-sql rule.
_REJECTION_REASON_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_loop_admission_attempts_reason') THEN "
    "ALTER TABLE public.system_map_loop_admission_attempts ADD CONSTRAINT "
    "ck_loop_admission_attempts_reason CHECK ("
    "rejection_reason IS NULL OR rejection_reason IN ("
    "'attribution', 'autonomy', 'budget', 'concurrency', 'cooldown', "
    "'inactive_pipeline', 'unattributed', 'unpublished_revision')) NOT VALID; "
    "END IF; END $$;"
)
_ATTEMPT_OUTCOME_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_loop_admission_attempts_outcome') THEN "
    "ALTER TABLE public.system_map_loop_admission_attempts ADD CONSTRAINT "
    "ck_loop_admission_attempts_outcome CHECK ("
    "(status = 'rejected' AND run_id IS NULL AND rejection_reason IS NOT NULL) "
    "OR (status = 'admitted' AND run_id IS NOT NULL AND rejection_reason IS NULL) "
    "OR (status IN ('pending', 'duplicate'))) NOT VALID; "
    "END IF; END $$;"
)
_LOOP_CONCURRENCY_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_loop_configs_max_concurrent_runs') THEN "
    "ALTER TABLE public.system_map_loop_configs ADD CONSTRAINT "
    "ck_loop_configs_max_concurrent_runs CHECK (max_concurrent_runs >= 1) NOT VALID; "
    "END IF; END $$;"
)
_RESERVED_BUDGET_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_loop_reservations_reserved_budget') THEN "
    "ALTER TABLE public.system_map_loop_reservations ADD CONSTRAINT "
    "ck_loop_reservations_reserved_budget CHECK (reserved_budget_usd >= 0) NOT VALID; "
    "END IF; END $$;"
)
_ACTUAL_SPEND_CHECK = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
    "WHERE conname='ck_loop_reservations_actual_spend') THEN "
    "ALTER TABLE public.system_map_loop_reservations ADD CONSTRAINT "
    "ck_loop_reservations_actual_spend CHECK (actual_spend_usd IS NULL OR actual_spend_usd >= 0) NOT VALID; "
    "END IF; END $$;"
)
_VALIDATE_STATEMENTS: tuple[str, ...] = (
    ("ALTER TABLE public.system_map_loop_admission_attempts VALIDATE CONSTRAINT ck_loop_admission_attempts_reason;"),
    ("ALTER TABLE public.system_map_loop_admission_attempts VALIDATE CONSTRAINT ck_loop_admission_attempts_outcome;"),
    "ALTER TABLE public.system_map_loop_configs VALIDATE CONSTRAINT ck_loop_configs_max_concurrent_runs;",
    "ALTER TABLE public.system_map_loop_reservations VALIDATE CONSTRAINT ck_loop_reservations_reserved_budget;",
    "ALTER TABLE public.system_map_loop_reservations VALIDATE CONSTRAINT ck_loop_reservations_actual_spend;",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Add the loop admission/runtime columns and their checks."""
    op.add_column(
        _TABLE_LOOP_CONFIGS,
        sa.Column("max_concurrent_runs", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )

    op.add_column(_TABLE_ATTEMPTS, sa.Column("run_id", sa.Uuid(), nullable=True))
    op.add_column(_TABLE_ATTEMPTS, sa.Column("rejection_reason", sa.String(length=32), nullable=True))
    op.add_column(
        _TABLE_ATTEMPTS,
        sa.Column("month_start", sa.Date(), nullable=False, server_default=sa.text(_MONTH_START_DEFAULT)),
    )

    op.add_column(_TABLE_RESERVATIONS, sa.Column("admission_attempt_id", sa.Uuid(), nullable=True))
    op.add_column(
        _TABLE_RESERVATIONS,
        sa.Column("month_start", sa.Date(), nullable=False, server_default=sa.text(_MONTH_START_DEFAULT)),
    )
    op.add_column(
        _TABLE_RESERVATIONS,
        sa.Column("reserved_budget_usd", sa.Numeric(14, 6), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(_TABLE_RESERVATIONS, sa.Column("actual_spend_usd", sa.Numeric(14, 6), nullable=True))

    op.create_index(
        "ix_loop_reservations_month",
        _TABLE_RESERVATIONS,
        ["organisation_id", "loop_config_id", "month_start", "status"],
    )
    op.create_index(
        "uq_loop_reservations_attempt",
        _TABLE_RESERVATIONS,
        ["admission_attempt_id"],
        unique=True,
        postgresql_where=sa.text("admission_attempt_id IS NOT NULL"),
    )

    if not _is_postgres():
        return

    op.create_foreign_key(
        "fk_loop_reservations_attempt",
        _TABLE_RESERVATIONS,
        _TABLE_ATTEMPTS,
        ["admission_attempt_id", "organisation_id"],
        ["id", "organisation_id"],
        ondelete="RESTRICT",
    )
    for statement in (
        _LOOP_CONCURRENCY_CHECK,
        _REJECTION_REASON_CHECK,
        _ATTEMPT_OUTCOME_CHECK,
        _RESERVED_BUDGET_CHECK,
        _ACTUAL_SPEND_CHECK,
    ):
        op.execute(sa.text(statement))
    for statement in _VALIDATE_STATEMENTS:
        op.execute(sa.text(statement))


def downgrade() -> None:
    """Drop the loop admission/runtime columns."""
    if _is_postgres():
        op.drop_constraint("fk_loop_reservations_attempt", _TABLE_RESERVATIONS, type_="foreignkey")
        for name, table in (
            ("ck_loop_admission_attempts_reason", _TABLE_ATTEMPTS),
            ("ck_loop_admission_attempts_outcome", _TABLE_ATTEMPTS),
            ("ck_loop_configs_max_concurrent_runs", _TABLE_LOOP_CONFIGS),
            ("ck_loop_reservations_reserved_budget", _TABLE_RESERVATIONS),
            ("ck_loop_reservations_actual_spend", _TABLE_RESERVATIONS),
        ):
            op.drop_constraint(name, table, type_="check")

    op.drop_index("uq_loop_reservations_attempt", table_name=_TABLE_RESERVATIONS)
    op.drop_index("ix_loop_reservations_month", table_name=_TABLE_RESERVATIONS)

    op.drop_column(_TABLE_RESERVATIONS, "actual_spend_usd")
    op.drop_column(_TABLE_RESERVATIONS, "reserved_budget_usd")
    op.drop_column(_TABLE_RESERVATIONS, "month_start")
    op.drop_column(_TABLE_RESERVATIONS, "admission_attempt_id")

    op.drop_column(_TABLE_ATTEMPTS, "month_start")
    op.drop_column(_TABLE_ATTEMPTS, "rejection_reason")
    op.drop_column(_TABLE_ATTEMPTS, "run_id")

    op.drop_column(_TABLE_LOOP_CONFIGS, "max_concurrent_runs")
