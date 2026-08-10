"""Daily run facts — the analytics denormalised fact table (ADR 020).

One row per terminal run, written by ``record_run_facts`` on every finalize
path and backfilled/maintained by the ``analytics_facts_maintenance`` cron.
The facts survive the 90-day run purge (``run_id`` is deliberately NOT a
foreign key), so dimensioned run history outlives the ``runs`` rows it was
derived from.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Index, Integer, Numeric, String, Uuid
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
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="SET NULL"))
    team_name: Mapped[str | None] = mapped_column(String(255))
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("pipelines.id", ondelete="SET NULL"))
    pipeline_name: Mapped[str | None] = mapped_column(String(255))
    folder_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("pipeline_folders.id", ondelete="SET NULL"))
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

    team: Mapped[Optional["Team"]] = relationship(foreign_keys=[team_id])
    pipeline: Mapped[Optional["Pipeline"]] = relationship(foreign_keys=[pipeline_id])
    folder: Mapped[Optional["PipelineFolder"]] = relationship(foreign_keys=[folder_id])
