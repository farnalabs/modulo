"""PipelineSnapshot live-edit history + release channels (FAR-402 P6).

Revision ID: 0148_pipeline_snapshot_versioning_far420
Revises: 0147_json_to_jsonb_standardize
Create Date: 2026-08-25

Adds four additive columns to ``pipeline_snapshots`` so the existing snapshot
machinery (``pipeline_snapshot_versioning``) can be reused for live-edit history
and release channels WITHOUT a new table — per design decision 4 of
``docs/design/execution-graph-composition.md`` (§11):

* ``version_kind``  — ``'edit'`` (a live-edit version) vs ``'run'`` (run-frozen
  snapshot). Discriminates the live-edit chain from run snapshots.
* ``created_kind``  — finer GUI provenance discriminator:
  ``'initial' | 'edit' | 'rollback' | 'run'``.
* ``draft``         — marks an in-progress editor auto-save (``false`` = a
  committed edit/run snapshot).
* ``channel``       — release channel the snapshot was created under:
  ``'none' | 'stable' | 'canary'``. ``'none'`` is the default (current
  behaviour — a run pins the live graph; no channel routing).

All four have a ``server_default`` so existing rows are backfilled with the
legacy semantics (a run-kind, non-draft, no-channel snapshot) and the migration
is safe to apply without a data migration. The columns are additive and
nullable-permitting; no constraint/index drop is required, so both the Postgres
(production) and SQLite (unit-test ``create_all``) paths use plain
``op.add_column``.

Reversible: ``downgrade`` drops the four columns. ``pipeline_snapshots`` is NOT
owned by ``modulo_migrate`` (it is an app-owned runtime table, migration 0003),
so no ``SET ROLE`` ceremony is used — the migration runs as its caller, which
holds the capability to ALTER an app-owned table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0148_pipeline_snapshot_versioning_far420"
down_revision: str | None = "0147_json_to_jsonb_standardize"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("version_kind", sa.String(length=10), "run"),
    ("created_kind", sa.String(length=10), "run"),
    ("channel", sa.String(length=10), "none"),
)


def upgrade() -> None:
    # String defaults: Alembic renders the server_default as a quoted literal.
    for name, coltype, server_default in _COLUMNS:
        op.add_column(
            "pipeline_snapshots",
            sa.Column(name, coltype, nullable=False, server_default=server_default),
        )
    # Boolean column — render ``false`` as the server default for both dialects.
    op.add_column(
        "pipeline_snapshots",
        sa.Column("draft", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    for name, _coltype, _server_default in _COLUMNS:
        op.drop_column("pipeline_snapshots", name)
    op.drop_column("pipeline_snapshots", "draft")
