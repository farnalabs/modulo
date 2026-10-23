import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
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

from modulo.db.models.base import JSONB_COL, JSONB_COL_NONE_AS_NULL, ONDELETE_SET_NULL, OrgScoped

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
        # FAR-1487: a HITL rejection with no reject route ENDS the run here
        # (neither ``failed`` — nothing broke — nor ``cancelled`` — no operator
        # stopped it). Also the home of coalesced-supersede system rejections
        # (error_code ``hitl.superseded``).
        "rejected",
    }
)

# Non-terminal (active) run statuses — a run that still holds a slot. A pending
# run is active but does not hold capacity (see crud.run._active_run_statuses).
# ``unknown`` (adopted from FAR-410) is a NON-TERMINAL recovery status: the run's
# outcome could not be determined (e.g. the sandbox was lost) but it is not
# finalised; it holds a slot until an operator re-runs it with the SAME persisted
# run-level ``idempotency_key``, reconciling it to a terminal outcome.
# ``hitl_parked`` (FAR-604 D2) is a NON-TERMINAL parked state: the run's HITL
# gate expired unanswered past the grace window and the park sweep moved it out
# of ``awaiting_human`` so it stops occupying review state. It is deliberately
# kept in this set (not a terminal status) so every existing consumer that
# treats ``awaiting_human`` as in-flight treats a parked run identically — the
# ONE exception is the pipeline capacity gate below, which excludes both.
ACTIVE_RUN_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "awaiting_human", "claimed", "unknown", "hitl_parked"}
)

# ---------------------------------------------------------------------------
# FAR-1233 — WHY a run was cancelled. The CLOSED vocabulary behind
# ``runs.cancel_reason``, enforced at the DB by ``ck_runs_cancel_reason``
# (created by migration 0260; the constraint text is maintained by migration
# 0275_run_cancel_reason_vocabulary, which reconciled it to this vocabulary
# after FAR-1104 renamed the values).
#
# ``NULL`` is a first-class value: every run cancelled BEFORE 0260 has no
# reason recorded and the run detail page renders a neutral
# "reason not recorded" fallback rather than guessing.
#
# Adding a cause means adding the constant here, widening the CHECK value in
# ``ck_runs_cancel_reason`` via a NEW migration (the constraint text ships in
# migration 0275 — never edit a shipped migration; migration-owned, per the
# repo parity rule: the DB-side vocabulary backstop is not duplicated in the
# ORM), and the write site — together, never as a follow-up.
# ---------------------------------------------------------------------------
CANCEL_REASON_USER_REQUESTED: Final[str] = "user_requested"
CANCEL_REASON_AGENT_REQUESTED: Final[str] = "agent_requested"
CANCEL_REASON_HITL_REVIEW_EXPIRED: Final[str] = "hitl_review_expired"
CANCEL_REASON_HITL_REVIEW_MISSING: Final[str] = "hitl_review_missing"
CANCEL_REASON_VALUES: frozenset[str] = frozenset(
    {
        CANCEL_REASON_USER_REQUESTED,
        CANCEL_REASON_AGENT_REQUESTED,
        CANCEL_REASON_HITL_REVIEW_EXPIRED,
        CANCEL_REASON_HITL_REVIEW_MISSING,
    }
)

# ``runs.cancelled_by`` sentinel for a system-owned cancellation (a watchdog /
# terminalizer, or a caller with no authenticated account in scope). Every
# other value is the acting account id rendered as text.
CANCELLED_BY_SYSTEM: Final[str] = "system"

# ---------------------------------------------------------------------------
# FAR-1141 / ADR-042 — WHERE the run's work was executed. The CLOSED
# vocabulary behind ``runs.execution_origin`` (migration 0288), single-sourced
# here so the write site (``crud.run.create_run``), the readers (runs API,
# ``run_daily_facts`` copy) and any future consumer share ONE spelling.
#
#   * EXECUTION_ORIGIN_DISPATCHED — the run's frozen snapshot graph contains
#     at least one ``dispatch`` node, i.e. part of the work is executed
#     OUTSIDE Modulo and merely witnessed/triggered by it.
#   * ``NULL`` — executed by Modulo, OR the run predates this column (legacy).
#     Existing rows are deliberately never backfilled (ADR 042: existing runs
#     keep their current provenance unchanged).
#
# No DB CHECK constraint backs this yet (see migration 0288): with exactly one
# member and every write site importing the constant, a constraint would cost
# a full-table validation scan on ``runs`` for no additional safety. Adding a
# value means adding it here AND widening any future constraint in the same
# change.
# ---------------------------------------------------------------------------
EXECUTION_ORIGIN_DISPATCHED: Final[str] = "dispatched"
EXECUTION_ORIGIN_VALUES: frozenset[str] = frozenset({EXECUTION_ORIGIN_DISPATCHED})

