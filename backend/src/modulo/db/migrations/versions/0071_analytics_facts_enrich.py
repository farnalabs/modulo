"""Enrich run_daily_facts — stall dimensions + other run facts (FAR-102).

Revision ID: 0071_analytics_facts_enrich
Revises: 0070_analytics_page_community_tier
Create Date: 2026-08-07

Adds the FAR-102 enrichment columns to the analytics fact table. All new
columns are nullable (the source run may not carry the value), including the
graph-derived fields (``node_count``, ``sandbox_agent_node_count``,
``max_node_timeout_seconds``) which are computed at write/backfill time from
the snapshot's ``graph_json``. ``parent_run_id`` and ``snapshot_id`` are
deliberately NOT foreign keys — like ``run_id``, facts must survive the run
and snapshot purge (ADR 020).

Only ADD COLUMN statements — no RLS policy, role, or index changes: the new
columns inherit the table's owner and the existing ``rls_org_isolation``
policy applies to them automatically. Non-Postgres dev backends (SQLite /
MariaDB) render the columns via ``sa.Uuid()`` / ``sa.JSON()`` equivalents the
same way the surrounding migrations do.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071_analytics_facts_enrich"
down_revision: str | None = "0070_analytics_page_community_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, type, comment) — comment kept in sync with the ORM mapped_column.
_NEW_COLUMNS: list[tuple[str, sa.types.TypeEngine[object], str]] = [
    (
        "error_code",
        sa.String(length=255),
        "the stall dimension — from Run.error_code",
    ),
    ("claim_count", sa.Integer(), "claim_count from the source run"),
    (
        "queue_wait_ms",
        sa.BigInteger(),
        "Run.dispatched_at - Run.started_at when both present, else NULL",
    ),
    (
        "final_idle_ms",
        sa.BigInteger(),
        "Run.completed_at - Run.heartbeat_at (the stuck-with-no-heartbeat window), else NULL",
    ),
    ("cancellation_requested", sa.Boolean(), "from Run.cancellation_requested"),
    ("dispatcher", sa.String(length=20), "from Run.dispatcher"),
    (
        "node_count",
        sa.Integer(),
        "number of nodes in the pipeline snapshot graph_json (NULL-safe)",
    ),
    (
        "sandbox_agent_node_count",
        sa.Integer(),
        "count of sandbox_agent nodes in the snapshot graph_json (NULL-safe)",
    ),
    (
        "max_node_timeout_seconds",
        sa.Integer(),
        "max timeout_seconds across snapshot graph nodes (NULL-safe)",
    ),
    (
        "parent_run_id",
        sa.Uuid(),
        "NOT a FK to runs — facts survive the run purge (ADR 020)",
    ),
    ("snapshot_id", sa.Uuid(), "NOT a FK — the snapshot may be purged independently"),
    ("run_number", sa.Integer(), "from Run.run_number"),
    (
        "output_bytes",
        sa.BigInteger(),
        "serialised size of Run.outputs_json (json.dumps length) when present",
    ),
    ("rate_limited", sa.Boolean(), "True when Run.rate_limit_key is not null"),
]


def upgrade() -> None:
    for name, type_, comment in _NEW_COLUMNS:
        op.add_column("run_daily_facts", sa.Column(name, type_, nullable=True, comment=comment))


def downgrade() -> None:
    for name, _type, _comment in reversed(_NEW_COLUMNS):
        op.drop_column("run_daily_facts", name)
