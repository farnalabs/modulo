"""One persisted spend-anomaly row per detected org-day (feat-costs).

Revision ID: 0201_spend_anomaly_unique_org_date
Revises: 0200_runs_runner_marker_sweep_index
Create Date: 2026-09-08

The rolling spend-anomaly endpoint now persists freshly detected anomalies on
first sight so they are dismissible: a detection that only lives in the
in-memory result carries an empty ``id`` and can never be targeted by
``POST /anomalies/dismiss/{id}``. Without a uniqueness guarantee, two reads for
the same org/day can double-insert, and a dismissal would then hide only one of
two rows describing the same date.

Detection is org-level today (daily org spend vs its trailing 7-day average):
``pipeline_id`` is always NULL for detected rows. The partial predicate
``WHERE pipeline_id IS NULL`` scopes uniqueness to the actually-modelled
per-day detections and leaves room for a future per-pipeline detection surface.
The predicate is ignored by the conformance SQLite/MariaDB backends, where the
index is created without it (same convention as 0127_soft_delete_partial_unique).
"""

from alembic import op
from sqlalchemy import text

revision: str = "0201_spend_anomaly_unique_org_date"
down_revision: str | None = "0200_runs_runner_marker_sweep_index"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "uq_spend_anomalies_org_date",
        "spend_anomalies",
        ["organisation_id", "anomaly_date"],
        unique=True,
        postgresql_where=text("pipeline_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_spend_anomalies_org_date", table_name="spend_anomalies")
