"""Per-pipeline benign run-outcome signatures + the analytics benign marker (FAR-707).

Revision ID: 0296_benign_run_outcomes
Revises: 0295_decision_record_event_identity
Create Date: 2026-10-11

FAR-707 (benign-outcome classification). Two nullable columns, both with the
NULL default being byte-identical to today's behaviour:

* ``pipelines.benign_signatures`` (JSON, NULL) — the EXPLICIT per-pipeline
  benign-signature config the run classifier consults at terminalization.
  Shape: ``[{"error_code": "<exact runs.error_code>",
  "error_detail_contains": "<case-insensitive substring, optional>"}]``.
  NULL = no signatures = the pre-FAR-707 decision table. No CHECK on the JSON
  shape: the reader (``core.pipeline_engine.classify.normalize_benign_signatures``)
  deliberately tolerates malformed entries and degrades to "no signatures",
  so a bad value can never make ``alembic upgrade`` (or a later config edit)
  fail closed against a whole pipeline.

* ``run_daily_facts.benign`` (BOOLEAN, NULL) — whether the run's terminal
  ``run_classification`` record carried value ``benign`` when the fact was
  written. The analytics read path (``core.analytics.builder``) needs it to
  drop business-expected outcomes from failure rates WITHOUT joining ``runs``
  (ADR 020: facts are self-contained and outlive the run purge). NULL = a fact
  written before this column existed, or before the raw-SQL terminalizer's
  classification landed (the reconcile sweep backfills classification within a
  minute, the FACT is not rewritten) — readers treat NULL as "not benign",
  i.e. the historical counting.

No backfill: both columns are additive with the NULL/absent value preserving
existing behaviour exactly (the run-classification precedent, migration 0110).
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0296_benign_run_outcomes"
down_revision: str | None = "0295_decision_record_event_identity"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("benign_signatures", sa.JSON(), nullable=True))
    op.add_column("run_daily_facts", sa.Column("benign", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_daily_facts", "benign")
    op.drop_column("pipelines", "benign_signatures")
