"""FAR-1463: node-deadline watchdog firing counters on ``runs`` + ``run_daily_facts``.

Revision ID: 0280_runs_node_deadline_watchdog_fired_count
Revises: 0279_table_autovacuum_tuning
Create Date: 2026-10-03

FAR-1423 established that the absolute node-deadline watchdog (FAR-369) DOES
fire, but on firing it re-dispatches through the shared retry hook rather than
terminal-failing — and the fenced pending-reset nulls ``error_code`` — so a
firing left ZERO analytics fingerprints (``error_code='node_deadline_exceeded'``
read 0 in every bucket, which was misread as "the watchdog never fires").

Two additive columns make the firing durable on the EXISTING observability
surfaces (no parallel endpoint/UI)::

    runs.node_deadline_watchdog_fired_count         integer NOT NULL DEFAULT 0
    run_daily_facts.node_deadline_watchdog_fired_count integer NULL

``runs`` is the source: ``pipeline_execution._fail_overdue_node`` increments it
(best-effort, org-scoped, committed immediately) BEFORE the retry consult, so
both outcomes — re-dispatch and terminal fail — are counted. The fact column is
the read side: ``record_run_facts`` copies it at finalize and ``backfill_facts``
copies it for gap-fills, keeping facts self-contained (ADR 020 — no ``runs``
join on the read path, and the marker outlives the 90-day run purge).

Survival across a re-dispatch is by construction, not by backfill: neither the
atomic claim (``_CLAIM_UPDATE_SQL``, which stamps status / heartbeat_at /
claim_count / dispatch_phase / claim_token) nor the fenced pending-reset
(``status='pending', error_code=NULL, error_detail=NULL``) names the new
column, so the count is intact when the run is re-claimed — unlike
``dispatch_phase``, which every re-claim deliberately resets to ``'claimed'``.

NO data backfill: the counter only starts counting from this revision, so every
existing ``runs`` row legitimately reads 0 (no firing was ever recorded) and
every pre-existing fact keeps NULL — the accurate "recorded before this
shipped" value. A backfill would copy zeros into NULLs and lose that signal.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0280_runs_node_deadline_watchdog_fired_count"
down_revision: str | None = "0279_table_autovacuum_tuning"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# NOT NULL DEFAULT 0 mirrors the existing run counters (claim_count /
# node_attempt_count): PG 11+ adds it without a table rewrite, and existing
# rows correctly read "no firing recorded".
_RUNS_COUNTER = sa.Column(
    "node_deadline_watchdog_fired_count",
    sa.Integer(),
    nullable=False,
    server_default="0",
    comment=(
        "node-deadline watchdog (FAR-369) firings — incremented on every firing, "
        "re-dispatch and terminal-fail alike (FAR-1463); copied to run_daily_facts at finalize"
    ),
)
# Nullable on the fact side, like every other enrichment column: NULL marks a
# fact finalized before this revision, when no firing was ever recorded.
_FACTS_COUNTER = sa.Column(
    "node_deadline_watchdog_fired_count",
    sa.Integer(),
    nullable=True,
    comment=(
        "node-deadline watchdog firings — from Run.node_deadline_watchdog_fired_count (NULL for pre-FAR-1463 facts)"
    ),
)


def upgrade() -> None:
    op.add_column("runs", _RUNS_COUNTER)
    op.add_column("run_daily_facts", _FACTS_COUNTER)


def downgrade() -> None:
    # Data columns only — the counts go with them. op.drop_column is
    # dialect-agnostic (no schema-qualified raw SQL needed: there is no
    # backfill to undo), so a SQLite/MariaDB downgrade works too.
    op.drop_column("run_daily_facts", "node_deadline_watchdog_fired_count")
    op.drop_column("runs", "node_deadline_watchdog_fired_count")
