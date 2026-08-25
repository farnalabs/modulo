"""Add port-addressing columns to ``pipeline_edges`` (FAR-416 / FAR-402 F1).

Revision ID: 0141_pipeline_edge_ports
Revises: 0140_eval_regression_alert
Create Date: 2026-08-25

F1 introduces port-addressed typed state. Edges gain ``source_port`` and
``target_port`` (default ``out`` / ``in``) — ADDITIVE metadata over the
existing flat run_context/artifact dict. The defaults mirror the pre-port
flat-state keys, so existing (port-less) edges route identically with no
rewrite. Both columns are NOT NULL with a server_default, so in-flight rows
are migrated implicitly by the database on first write; no batch backfill is
required and the change is zero-break for existing pipelines.
"""

from __future__ import annotations

from alembic import op

revision: str = "0141_pipeline_edge_ports"
down_revision: str | None = "0140_eval_regression_alert"
branch_labels: str | None = None
depends_on: str | None = None


def _upgrade_postgres() -> None:
    op.execute(
        'ALTER TABLE public."pipeline_edges" '
        "ADD COLUMN IF NOT EXISTS \"source_port\" varchar(64) NOT NULL DEFAULT 'out';"
    )
    op.execute(
        'ALTER TABLE public."pipeline_edges" '
        "ADD COLUMN IF NOT EXISTS \"target_port\" varchar(64) NOT NULL DEFAULT 'in';"
    )
    # Drop the now-redundant per-row server default so the NOT NULL constraint
    # is enforced for future inserts without silently re-applying the legacy default.
    op.execute('ALTER TABLE public."pipeline_edges" ALTER COLUMN "source_port" DROP DEFAULT;')
    op.execute('ALTER TABLE public."pipeline_edges" ALTER COLUMN "target_port" DROP DEFAULT;')


def _upgrade_other() -> None:
    from sqlalchemy import Column, String

    with op.batch_alter_table("pipeline_edges") as batch:
        batch.add_column(Column("source_port", String(64), nullable=False, server_default="out"))
        batch.add_column(Column("target_port", String(64), nullable=False, server_default="in"))


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _upgrade_postgres()
    else:
        # SQLite test backend builds its schema from the ORM model, so the
        # columns appear automatically; this branch guards parity only.
        _upgrade_other()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute('ALTER TABLE public."pipeline_edges" DROP COLUMN IF EXISTS "source_port";')
        op.execute('ALTER TABLE public."pipeline_edges" DROP COLUMN IF EXISTS "target_port";')
    else:
        with op.batch_alter_table("pipeline_edges") as batch:
            batch.drop_column("source_port")
            batch.drop_column("target_port")
