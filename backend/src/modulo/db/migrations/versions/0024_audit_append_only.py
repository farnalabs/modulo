"""Add database-level triggers preventing UPDATE/DELETE on audit_events.

Also registers the ORM-level event listeners for defense in depth.

Revision ID: 0024_audit_append_only
Revises: 0023_run_error_detail, 0072_eval_suite_threshold
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_audit_append_only"
down_revision: str | Sequence[str] | None = (
    "0023_run_error_detail",
    "0072_eval_suite_threshold",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create trigger function that raises an exception on UPDATE/DELETE
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION audit_events_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'audit_events are append-only: DELETE is not permitted';
            ELSIF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'audit_events are append-only: UPDATE is not permitted';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """)
    )

    # Attach BEFORE UPDATE trigger
    op.execute(
        sa.text("""
        CREATE TRIGGER audit_events_no_update
            BEFORE UPDATE ON audit_events
            FOR EACH ROW
            EXECUTE FUNCTION audit_events_append_only();
        """)
    )

    # Attach BEFORE DELETE trigger
    op.execute(
        sa.text("""
        CREATE TRIGGER audit_events_no_delete
            BEFORE DELETE ON audit_events
            FOR EACH ROW
            EXECUTE FUNCTION audit_events_append_only();
        """)
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS audit_events_no_delete ON audit_events"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS audit_events_append_only()"))
