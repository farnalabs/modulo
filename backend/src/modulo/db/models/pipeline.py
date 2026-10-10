import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped, SoftDeleteMixin

if TYPE_CHECKING:
    from modulo.db.models.account import Account
    from modulo.db.models.organisation import Organisation

# Repeated FK fragments (S1192).
_ONDELETE_SET_NULL = "SET NULL"
_ACCOUNTS_FK = "accounts.id"


class Pipeline(SoftDeleteMixin, OrgScoped):
    __tablename__ = "pipelines"
    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_pipelines_visibility"),
        CheckConstraint(
            "visibility = 'org' OR owner_team_id IS NOT NULL",
            name="ck_pipelines_team_owner",
        ),
        CheckConstraint("max_concurrent_runs > 0", name="ck_pipelines_max_concurrent_runs"),
        CheckConstraint(
            "lock_wait_timeout_seconds BETWEEN 30 AND 3600",
            name="ck_pipelines_lock_wait_timeout",
        ),
        CheckConstraint("node_timeout_seconds > 0", name="ck_pipelines_node_timeout"),
        # FAR-1257: per-pipeline HITL review window override (seconds). NULL =
        # no override (inherit the org default, then the instance/env default).
        # The 60..604800 envelope (1 min .. 7 days) is the SAME one Pydantic
        # enforces on the API surfaces and resolve_hitl_review_window_seconds
        # applies at resolution time — migration 0263.
        CheckConstraint(
            "hitl_review_window_seconds IS NULL OR hitl_review_window_seconds BETWEEN 60 AND 604800",
            name="ck_pipelines_hitl_review_window",
        ),
        CheckConstraint(
            "default_autonomy_level IN ('manual_approval', 'notify_on_complete', 'fully_autonomous')",
            name="ck_pipelines_autonomy_level",
        ),
        CheckConstraint(
            "max_autonomy_level IS NULL OR "
            "max_autonomy_level IN ('manual_approval', 'notify_on_complete', 'fully_autonomous')",
            name="ck_pipelines_max_autonomy_level",
        ),
        # 0264: the ordering invariant itself (levels are NOT lexicographically
        # ordered, hence the CASE rank map). A NULL ceiling always passes the
        # first arm; a NULL default makes the comparison NULL, which a CHECK
        # treats as satisfied - matching validate_autonomy_ceiling's
        # "NULL default ranks as manual_approval" fallback.
        CheckConstraint(
            "max_autonomy_level IS NULL OR "
            "(CASE default_autonomy_level "
            "WHEN 'manual_approval' THEN 0 "
            "WHEN 'notify_on_complete' THEN 1 "
            "WHEN 'fully_autonomous' THEN 2 END) <= "
            "(CASE max_autonomy_level "
            "WHEN 'manual_approval' THEN 0 "
            "WHEN 'notify_on_complete' THEN 1 "
            "WHEN 'fully_autonomous' THEN 2 END)",
            name="ck_pipelines_max_autonomy_ge_default",
        ),
        # FAR-1530: per-pipeline Paused execution state. A disabled row must
        # carry BOTH its cause and when it was set (mirrors
        # ck_organisations_triggers_paused_at); the reason vocabulary is the
        # closed set ``('operator', 'circuit_breaker')`` — the pipeline-level
        # state is deliberately distinct from the org-level
        # ``triggers_paused`` kill-switch. Migration 0286.
        CheckConstraint(
            "run_enabled OR (run_disabled_reason IS NOT NULL AND run_disabled_at IS NOT NULL)",
            name="ck_pipelines_run_enabled",
        ),
        CheckConstraint(
            "run_disabled_reason IS NULL OR run_disabled_reason IN ('operator', 'circuit_breaker')",
            name="ck_pipelines_run_disabled_reason",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("pipeline_folders.id", ondelete=_ONDELETE_SET_NULL), index=True
    )
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    # FAR-1161: accountability owners — DISTINCT from owner_team_id (the
    # tenancy/visibility boundary). Nullable: assigned per-pipeline by an
    # operator; FK SET NULL so deleting an account clears the reference
    # rather than the pipeline. Assignment is gated by the eligibility
    # invariant (active org member + team member when visibility='team') in
    # ``db.crud.pipeline_owner``.
    business_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey(_ACCOUNTS_FK, ondelete=_ONDELETE_SET_NULL), index=True
    )
    reliability_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey(_ACCOUNTS_FK, ondelete=_ONDELETE_SET_NULL), index=True
    )
    # FAR-1558: per-pipeline environment-profile binding — WHICH runtime
    # provider tier (runner_docker / e2b / kubernetes / ...) this pipeline's
    # sandbox_agent nodes dispatch on. Nullable: NULL keeps the historical
    # default route (no bound profile -> provider_type "none" -> the legacy E2B
    # path). Copied verbatim into ``PipelineSnapshot.environment_profile_id`` at
    # snapshot freeze (the column runs already read there); FK SET NULL so
    # deleting a profile unbinds rather than deletes the pipeline. Same-org
    # tenant trigger ``trg_pipelines_environment_profile_id_tenant`` (migration
    # 0289) plus a route-level eligibility check (org + owner-team visibility).
    environment_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("environment_profiles.id", ondelete=_ONDELETE_SET_NULL), index=True
    )
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    lock_wait_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    node_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    # FAR-1257: per-pipeline HITL review window override (seconds), mirroring
    # ``node_timeout_seconds``'s plumbing but NULLABLE — NULL means "no
    # override, inherit the org default then the instance/env default". The
    # chain resolves ONCE at gate fire time and is stamped as an absolute
    # ``hitl_claims.terminalize_at``. CHECK ck_pipelines_hitl_review_window.
    hitl_review_window_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600, server_default="3600")
    max_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Cost-control circuit breaker (FAR-105, spec §8.10) — per-pipeline monthly
    # spend threshold. When the pipeline's monthly accumulated spend + a new
    # run's cost would exceed ``circuit_breaker_threshold``, the breaker trips
    # (``circuit_breaker_tripped``), permanently pausing the pipeline's
    # triggers until an admin re-enables the pipeline. Migration 0086.
    circuit_breaker_threshold: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    circuit_breaker_tripped: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    circuit_breaker_tripped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # FAR-1530: per-pipeline Paused execution state — PRESENT and VISIBLE but
    # NON-EXECUTING (triggers skipped AND manual/REST/MCP refused at the
    # ``create_run`` state gate). Distinct from ``archived_at`` (hidden) and
    # from the ORG-level ``triggers_paused`` kill-switch. ``run_enabled`` is
    # the unified state; ``circuit_breaker_tripped(_at)`` above STAYS as the
    # breaker's witness — a trip folds into the unified state only when the
    # pipeline is not already disabled, and an admin reset clears the unified
    # state only when its reason is ``'circuit_breaker'`` (an operator pause
    # survives a reset). CHECKs: ck_pipelines_run_enabled (a disabled row
    # carries cause + timestamp) and ck_pipelines_run_disabled_reason (closed
    # reason vocabulary). Migration 0286.
    run_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    run_disabled_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    run_disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_context_defaults: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    default_autonomy_level: Mapped[str | None] = mapped_column(String(30), server_default="manual_approval")
    # FAR-1163 S0: hard ceiling on the autonomy level any HITL-gate resolution
    # may reach. NULL = effective ceiling is default_autonomy_level (a
    # context-setter recommendation can then only LOWER autonomy). Migration
    # 0256; CHECKs ck_pipelines_max_autonomy_level (vocabulary) and
    # ck_pipelines_max_autonomy_ge_default (>= default, migration 0264) above.
    max_autonomy_level: Mapped[str | None] = mapped_column(String(30), nullable=True)
    graph_nodes_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    default_feedback_handler: Mapped[str | None] = mapped_column(String(50))
    rate_limit_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # FAR-811: pipeline-level default for sandbox stdout retention.
    # Shape: {"mode": "tail"|"full", "max_bytes": <positive int>} or None
    # (no pipeline override — inherit from org ceiling only).
    stdout_retention_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    stale_run_timeout_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default=text("'30'")
    )
    collection_install_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey(_ACCOUNTS_FK, ondelete="RESTRICT"), nullable=False, index=True
    )
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    organisation: Mapped["Organisation"] = relationship()
    creator: Mapped["Account"] = relationship(foreign_keys=[account_id])
