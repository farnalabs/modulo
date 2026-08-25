"""Metrics staging table for product analytics (FAR-355).

Revision ID: 0121_metrics_staging
Revises: 0120_org_fk_hardening
Create Date: 2026-08-21

Creates the ``metrics_staging`` table used by the product analytics ingest
endpoint.  Events are staged here and consumed by the daily ``metrics_dump``
SAQ cron job (FAR-356).

This is a reconciliation-style migration: it is idempotent (guards on existing
tables) and intentionally non-reversible.
"""

import sqlalchemy as sa
from alembic import op

revision = "0121_metrics_staging"
down_revision = "0120_org_fk_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS metrics_staging (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
                event_id VARCHAR(128) NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                recorded_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
                created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
            )
            """
        )
    )

    # Unique constraint on (organisation_id, event_id) for dedup
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_metrics_staging_org_event_id'
                ) THEN
                    ALTER TABLE metrics_staging
                    ADD CONSTRAINT uq_metrics_staging_org_event_id
                    UNIQUE (organisation_id, event_id);
                END IF;
            END $$;
            """
        )
    )

    # Index on (organisation_id, recorded_at) for dump queries
    op.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_metrics_staging_org_recorded_at
            ON metrics_staging (organisation_id, recorded_at)
            """
        )
    )

    # Row-level security — metrics_staging is org-scoped (OrgScoped base).
    op.execute(
        sa.text(
            """
            ALTER TABLE "metrics_staging" ENABLE ROW LEVEL SECURITY
            """
        )
    )
    # Idempotent guard: the table/index/constraint above use IF NOT EXISTS, so
    # this reconciliation migration must not fail when re-applied against a DB
    # that already carries the policy (e.g. a reused/persisted Postgres, or a
    # re-run of the migration chain). CREATE POLICY has no native IF NOT EXISTS.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename = 'metrics_staging'
                      AND policyname = 'rls_org_isolation'
                ) THEN
                    CREATE POLICY rls_org_isolation ON "metrics_staging"
                    USING (organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid);
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    # Non-reversible reconciliation migration
    pass