# ---------------------------------------------------------------------------
# FAR-153 / §15.1 (agent-failure UX v6) — WHY a run is an attempt of another.
# The CLOSED vocabulary behind ``runs.run_group_kind``:
#
#   * RUN_GROUP_KIND_RETRY — a retry attempt: a CHILD row whose graph runs
#     CLEAN on a fresh ``langgraph_thread_id`` (the default ``create_run``
#     thread; no override).
#   * RUN_GROUP_KIND_RESUME — a resume attempt: a CHILD row that inherits
#     its parent's ``langgraph_thread_id`` via ``create_run(thread_id=...)``
#     so it continues the parent's checkpoint.
#   * ``NULL`` — a root run, or a signal / correction / work-item child
#     (those keep their existing rollup behaviour; the new grouping logic
#     filters ``run_group_kind IN ('retry','resume')``).
#
# Like ``EXECUTION_ORIGIN_VALUES`` above, no DB CHECK backs this yet (§15.12
# schema diff lists no constraint): a CHECK validated against the populated
# ``runs`` table costs a full-table scan for no safety the write site does
# not already provide — ``crud.run.create_run`` refuses any value outside
# this set at the persist boundary. Adding a value means adding it here AND
# widening any future constraint in the same change.
# ---------------------------------------------------------------------------
RUN_GROUP_KIND_RETRY: Final[str] = "retry"
RUN_GROUP_KIND_RESUME: Final[str] = "resume"
RUN_GROUP_KIND_VALUES: frozenset[str] = frozenset({RUN_GROUP_KIND_RETRY, RUN_GROUP_KIND_RESUME})


# The canonical status literal (FAR-604 qa F15): every write site that names the
# parked status binds/compares against this constant so a rename or a typo can
# never desynchronise the park sweep, the un-park transitions, and the guard.
HITL_PARKED_STATUS: Final[str] = "hitl_parked"
AWAITING_HUMAN_STATUS: Final[str] = "awaiting_human"

# FAR-604 D1 (HITL capacity): statuses that hold a PIPELINE execution slot —
# the set the pipeline-level ``max_concurrent_runs`` admission gate counts
# (``count_active_runs_for_pipeline(..., include_pending=False)``).
# ``awaiting_human`` and ``hitl_parked`` are DELIBERATELY EXCLUDED: a run parked
# on a human decision is not executing (the 2026-09-04 incident: 20
# awaiting_human runs consumed a 20-cap pipeline for 26h). The ORG-level
# ``run_concurrency_limit`` gate still counts them (it uses
# ``ACTIVE_RUN_STATUSES - {'pending'}``), so parked runs remain org-bounded.
# Derived (qa F15): subtracting from ACTIVE_RUN_STATUSES keeps the two sets from
# drifting apart when a status is added to the active vocabulary.
PIPELINE_CAPACITY_STATUSES: frozenset[str] = frozenset(
    ACTIVE_RUN_STATUSES - {"pending", AWAITING_HUMAN_STATUS, HITL_PARKED_STATUS}
)

# Run statuses under which an undecided HITL gate is actionable work (FAR-612).
# ``awaiting_human`` gates are claimable now; ``claimed`` gates are held by a
# reviewer and legitimately render as claimed. ``hitl_parked`` is a non-terminal
# parked state (FAR-604 D2) whose gate stays OPEN AND CLAIMABLE — park != decide,
# so a parked run's gate remains undecided work until a decision un-parks it via
# ``dispatcher_reconcile``. Every other status makes an undecided gate data rot
# (e.g. orphaned rows left by the since-fixed auto-approve bug), so both
# pending-gate surfaces (REST org-wide queue and MCP ``list_pending_hitl``) filter
# to this exact set.
HITL_ACTIONABLE_RUN_STATUSES: frozenset[str] = frozenset({"awaiting_human", "claimed", HITL_PARKED_STATUS})

