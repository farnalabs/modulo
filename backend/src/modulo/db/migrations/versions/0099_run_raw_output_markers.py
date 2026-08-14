"""Add the ``raw_output_markers`` column to runs (FAR-188)

Revision ID: 0099_run_raw_output_markers
Revises: 0098_slack_app_mention_trigger_type
Create Date: 2026-08-14

FAR-188 raw-output retention moves OUT of the Agent Return Contract columns
(``outputs_json`` / ``node_telemetry_json``) into a DEDICATED nullable JSONB
column ``raw_output_markers``. When a ``sandbox_agent`` node's ``output.json``
fails to parse (or the command stalls/times out), the raw evidence is retained
keyed by ``attempt_key`` so every retry attempt's ``pr_url`` survives and the
markers can never be mistaken for genuine agent output — a reserved
``_modulo_marker: true`` discriminator rides on each marker so classification
(FAR-189) can unambiguously distinguish retention markers from real node
output.

The ORM model maps the column as generic JSON for SQLite/MariaDB parity (the
``work_item_refs`` precedent, migration 0083). No index: the markers are
written once per attempt and read by the classification pipeline per run — not
queried in bulk.

Downgrade drops the column (additive, nullable, never backfilled — safe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0099_run_raw_output_markers"
down_revision: str | None = "0098_slack_app_mention_trigger_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("raw_output_markers", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "raw_output_markers")
