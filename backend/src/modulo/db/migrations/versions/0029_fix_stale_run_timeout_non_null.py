"""Make stale_run_timeout_minutes non-nullable with default 30."""

from alembic import op

revision = "0029_fix_stale_run_timeout_non_null"
down_revision = "0028_add_claimed_by_to_runs"


def upgrade() -> None:
    op.execute("UPDATE pipelines SET stale_run_timeout_minutes = 30 WHERE stale_run_timeout_minutes IS NULL")
    op.execute("ALTER TABLE pipelines ALTER COLUMN stale_run_timeout_minutes SET DEFAULT 30")
    op.execute("ALTER TABLE pipelines ALTER COLUMN stale_run_timeout_minutes SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE pipelines ALTER COLUMN stale_run_timeout_minutes DROP NOT NULL")
    op.execute("ALTER TABLE pipelines ALTER COLUMN stale_run_timeout_minutes DROP DEFAULT")
