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
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression
from sqlalchemy.sql.compiler import SQLCompiler

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.pipeline_snapshot import PipelineSnapshot
    from modulo.db.models.team import Team


# Single source of truth for run status sets (ADR 020 / dist/runtime-core A1).
# Both are subsets of the ``ck_runs_status`` CHECK-constraint values. The
# never-entered ``waiting_for_lock`` sub-state was excised in migration 0074/0075
# (rows backfilled to ``pending``); it MUST NOT appear in either set. Consumers
# across the codebase (crud/run, cron_helpers, analytics) import these instead
# of re-declaring their own tuples.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "complete",
        "failed",
        "cancelled",
        "eval_failed",
        "stalled",
        "budget_exceeded",
        "router_no_match",
        "cost_ceiling_exceeded",
        "compensation_failed",
    }
)

# Non-terminal (active) run statuses — a run that still holds a slot. A pending
# run is active but does not hold capacity (see crud.run._active_run_statuses).
# ``unknown`` (adopted from FAR-410) is a NON-TERMINAL recovery status: the run's
# outcome could not be determined (e.g. the sandbox was lost) but it is not
# finalised; it holds a slot until an operator re-runs it with the SAME persisted
# run-level ``idempotency_key``, reconciling it to a terminal outcome.
ACTIVE_RUN_STATUSES: frozenset[str] = frozenset({"pending", "running", "awaiting_human", "claimed", "unknown"})

# In-flight run statuses for the ``ongoing`` trigger type (FAR-158). An ongoing
# trigger keeps its pipeline topped up to ``max_concurrent_runs`` runs whose
# status is in this set. pending = "queued" (the user-facing semantics — a
# queued run counts toward the target because it will claim a slot shortly).
# ``awaiting_human`` is DELIBERATELY EXCLUDED: a never-answered HITL gate must
# not permanently starve the pool (verified: claim expiry resets the claim but
# the run stays ``awaiting_human``; dispatcher_reconcile only resumes an
# ``awaiting_human`` run when a committed decision exists), so a run parked on a
# human must not count against the target. This set is what separates the
# ongoing top-up count (``cron_helpers._count_ongoing_runs``) from the general
# ``_count_active_runs``. ``unknown`` is deliberately EXCLUDED: a stuck UNKNOWN
# run must not trigger an ongoing top-up (it is not a fresh unit of work).
ONGOING_ACTIVE_STATUSES: frozenset[str] = frozenset({"pending", "running", "claimed"})


class _GenRandomUuid(expression.FunctionElement[str]):
    """Dialect-portable server_default for ``runs.claim_token``.

    Migration 0074 makes ``claim_token`` NOT NULL with a
    ``gen_random_uuid()::text`` server_default on Postgres. The ORM model
    mirrors that so ORM-created schemas (unit tests on in-memory SQLite, dev
    mode) stay valid: Postgres renders the native function, SQLite falls back
    to ``hex(randomblob(16))`` (a valid 32-char hex UUID).
    """

    type = String(128)
    inherit_cache = True


@compiles(_GenRandomUuid)
def _compile_postgres_default(_element: _GenRandomUuid, _compiler: SQLCompiler, **kw: Any) -> str:
    return "gen_random_uuid()::text"


@compiles(_GenRandomUuid, "sqlite")
def _compile_sqlite_default(_element: _GenRandomUuid, _compiler: SQLCompiler, **kw: Any) -> str:
    return "lower(hex(randomblob(16)))"


