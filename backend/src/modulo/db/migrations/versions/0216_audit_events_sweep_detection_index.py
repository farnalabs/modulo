"""Add the FAR-611 sweep-alarm detection composite index on audit_events.

The approve-sweep detection aggregate (``hitl_manager.sweep_alarm.
count_recent_approves``) filters by (organisation_id, event_type,
account_id, created_at). Before this migration that shape was served by
the single-column indexes over an org-narrowed subset, which was fine at
FAR-611 volumes; the composite index pins the query shape so approve
volume growth can never turn the detection into a hot
pg_stat_statements entry.

The index is created idempotently (CREATE INDEX IF NOT EXISTS, the repo's
0128/0154/0155/0214 convention) so ORM-created test schemas and already-
altered databases converge without error.
"""

from alembic import op

revision: str = "0216_audit_events_sweep_detection_index"
down_revision: str | None = "0215_drop_runs_blob_columns"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_events_org_type_actor_time "
        'ON public."audit_events" USING btree (organisation_id, event_type, account_id, created_at);'
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_audit_events_org_type_actor_time;")
