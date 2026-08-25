"""Daily run facts — the analytics denormalised fact table (ADR 020).

One row per terminal run, written by ``record_run_facts`` on every finalize
path and backfilled/maintained by the ``analytics_facts_maintenance`` cron.
Every raw terminal writer that bypasses ``finalize_cost`` writes a compensating
fact row (in its own separate session) through the shared
``record_fact_for_terminal_failed_run`` wrapper: the SAQ task_failure hook, the
stale-run sweep terminalizers (``never_dispatched`` / ``capacity_timeout`` /
``worker_lost``), the ``dispatcher_reconcile`` terminalizers
(``executor_superseded`` / ``claim_cap_exhausted`` / ``dispatch_failed``) and
``fail_run_terminal`` (``executor_stalled`` / ``executor_heartbeat_lost`` /
``executor_failed`` / ``executor_setup_failed``) — so no terminal run is ever
invisible to analytics.
The facts survive the 90-day run purge (``run_id`` is deliberately NOT a
foreign key), so dimensioned run history outlives the ``runs`` rows it was
derived from.

``JourneyFact`` (FAR-143 part 4) is the per-writer self-report denominator
table in the same file: one row per (run, finalize-writer path) carrying the
parse-failure / finalise-attempt counts needed to compute a 7d self-report
parse-failure ratio after the source ``runs`` rows are swept.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.pipeline_folder import PipelineFolder
    from modulo.db.models.team import Team


class RunDailyFact(OrgScoped):
    """A daily analytics fact for one terminal run.

    ``run_id`` is a surrogate business key with a UNIQUE index — deliberately
    NOT a FK to ``runs``: facts must survive the 90-day run purge. A future
    "fix" into an FK breaks retention. ``created_at`` is the source run's
    created-at instant (rolling-window precision for "last 24h" queries);
    ``run_date`` is the UTC day the run is attributed to (started-at or
    created-at, matching the ledger).
    """

    __tablename__ = "run_daily_facts"
    __table_args__ = (
        # Per-org daily dimensioned-history access path (ADR 020).
        Index("ix_run_daily_facts_org_date", "organisation_id", "run_date"),
        # One fact per run — the upsert target of the live writer and backfill.
        Index("uq_run_daily_facts_run_id", "run_id", unique=True),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        nullable=False,
        comment=(
            "deliberately NOT a FK to runs — facts must survive the 90-day run "
            "purge; a future 'fix' into an FK breaks retention"
        ),
    )
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="SET NULL"), index=True)
    team_name: Mapped[str | None] = mapped_column(String(255))
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("pipelines.id", ondelete="SET NULL"), index=True
    )
    pipeline_name: Mapped[str | None] = mapped_column(String(255))
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("pipeline_folders.id", ondelete="SET NULL"), index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    total_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    # FAR-102 enrichment — stall dimensions + other run facts (migration 0071).
    # All nullable where the source run may not carry the value.
    error_code: Mapped[str | None] = mapped_column(String(255), comment="the stall dimension — from Run.error_code")
    claim_count: Mapped[int | None] = mapped_column(Integer)
    queue_wait_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="Run.started_at - Run.dispatched_at when both present, else NULL"
    )
    final_idle_ms: Mapped[int | None] = mapped_column(
        BigInteger, comment="Run.completed_at - Run.heartbeat_at (the stuck-with-no-heartbeat window), else NULL"
    )
    cancellation_requested: Mapped[bool | None] = mapped_column(Boolean)
    dispatcher: Mapped[str | None] = mapped_column(String(20))
    node_count: Mapped[int | None] = mapped_column(
        Integer, comment="number of nodes in the pipeline snapshot graph_json (NULL-safe)"
    )
    sandbox_agent_node_count: Mapped[int | None] = mapped_column(
        Integer, comment="count of sandbox_agent nodes in the snapshot graph_json (NULL-safe)"
    )
    max_node_timeout_seconds: Mapped[int | None] = mapped_column(
        Integer, comment="max timeout_seconds across snapshot graph nodes (NULL-safe)"
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        comment=(
            "deliberately NOT a FK to runs — facts survive the run purge; a future 'fix' into an FK breaks retention"
        ),
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        comment=(
            "FAR-332 batch-scoped variant comparison — the batch_id every run fired "
            "together by run_variant_batch shares; NOT a FK (facts survive the run "
            "purge, ADR 020). Null for legacy and non-variant runs."
        ),
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), comment="NOT a FK — the snapshot row may be purged independently of the fact"
    )
    run_number: Mapped[int | None] = mapped_column(Integer)
    output_bytes: Mapped[int | None] = mapped_column(
        BigInteger, comment="serialised size of Run.outputs_json (json.dumps length) when present"
    )
    telemetry_bytes: Mapped[int | None] = mapped_column(
        BigInteger, comment="serialised size of Run.node_telemetry_json (json.dumps length) when present"
    )
    rate_limited: Mapped[bool | None] = mapped_column(Boolean, comment="True when Run.rate_limit_key is not null")
    # FAR-134 concurrency/slot-utilization columns (migration 0075) — absolute
    # UTC instants copied from the source run. Deliberately NOT FKs — facts
    # survive the run purge (ADR 020). The overlap between [started_at,
    # completed_at) is what the concurrency query surface buckets.
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="absolute UTC instant the run was dispatched to the queue — from Run.dispatched_at",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="absolute UTC instant the run started executing — from Run.started_at"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="absolute UTC instant the run completed — from Run.completed_at"
    )
    total_queue_wait_ms: Mapped[int | None] = mapped_column(
        BigInteger,
        comment="Run.started_at - Run.created_at (full wait from creation to start), else NULL",
    )

    team: Mapped[Optional["Team"]] = relationship(foreign_keys=[team_id])
    pipeline: Mapped[Optional["Pipeline"]] = relationship(foreign_keys=[pipeline_id])
    folder: Mapped[Optional["PipelineFolder"]] = relationship(foreign_keys=[folder_id])


class JourneyFact(OrgScoped):
    """Per-writer journey self-report denominators (FAR-143 part 4, migration 0085).

    One row per ``(run_id, writer)`` — *writer* is the finalize write path that
    drove the journey hook (``live`` / ``fallback`` / ``early_return``). The
    counters are written from the journey finalise hook (fail-open) and are
    enough to compute a 7d self-report parse-failure ratio
    (``SUM(parse_failures) / SUM(finalise_attempts)``) after the ``runs`` rows
    are purged. ``run_id`` is deliberately NOT a FK — like ``run_daily_facts``,
    journey facts must survive the 90-day run purge (a future "fix" into an FK
    breaks retention). ``created_at`` is the fact's own write instant.
    """

    __tablename__ = "modulo_journey_facts"
    __table_args__ = (
        # 7d ratio lookups — org + fact write instant (run-sweep independent).
        Index("ix_modulo_journey_facts_org_created", "organisation_id", "created_at"),
        # One fact per (run, writer) — the upsert target of the live writer.
        UniqueConstraint("run_id", "writer", name="uq_modulo_journey_facts_run_writer"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        nullable=False,
        comment=(
            "deliberately NOT a FK to runs — journey facts must survive the 90-day "
            "run purge; a future 'fix' into an FK breaks retention"
        ),
    )
    writer: Mapped[str] = mapped_column(String(30), nullable=False)
    parse_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    finalise_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