class Run(OrgScoped):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', "
            "'correction', 'slack_app_mention')",
            name="ck_runs_trigger_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'awaiting_human', 'claimed', 'unknown', "
            "'complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', "
            "'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed')",
            name="ck_runs_status",
        ),
        UniqueConstraint("organisation_id", "run_number", name="uq_runs_org_run_number"),
        # Probe sample query (organisation_id, started_at) — migration 0066.
        Index("ix_runs_probe", "organisation_id", "started_at"),
        # Per-trigger daily-spend-limit enforcement readers (cron_helpers /
        # polling) + billing overview — org_id + created_at. Migration 0066.
        # The cost-controller refusal SUM reads the ledger, NOT runs (0066).
        Index("ix_runs_refusal", "organisation_id", "created_at"),
        # Per-pipeline trigger rate-limit backstop (migration 0117 / #1105) —
        # one active run per (pipeline, rate_limit_key). create_run admits
        # atomically and translates the IntegrityError to a rate-limit error.
        Index(
            "uq_runs_pipeline_rate_limit_key",
            "pipeline_id",
            "rate_limit_key",
            unique=True,
            postgresql_where=text("rate_limit_key IS NOT NULL"),
        ),
        # FAR-332 batch-scoped variant comparison — one batch = one batch_id
        # shared by every run fired together; the (variant_group_id, batch_id)
        # composite powers the batch compare read. Migration 0118.
        Index("ix_runs_variant_group_batch", "variant_group_id", "batch_id"),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("pipeline_snapshots.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("triggers.id", ondelete="SET NULL"), index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # FAR-410 / FAR-402 P5: the logical idempotency identity of the operator
    # re-run, derived deterministically (``<pipeline_id>:<run_number>`` +
    # node + index). An UNKNOWN run re-run by an operator reuses the SAME
    # persisted key so a write that may have reached the upstream is not
    # re-applied as a fresh operation. NULL for pre-P5 runs / runs never
    # re-run; set at create_run when the pipeline carries idempotency config.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
    rate_limit_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
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
    claim_token: Mapped[str] = mapped_column(String(128), nullable=False, server_default=_GenRandomUuid())
    # Enqueue-failure audit timestamp (migration 0074) — set when a SAQ
    # dispatch enqueue fails so dispatcher_reconcile can fail the run.
    enqueue_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sandbox dispatch lifecycle state (migration 0074) — the persistent handle
    # dispatch.py reads to resume/retry a sandbox_agent node after a crash.
    sandbox_dispatch_state: Mapped[str | None] = mapped_column(Text)
    # E2B sandbox id surfaced for observability (migration 0074).
    sandbox_id: Mapped[str | None] = mapped_column(Text)
    outputs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # FAR-152 work_intact (migration 0091) — computed at terminalization by the
    # executor from completed-node artifacts + the full DAG (``evidence.compute_work_intact``)
    # and written via a fenced raw UPDATE (``executor._apply_work_intact``). Mapped on
    # the ORM so the FAR-189 classifier can record it as metadata (the old
    # ``getattr(run, "work_intact", None)`` never observed the column).
    work_intact: Mapped[bool | None] = mapped_column(Boolean)
    # Per-node telemetry (status, wall_clock_time_ms, exit_code, ...) split out
    # of outputs_json by the Agent Return Contract (FAR-125). NULL for
    # pre-split runs. Written atomically with outputs_json (migration 0074).
    node_telemetry_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # FAR-188 raw-output retention markers (migration 0099) — dedicated JSONB
    # column keyed by attempt_key, holding raw sandbox output retained when a
    # sandbox_agent node's output.json fails to parse or the command
    # stalls/times out. DELIBERATELY separate from outputs_json /
    # node_telemetry_json so the Agent Return Contract columns stay clean: the
    # node-output endpoint can never serve raw stdout, recover_node's
    # already-completed guard never sees a fake completed node, and finalize's
    # split-output machinery never touches the marker. Generic JSON here for
    # SQLite/MariaDB parity (the work_item_refs precedent, migration 0083).
    raw_output_markers: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # FAR-189 run-outcome classification (migration 0100) — JSONB column with
    # shape {value, reason, delivered_pr_urls, computed_at, work_intact,
    # declared_success_nodes}. UNIQUE(run_id) is the runs PK; the record is
    # written atomically with terminalization by the shared fenced terminal
    # write (crud/run) and refreshed (upsert) on re-terminalization. Generic
    # JSON here for SQLite/MariaDB parity (the raw_output_markers precedent).
    run_classification: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # FAR-213 blocked-partial summary (migration 0111) — structured
    # run-termination compensation record written when a run terminalizes
    # ``eval_failed``/``eval_blocked`` from a guardrail block: executed nodes
    # (in order), per-node publish status (published/compensated/
    # not-compensated), output references (never duplicated raw payloads), and
    # per-attempt compensation outcomes. Generic JSON for SQLite/MariaDB
    # parity (the run_classification precedent).
    blocked_partial_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # FAR-223 item 11 guardrail_summary telemetry (migration 0113) — a
    # point-in-time snapshot of the guardrail interception written at
    # create_run when guardrails ran; NULL otherwise. Shape:
    # {bound, evaluated, passed, violated, observed, errored, redacted,
    # skipped, expected_skips, unexpected_skips}. Generic JSON for
    # SQLite/MariaDB parity (the run_classification precedent).
    guardrail_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Journey / work-item tracking (FAR-142, migration 0083) — additive,
    # nullable, never backfilled. ``work_item_id`` is the chain anchor written
    # ONCE at create (floor id or adopted from the parent run) and NEVER
    # mutated; ``work_item_refs`` is a JSON array of {kind, ref, source,
    # status?} entries (JSONB in the migration for the partial GIN index;
    # generic JSON here keeps SQLite/MariaDB parity — the
    # hitl_claims.decision_payload precedent). ``is_replay`` is set by
    # replay_event; ``variant_group_id`` by run_variant_weighted.
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    work_item_refs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    is_replay: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    variant_group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("variant_groups.id", ondelete="SET NULL")
    )
    # FAR-332 batch-scoped variant comparison (migration 0118). Every run fired
    # together in one ``run_variant_batch`` shares the same ``batch_id``; the
    # compare route loads runs purely by batch_id (never a live group), so
    # soft-deleting the group does not break comparison.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    # Frozen snapshot/override capture at fire time (FAR-332 3c) — the single
    # source of truth for "which input each variant ran with". Shape:
    # {variant_id, variant_name, snapshot_id, run_context_overrides, batch_id}.
    # The compare view reads this, never the live snapshot, so later edits to
    # the variant group cannot rewrite history.
    variant_config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    organisation: Mapped["Organisation"] = relationship()
    pipeline: Mapped["Pipeline"] = relationship()
    snapshot: Mapped["PipelineSnapshot"] = relationship()
    owner_team: Mapped[Optional["Team"]] = relationship()
