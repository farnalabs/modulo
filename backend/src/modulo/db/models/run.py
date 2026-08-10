import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.pipeline_snapshot import PipelineSnapshot
    from modulo.db.models.team import Team


# Single source of truth for terminal run statuses (ADR 020). Used by the
# analytics facts writer, the maintenance backfill, and the run purge. Must be
# a subset of the ``ck_runs_status`` CHECK-constraint values.
TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "failed", "cancelled", "eval_failed"})


class Run(OrgScoped):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'correction')",
            name="ck_runs_trigger_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'awaiting_human', 'claimed', "
            "'complete', 'failed', 'cancelled', 'eval_failed')",
            name="ck_runs_status",
        ),
        UniqueConstraint("organisation_id", "run_number", name="uq_runs_org_run_number"),
        # Probe sample query (organisation_id, started_at) — migration 0066.
        Index("ix_runs_probe", "organisation_id", "started_at"),
        # Per-trigger daily-spend-limit enforcement readers (cron_helpers /
        # polling) + billing overview — org_id + created_at. Migration 0066.
        # The cost-controller refusal SUM reads the ledger, NOT runs (0066).
        Index("ix_runs_refusal", "organisation_id", "created_at"),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("pipeline_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("triggers.id", ondelete="SET NULL"))
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"))
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"))
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Execution heartbeats + dispatch tracking (migration 0027). Used by the
    # shared claim logic and dispatcher_reconcile (SAQ, PR B-2).
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Count of REAL node-execution attempts (post capacity-check, pre-stream in
    # PipelineExecutor.execute). Bounds the NodeCancelledError retry budget —
    # distinct from claim_count, which increments on EVERY SAQ claim including
    # non-executing ones (capacity-deferral demotions, pre-node setup failures)
    # that would otherwise exhaust the retry budget (postmortem FAR-121).
    node_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    total_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    # Cost breakdown — list of component snapshots (amounts as strings).
    # NULL for pre-migration runs. Migration 0066.
    cost_breakdown: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    # Ledger guards (migration 0066) — terminal-only spend recording (PR A2).
    ledger_written: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    ledger_refused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    node_token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_detail: Mapped[str | None] = mapped_column(String(5000))
    error_code: Mapped[str | None] = mapped_column(String(255))
    langgraph_thread_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    input_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    rate_limit_key: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # SAQ dispatch tracking (PR B, migration 0031) — dispatcher reflects where
    # the job actually went: 'saq' iff enqueued to SAQ; NULL iff legacy (pre-PR C).
    dispatcher: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # SAQ job id — deterministic saq:job:{queue}:run:{id}. SAQ retries reuse it.
    saq_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # DISTINCT per-claim value (NOT saq_job_id — SAQ retries reuse saq_job_id so a
    # token identical to it could never be superseded). F3a claim-token fence.
    # NOT NULL since migration 0074 (NULLs backfilled to gen_random_uuid()::text;
    # server_default keeps old-app INSERTs legal during bluegreen cutover).
    claim_token: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=text("gen_random_uuid()::text")
    )
    # Enqueue-failure audit timestamp (migration 0074) — set when a SAQ
    # dispatch enqueue fails so dispatcher_reconcile can fail the run.
    enqueue_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sandbox dispatch lifecycle state (migration 0074) — the persistent handle
    # dispatch.py reads to resume/retry a sandbox_agent node after a crash.
    sandbox_dispatch_state: Mapped[str | None] = mapped_column(Text)
    # E2B sandbox id surfaced for observability (migration 0074).
    sandbox_id: Mapped[str | None] = mapped_column(Text)
    outputs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    organisation: Mapped["Organisation"] = relationship()
    pipeline: Mapped["Pipeline"] = relationship()
    snapshot: Mapped["PipelineSnapshot"] = relationship()
    owner_team: Mapped[Optional["Team"]] = relationship()
