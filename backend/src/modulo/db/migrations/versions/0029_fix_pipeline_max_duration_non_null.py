"""Make pipeline.max_duration_seconds non-nullable with default 3600."""

from alembic import op

revision = "0029_fix_pipeline_max_duration_non_null"
down_revision = "0029_fix_expiry_fields_non_null"


def upgrade() -> None:
    op.execute("UPDATE pipelines SET max_duration_seconds = 3600 WHERE max_duration_seconds IS NULL")
    op.execute("ALTER TABLE pipelines ALTER COLUMN max_duration_seconds SET DEFAULT 3600")
    op.execute("ALTER TABLE pipelines ALTER COLUMN max_duration_seconds SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE pipelines ALTER COLUMN max_duration_seconds DROP NOT NULL")
    op.execute("ALTER TABLE pipelines ALTER COLUMN max_duration_seconds DROP DEFAULT")
