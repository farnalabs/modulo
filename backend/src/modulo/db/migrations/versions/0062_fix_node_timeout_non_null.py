"""Make node.timeout_seconds non-nullable with default 300."""

from alembic import op

revision = "0030_fix_node_timeout_non_null"
down_revision = "0029_fix_pipeline_max_duration_non_null"


def upgrade() -> None:
    op.execute("UPDATE nodes SET timeout_seconds = 300 WHERE timeout_seconds IS NULL")
    op.execute("ALTER TABLE nodes ALTER COLUMN timeout_seconds SET DEFAULT 300")
    op.execute("ALTER TABLE nodes ALTER COLUMN timeout_seconds SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE nodes ALTER COLUMN timeout_seconds DROP NOT NULL")
    op.execute("ALTER TABLE nodes ALTER COLUMN timeout_seconds DROP DEFAULT")
