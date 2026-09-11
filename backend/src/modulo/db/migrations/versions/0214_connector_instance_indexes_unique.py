"""Add composite indexes and unique constraint for connector_instances.

Lens: Indexes — add composite indexes on common query patterns.
Lens: Constraints — add unique constraint on (organisation_id, name) to
prevent duplicate connector names within an org.

The unique constraint uses a partial index (WHERE deleted_at IS NULL) so
that soft-deleted rows with the same name do not block re-creation.
"""

from alembic import op

revision: str = "0214_connector_instance_indexes_unique"
down_revision: str | None = "0213_runs_rerun_trigger_type"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Composite index: list connectors by org + account (common query pattern)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_connector_instances_org_account "
        'ON public."connector_instances" USING btree (organisation_id, account_id);'
    )
    # Composite index: list connectors by org + type
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_connector_instances_org_type "
        'ON public."connector_instances" USING btree (organisation_id, connector_type_id);'
    )
    # Unique constraint: prevent duplicate connector names per org (active only)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_connector_instances_org_name "
        'ON public."connector_instances" USING btree (organisation_id, name) '
        "WHERE (deleted_at IS NULL);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_connector_instances_org_name;")
    op.execute("DROP INDEX IF EXISTS ix_connector_instances_org_type;")
    op.execute("DROP INDEX IF EXISTS ix_connector_instances_org_account;")
