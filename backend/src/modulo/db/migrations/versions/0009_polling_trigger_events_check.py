"""Add polling validation_result values to trigger_events check constraint.

Revision ID: 0009_polling_trigger_events_check
Revises: 0008_otel_config
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_polling_trigger_events_check"
down_revision: str | None = "0008_otel_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_CONSTRAINT = (
    "validation_result IN ('accepted', 'passed', 'hmac_failed', "
    "'schema_validation_failed', 'deduplicated', 'concurrency_limit_reached', "
    "'flood_rejected', 'timestamp_expired', 'validation_failed', 'rate_limited', "
    "'no_match', 'condition_met', 'poll_error')"
)

_OLD_CONSTRAINT = (
    "validation_result IN ('passed', 'hmac_failed', "
    "'schema_validation_failed', 'deduplicated', 'concurrency_limit_reached', "
    "'timestamp_expired', 'validation_failed', 'rate_limited')"
)


def upgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE trigger_events DROP CONSTRAINT "
            "ck_trigger_events_validation_result"
        )
    )
    op.execute(
        sa.text(
            f"ALTER TABLE trigger_events ADD CONSTRAINT "
            f"ck_trigger_events_validation_result CHECK ({_NEW_CONSTRAINT})"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM trigger_events "
            "WHERE validation_result IN "
            "('accepted', 'flood_rejected', 'no_match', 'condition_met', 'poll_error')"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE trigger_events DROP CONSTRAINT "
            "ck_trigger_events_validation_result"
        )
    )
    op.execute(
        sa.text(
            f"ALTER TABLE trigger_events ADD CONSTRAINT "
            f"ck_trigger_events_validation_result CHECK ({_OLD_CONSTRAINT})"
        )
    )