# Run statuses under which a claim may atomically ACQUIRE a gate (FAR-645).
# Single source of truth for ``HITLManager.claim()``: the claimable set is
# ``awaiting_human`` (claimable now) and ``hitl_parked`` (stays claimable per
# FAR-604 D2 — park != decide, a parked run's gate remains undecided work until
# a decision un-parks it). Deliberately distinct from
# ``HITL_ACTIONABLE_RUN_STATUSES`` (which also contains ``claimed`` for listing
# semantics): a claimed gate is HELD by a reviewer — claim() extends this set
# with ``claimed`` ONLY for the same-account re-claim arm (FAR-686 token
# recovery), mirrored atomically in the UPDATE's ``runs`` EXISTS predicate.
# claim() uses the base set in its fast-fail pre-check AND in the atomic
# UPDATE's ``runs`` EXISTS predicate, so a run that goes terminal between the
# pre-check and the write can never be claimed.
HITL_CLAIMABLE_RUN_STATUSES: frozenset[str] = frozenset({AWAITING_HUMAN_STATUS, HITL_PARKED_STATUS})

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
            "'correction', 'slack_app_mention', 'rerun')",
            name="ck_runs_trigger_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'awaiting_human', 'claimed', 'unknown', 'hitl_parked', "
            "'complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', "
            "'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed', 'rejected')",
            name="ck_runs_status",
        ),
        UniqueConstraint("organisation_id", "run_number", name="uq_runs_org_run_number"),
        # Probe sample query (organisation_id, started_at) — migration 0066.
        Index("ix_runs_probe", "organisation_id", "started_at"),
        # Per-trigger daily-spend-limit enforcement readers (cron_helpers /
        # polling) + billing overview — (organisation_id, created_at). This is
        # served by ix_runs_org_created_pipeline (organisation_id, created_at)
        # INCLUDE (pipeline_id) declared below; the dedicated ix_runs_refusal
        # index was intentionally dropped by migration 0197_runs_index_and_constraint_fixes
        # because it was a strict prefix and only doubled write amplification.
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
        # Runs-page list total COUNT (migration 0171) — the list's count query
        # filters org + join pipelines (needs pipeline_id per run). The INCLUDE
        # column keeps that COUNT index-only (no heap fetch per run) without
        # widening the (organisation_id, created_at) key that also serves the
        # created_at DESC page ordering. Active-run counts (org/pipeline +
        # status IN (...)) are already served by migration 0155's
        # ix_runs_organisation_status / ix_runs_pipeline_status, which — like
        # 0155's other hot-query indexes — are intentionally NOT declared here;
        # a duplicate org+status index would tax the hottest write path of the
        # biggest table (status is updated on every run transition).
        Index(
            "ix_runs_org_created_pipeline",
            "organisation_id",
            "created_at",
            postgresql_include=["pipeline_id"],
        ),
        # Error-code analytics (migration 0202) — error_tracking.py:345-349
        # filters on (organisation_id, error_code IN capacity markers) and the
        # RLS-scoped failure-reason breakdown (crud/run.py:3168-3180) filters
        # error_code IS NOT NULL and groups by it.
        Index(
            "ix_runs_org_error_code",
            "organisation_id",
            "error_code",
            postgresql_where=text("error_code IS NOT NULL"),
        ),
        # FAR-2055: the compliance evidence index counts
        # ``count(DISTINCT snapshot_id)`` over an org
        # (compliance/evidence_index.py). The single-column organisation and
        # snapshot indexes cannot answer that without a heap fetch per run;
        # this composite makes the aggregate an index-only scan. ``snapshot_id``
        # is immutable on a run, so the write cost is one narrow entry per run
        # creation (not one per status transition). Mirrors migration 0326.
        Index("ix_runs_org_snapshot_id", "organisation_id", "snapshot_id"),
        # FAR-1438 — workspace-input drift compensating sweep
        # (core/cron_helpers.py::_sweep_workspace_input_drift_flags): its
        # bounded SELECT filters status IN (TERMINAL_STATUSES) AND
        # workspace_inputs_drift_detected IS NULL, ORDER BY id, LIMIT 200 and
        # runs every reconcile tick (60s). Migration 0238 added the column
        # with no index, so once the historical NULL set drains the planner
        # seq-scans the whole runs table per tick. The partial predicate is
        # the sweep's WHERE VERBATIM (both conjuncts), so every entry already
        # passes the filter and the (id) key serves ORDER BY id as a plain
        # ordered scan stopping at LIMIT 200. Migration 0278; parity with the
        # sweep predicate is guarded by
        # tests/unit/db/test_migration_0278_runs_workspace_drift_sweep_index.py.
        # FAR-1487 (migration 0284) re-created it with ``rejected`` in the IN
        # list so the widened TERMINAL_STATUSES sweep still matches the predicate.
        Index(
            "ix_runs_workspace_drift_sweep",
            "id",
            postgresql_where=text(
                "status IN ('complete', 'failed', 'cancelled', 'eval_failed', 'stalled', "
                "'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed', 'rejected') "
                "AND workspace_inputs_drift_detected IS NULL"
            ),
            sqlite_where=text(
                "status IN ('complete', 'failed', 'cancelled', 'eval_failed', 'stalled', "
                "'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed', 'rejected') "
                "AND workspace_inputs_drift_detected IS NULL"
            ),
        ),
        # FAR-153 / §15.1 — the two partial unique indexes that REPLACE the
        # former global UNIQUE(langgraph_thread_id) (dropped by migration
        # 0302_runs_run_grouping):
        #
        # 1. Exactly ONE resume child per parent: a second resume of the same
        #    stalled parent is rejected at the DB (409 at the API), while a
        #    resume child that itself stalls can be resumed again because its
        #    parent is a DIFFERENT row — so resume-of-resume chains work.
        Index(
            "uq_runs_resume_parent",
            "parent_run_id",
            unique=True,
            postgresql_where=text("run_group_kind = 'resume'"),
            sqlite_where=text("run_group_kind = 'resume'"),
        ),
        # 2. Global thread uniqueness for every NON-resume row (roots,
        #    retries, signal/correction/work-item children, legacy rows):
        #    only a resume child may share its parent's thread, and only to
        #    continue that parent's checkpoint — the "only their parent's"
        #    half is enforced by ``crud.run.create_run``, which reads the
        #    parent and rejects a resume whose thread_id is not the
        #    parent's langgraph_thread_id (the indexes below exclude resume
        #    rows, so they cannot make that check themselves).
        #    ``IS NOT`` on SQLite has the
        #    same null-safe semantics as ``IS DISTINCT FROM`` on Postgres
        #    (NULL kind IS NOT 'resume' -> true), so both dialects index
        #    exactly the rows the spec names.
        Index(
            "uq_runs_thread_non_resume",
            "langgraph_thread_id",
            unique=True,
            postgresql_where=text("run_group_kind IS DISTINCT FROM 'resume'"),
            sqlite_where=text("run_group_kind IS NOT 'resume'"),
        ),
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
        Uuid(), ForeignKey("triggers.id", ondelete=ONDELETE_SET_NULL), index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # FAR-1141 / ADR-042: the run-level execution origin — ONE of
    # EXECUTION_ORIGIN_VALUES, or NULL for "executed by Modulo / recorded
    # before this shipped". Stamped once by ``crud.run.create_run`` from the
    # run's frozen snapshot graph (never the live pipeline) and never
    # rewritten afterwards, so it survives re-claims and re-dispatches the
    # same way ``node_deadline_watchdog_fired_count`` does (neither the atomic
    # claim SQL nor the fenced pending-reset names this column).
    # Nullable + no server default (migration 0288): additive, no table
    # rewrite, existing rows untouched. API-projected on the runs list item
    # and the run detail response — it is the claim-ready surface ADR-042
    # requires to distinguish the two origins.
    execution_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="pending")
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("runs.id", ondelete=ONDELETE_SET_NULL), nullable=True, index=True
    )
    # FAR-153 / §15.1 — run-group identity. ``run_group_id`` is the ROOT run
    # id of the retry/resume group this attempt belongs to (indexed: group
    # queries read WHERE run_group_id = :root). NULL for roots, for signal /
    # correction / work-item children (their rollup behaviour is unchanged),
    # and for legacy rows until the FAR-147 backfill classifies them — never
    # backfilled here (additive column, no table rewrite; 0288 precedent).
    # No FK: the root row may itself be purged by retention while a younger
    # attempt survives, and a dangling group id is the intended read
    # (grouping is by value, not by join). ``run_group_kind`` names WHY this
    # row joined the group — one of RUN_GROUP_KIND_VALUES or NULL — and is
    # what the two partial unique indexes in ``__table_args__`` key on.
    run_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    run_group_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    # account_id (FAR-1443): deliberately NOT indexed — no query filters this
    # column (it is written at create and read for display only; the sole
    # WHERE against it is by primary key), so migration 0283 drops
    # ix_runs_account_id. Re-add index=True only together with a query that
    # actually predicates on it.
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("accounts.id", ondelete=ONDELETE_SET_NULL))
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
    # FAR-1233 cancellation transparency: WHY and WHO. ``cancel_reason`` is one
    # of CANCEL_REASON_VALUES (DB backstop ``ck_runs_cancel_reason``, migration
    # 0260, vocabulary text maintained by 0275); NULL for every run cancelled
    # before 0260 — the API serves NULL
    # as-is and the UI renders a neutral "reason not recorded" fallback.
    # ``cancelled_by`` is the acting account id (text) or CANCELLED_BY_SYSTEM.
    # Nullable and additive: existing rows and existing API consumers are
    # unaffected.
    cancel_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    # FAR-1463: absolute node-deadline watchdog (FAR-369) FIRINGS — the durable
    # fingerprint that makes a watchdog firing observable in analytics WITHOUT
    # log access. Incremented on EVERY firing by
    # ``pipeline_execution._fail_overdue_node`` BEFORE the shared retry consult,
    # so BOTH outcomes are counted: the re-dispatch (which otherwise leaves ZERO
    # analytics fingerprints, because the fenced pending-reset nulls
    # ``error_code`` — FAR-1423) and the terminal fail (which also carries
    # ``error_code='node_deadline_exceeded'``). Copied onto ``run_daily_facts``
    # at finalize so the analytics read path never joins ``runs`` (ADR 020) and
    # the marker outlives the 90-day run purge.
    #
    # SURVIVES A RE-DISPATCH: neither ``_CLAIM_UPDATE_SQL`` (sets status /
    # heartbeat_at / claim_count / dispatch_phase / claim_token) nor the fenced
    # pending-reset (sets status='pending', error_code=NULL, error_detail=NULL)
    # names this column, so the count is intact on the next claim — unlike
    # ``dispatch_phase``, which every re-claim deliberately resets to 'claimed'.
    #
    # Distinctness: a node-deadline kill structurally requires an IN-FLIGHT
    # node, so ``>= 1`` proves a node was dispatched and blew its deadline (a
    # run that never dispatched a node always reads 0), while a genuine
    # terminal failure with no firing also reads 0 (``error_code`` separates
    # those two).
    #
    # INTERNAL ONLY: NOT API-projected (absent from ``RunResponse`` /
    # ``_build_list_item`` / the MCP run payloads) — the analytics surface
    # (facts export + ``query_analytics`` buckets) is the read path. NOT NULL
    # DEFAULT 0: rows that predate this migration never recorded a firing.
    node_deadline_watchdog_fired_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    total_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    # Cost breakdown — list of component snapshots (amounts as strings).
    # NULL for pre-migration runs. Migration 0066.
    cost_breakdown: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB_COL)
    # Ledger guards (migration 0066) — terminal-only spend recording (PR A2).
    ledger_written: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    ledger_refused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    node_token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB_COL)
    error_detail: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(255))
    # FAR-153 / §15.1: the GLOBAL unique constraint this column used to carry
    # (migration 0110's ``runs_langgraph_thread_id_key``) is replaced by the
    # two partial unique indexes in ``__table_args__`` — a resume child
    # INTENTIONALLY shares its parent's thread to continue the checkpoint,
    # while every other row keeps global thread uniqueness.
    langgraph_thread_id: Mapped[str] = mapped_column(String(512), nullable=False)
    # DB jsonb — 0110 created it ``json``; 0129 promoted it. JSONB_COL keeps the
    # ORM declaration honest (a bare JSON over a jsonb column binds ``col = {}``
    # as ``$1::JSON`` and Postgres raises 'operator does not exist: jsonb = json').
    input_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB_COL)
    rate_limit_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # SAQ dispatch tracking (PR B, migration 0031) — dispatcher reflects where
    # the job actually went: 'saq' iff enqueued to SAQ; NULL iff legacy (pre-PR C).
    # NOT indexed (FAR-1443): no query leads with dispatcher — it only ever
    # appears bundled with selective status/heartbeat predicates — and the
    # near-binary column distribution makes it a planner-unfriendly key, so
    # migration 0283 drops ix_runs_dispatcher.
    dispatcher: Mapped[str | None] = mapped_column(String(20), nullable=True)
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
    # FAR-1728 (migration 0307): the guardrail-override resume self-heal's
    # DEDICATED discriminator — a BOOLEAN column, never a sentinel string in
    # the observation column ``error_detail`` (which is exposed on REST/MCP
    # responses; a healthy overridden run must not carry a control-flow marker
    # on the wire). SINGLE WRITER: only ``recovery.guardrail_override``'s flip
    # sets it, and only for a node-started (``first_node_dispatched``)
    # eval-blocked run — in the same atomic optimistic UPDATE that flips the
    # status. SINGLE CLEARER: the resume claim
    # (``pipeline_execution._RESUME_CLAIM_UPDATE_SQL`` / ``..._WITH_TOKEN``)
    # clears it in the same statement that stamps
    # ``dispatch_phase='claimed'``, so a successful claim takes the row out of
    # the self-heal class and clears the flag in one write. The zero-node
    # override flip (``pending`` — nothing to resume, re-dispatched
    # ``execute_run``) never sets it. INTERNAL ONLY: not API-projected.
    # NOT NULL DEFAULT false (server_default in the migration): pre-existing
    # rows read false, no backfill. Inert on a terminal row left behind by the
    # age gate (the flag is only consulted together with the
    # ``running`` + ``first_node_dispatched`` + stale-heartbeat shape) — the
    # claim-clear covers the healthy path; a wedged row's lingering flag is
    # never read and never surfaces.
    guardrail_override_resume_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Sandbox dispatch lifecycle state (migration 0074) — the persistent handle
    # dispatch.py reads to resume/retry a sandbox_agent node after a crash.
    # D8 (FAR-594) JSON vocabulary (written by ``runner_capacity`` /
    # ``node_runner``; consumers read ONLY ``state``/``attempt_key``):
    #   * dispatch marker  — {"state": "dispatching", "attempt_key": …,
    #     "provider": "runner_docker"|"e2b"|"local", "written_at": ISO-8601}
    #     — the D8 atomic dispatch gate commits it as the slot reservation
    #     (provider/written_at may be absent on tier-less writers, which
    #     attribute to the Docker tier — fail-safe);
    #   * script lease     — {"state": "script_executing", "attempt_key": …,
    #     "provider": …, "written_at": …} — the exactly-once fence; the
    #     reconciliation sweep NEVER clears it;
    #   * HITL tombstone   — {"state": "cleared_at_hitl", "written_at": …} —
    #     written at the interrupt boundary (capacity-neutral: the D8 count is
    #     running-only AND excludes this state; visible to the rollback
    #     detector).
    # Pre-D8 values (bare "dispatching" literals, JSON without
    # provider/written_at) count as Docker-tier and age out via the sweep.
    sandbox_dispatch_state: Mapped[str | None] = mapped_column(Text)
    # E2B sandbox id surfaced for observability (migration 0074).
    sandbox_id: Mapped[str | None] = mapped_column(Text)
    # FAR-1088 dispatch-phase instrumentation (migration 0273) — INTERNAL ONLY:
    # NOT API-projected (absent from RunResponse / _build_list_item / MCP
    # payloads). Records which phase of the dispatch pipeline the run last
    # entered, for diagnosing claimed-but-nodeless runs.
    #   * ``dispatch_phase`` — the phase label ('claimed', later
    #     loading_setup/setup_complete/streaming/...); NULL for runs claimed
    #     before this shipped.
    #   * ``dispatch_phase_entered_at`` — when that phase was entered.
    # The floor write is the claim itself: ``pipeline_execution`` stamps
    # 'claimed' inside the atomic claim UPDATE, so a run that was claimed can
    # never lack a phase even if no node ever ran.
    dispatch_phase: Mapped[str | None] = mapped_column(Text)
    dispatch_phase_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # FAR-1714: the full durable phase timeline the two columns above can only
    # hold the LAST edge of. Append-only JSONB array of
    # ``{"phase": ..., "entered_at": <ISO-8601 timestamptz>}`` — one entry per
    # durable phase write (``_PHASE_UPDATE_SQL``), PLUS one ``"claimed"``
    # entry appended by each of the four claim statements (execute/resume,
    # plain/token), so:
    #   * every attempt's claim time is recoverable (the scalar floor is
    #     overwritten by the later phases),
    #   * the ``claimed`` entries are the ATTEMPT BOUNDARIES — split the
    #     array on them to decompose claim→first-node per attempt;
    #   * entries are CUMULATIVE across re-claims, never reset — the wedged
    #     prior attempt is exactly what incident forensics need.
    # Entries are non-decreasing in ``entered_at`` (the monotonic guard on
    # ``_PHASE_UPDATE_SQL`` pairs every append with ``entered_at=now()``).
    # INTERNAL ONLY: not API-projected (like ``dispatch_phase`` above).
    # NULL for rows that predate migration 0308.
    dispatch_phase_history: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB_COL,
        nullable=True,
    )
    # FAR-1714 / FAR-1511: how many durable phase writes this run ABANDONED
    # at the 2 s bound (``PHASE_WRITE_TIMEOUT_SECONDS`` — timeout or DB
    # error, ``DispatchPhaseWriter._drain``'s arms). The durable saturation
    # signal: prod showed 10-13 drops/min, previously visible only in
    # ephemeral worker logs. NOT reset by a re-claim (neither
    # ``_CLAIM_UPDATE_SQL`` nor the fenced pending-reset names it — the drops
    # are an observation about the DATABASE, cumulative for the run's whole
    # life, additive-only so no attempt can stomp another's count). NOT
    # claim-token fenced for the same reason. NOT API-projected; aggregate
    # with ``SUM(dispatch_phase_write_drops)`` over runs for the org-level
    # saturation view. NOT NULL DEFAULT 0: rows that predate migration 0308
    # never recorded a drop.
    dispatch_phase_write_drops: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # FAR-583 B1: the legacy blob columns (outputs_json / node_telemetry_json /
    # raw_output_markers) are NO LONGER mapped on the ORM — the per-node store
    # `run_node_outputs` is the single blob surface (crud.run_node_outputs).
    # The columns still EXIST in the database until migration 0194 (B2b), so
    # the EMPTY/MISMATCH fallback readers in crud/run_node_outputs keep
    # reading them via raw parameterised SQL. Do not re-add these mappings:
    # the B1 zero-refs architecture test fails any ORM attribute reference.
    # FAR-152 work_intact (migration 0091) — computed at terminalization by the
    # executor from completed-node artifacts + the full DAG (``evidence.compute_work_intact``)
    # and written via a fenced raw UPDATE (``executor._apply_work_intact``). Mapped on
    # the ORM so the FAR-189 classifier can record it as metadata (the old
    # ``getattr(run, "work_intact", None)`` never observed the column).
    work_intact: Mapped[bool | None] = mapped_column(Boolean)
    # Per-node telemetry (status, wall_clock_time_ms, exit_code, ...) was split
    # out of outputs_json by the Agent Return Contract (FAR-125) into the
    # DROPPED legacy column node_telemetry_json, and now lives in the
    # `run_node_outputs` store (see the mapping note above). FAR-189 run-outcome
    # shape {value, reason, delivered_pr_urls, computed_at, work_intact,
    # declared_success_nodes, pr_url_provenance, delivery_confidence}.
    # delivered_pr_urls are agent-reported by the run's own output and
    # UNVERIFIED against an SCM; pr_url_provenance records how each URL was
    # harvested and delivery_confidence states that plainly (agent_reported,
    # FAR-1336/FAR-1388 — rows written before the FAR-1388 rename carry the
    # deprecated pre-rename spelling, are never backfilled, and readers
    # tolerate both). The eight-key shape is forward-only: rows written before
    # FAR-1336 are six-key and are never backfilled, so readers must treat an
    # absent pr_url_provenance/delivery_confidence key as legacy/unknown, not
    # an error. UNIQUE(run_id) is the runs PK; the record is
    # written atomically with terminalization by the shared fenced terminal
    # write (crud/run) and refreshed (upsert) on re-terminalization. Physical
    # jsonb since 0110; JSONB_COL keeps the ORM declaration honest while
    # preserving SQLite/MariaDB parity.
    run_classification: Mapped[dict[str, Any] | None] = mapped_column(JSONB_COL)
    # FAR-213 blocked-partial summary (migration 0111) — structured
    # run-termination compensation record written when a run terminalizes
    # ``eval_failed``/``eval_blocked`` from a guardrail block: executed nodes
    # (in order), per-node publish status (published/compensated/
    # not-compensated), output references (never duplicated raw payloads), and
    # per-attempt compensation outcomes. JSONB in the 0111 migration;
    # JSONB_COL keeps the ORM declaration honest while preserving
    # SQLite/MariaDB parity.
    blocked_partial_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB_COL)
    # FAR-223 item 11 guardrail_summary telemetry (migration 0113) — a
    # point-in-time snapshot of the guardrail interception written at
    # create_run when guardrails ran; NULL otherwise. Shape:
    # {bound, evaluated, passed, violated, observed, errored, redacted,
    # skipped, expected_skips, unexpected_skips}. 0129 promoted it to jsonb;
    # JSONB_COL keeps the ORM declaration honest while preserving
    # SQLite/MariaDB parity.
    guardrail_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB_COL)
    # FAR-2054 seam-scoped guardrail coverage (migration 0325) — the exhaustive
    # per-seam list written at create_run: [{seam, state, provenance, reason}]
    # over the known seams (ingestion_edge / node_output_edge / runtime_loop).
    # ``enforced`` is only ever stored with ADR-042 ``attested`` provenance
    # (Modulo acted); ``runtime_loop`` is always ``not_applicable`` because
    # Alpha does not consume a runtime tool-event stream. JSONB in 0325;
    # JSONB_COL keeps the ORM declaration honest while preserving
    # SQLite/MariaDB parity. NULL only for runs created before this shipped.
    seam_coverage_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB_COL)
    # FAR-801: workspace-input drift detection flag — written by the audit
    # layer (record_drift) in the SAME transaction as the audit row so
    # terminalization sees the flag before it classifies the run.  NULL =
    # unknown / most-recently-no-inputs (no workspace inputs configured or
    # drift detection never ran).
    workspace_inputs_drift_detected: Mapped[bool | None] = mapped_column(Boolean)
    # Journey / work-item tracking (FAR-142, migration 0083) — additive,
    # nullable, never backfilled.  ``work_item_id`` is the chain anchor written
    # ONCE at create (floor id or adopted from the parent run) and NEVER
    # mutated; ``work_item_refs`` is a JSON array of {kind, ref, source,
    # status?} entries. Physical jsonb (0110, for the partial GIN index);
    # JSONB_COL_NONE_AS_NULL keeps the ORM declaration honest AND preserves the
    # Python-None -> SQL NULL binding this column was declared with. ``is_replay``
    # is set by replay_event; ``variant_group_id`` by run_variant_weighted.
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    work_item_refs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB_COL_NONE_AS_NULL, nullable=True)
    is_replay: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    variant_group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("variant_groups.id", ondelete=ONDELETE_SET_NULL)
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
    # the variant group cannot rewrite history. Physical jsonb since 0129;
    # JSONB_COL_NONE_AS_NULL preserves both the honest declaration and the
    # Python-None -> SQL NULL binding this column was declared with.
    variant_config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB_COL_NONE_AS_NULL, nullable=True)
    # FAR-902: schema enforcement metadata — populated at finalization from
    # per-node enforcement records.  ``schema_validator_mode`` is the mode
    # the run actually executed under (resolved from enforcement records);
    # ``schema_validation_outcome`` is the run-level aggregate outcome
    # (derived from per-attempt outcomes).  Both NULL for pre-902 runs.
    schema_validator_mode: Mapped[str | None] = mapped_column(String(30))
    schema_validation_outcome: Mapped[str | None] = mapped_column(String(40))
    organisation: Mapped["Organisation"] = relationship()
    pipeline: Mapped["Pipeline"] = relationship()
    snapshot: Mapped["PipelineSnapshot"] = relationship()
    owner_team: Mapped["Team | None"] = relationship()
