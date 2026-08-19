"""TOCTOU hardening via atomic constraints (#1376, #1105).

Revision ID: 0117_toctou_hardening
Revises: 0116_guardrail_trust_pr_b
Create Date: 2026-08-19

Two check-then-act race windows are closed at the database level:

1. **Error-notification-rule cap (#1376)** — ``error_notification_rules``
   gains a ``deleted_at`` column (nullable, for future soft-delete support)
   and a partial unique index on ``(organisation_id, signal)`` WHERE
   ``deleted_at IS NULL``. This enforces ONE active rule per signal per org —
   the invariant the default-rule seed (``seed_default_alert_rules_for_org``,
   which inserts one default rule per signal) requires — while still allowing up
   to ``_MAX_RULES_PER_ORG`` (10) distinct active rules per org. The application
   still count-then-inserts in ``create_notification_rule`` for the cap; the
   unique index is the backstop that prevents duplicate default seeds.

2. **Per-pipeline trigger rate limit (#1105)** — the existing plain index on
   ``runs.rate_limit_key`` is replaced with a partial unique index on
   ``(pipeline_id, rate_limit_key)`` WHERE ``rate_limit_key IS NOT NULL``.
   ``create_run`` catches ``IntegrityError`` on insert and translates it to
   a rate-limit error, making admission atomic.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0117_toctou_hardening"
down_revision: str | None = "0116_guardrail_trust_pr_b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── #1376: error-notification-rule cap ──────────────────────────────
    op.add_column(
        "error_notification_rules",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_enr_org_active ON error_notification_rules (organisation_id, signal) WHERE deleted_at IS NULL"
    )

    # ── #1105: per-pipeline trigger rate limit ──────────────────────────
    # Drop the plain index before creating the unique partial index.
    op.execute("DROP INDEX IF EXISTS ix_runs_rate_limit_key")
    op.execute(
        "CREATE UNIQUE INDEX uq_runs_pipeline_rate_limit_key "
        "ON runs (pipeline_id, rate_limit_key) "
        "WHERE rate_limit_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_runs_pipeline_rate_limit_key")
    op.execute("CREATE INDEX ix_runs_rate_limit_key ON runs USING btree (rate_limit_key)")
    op.execute("DROP INDEX IF EXISTS uq_enr_org_active")
    op.drop_column("error_notification_rules", "deleted_at")
