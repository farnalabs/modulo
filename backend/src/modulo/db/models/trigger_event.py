import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped

# Full validation_result vocabulary. Generated into the ORM CheckConstraint SQL
# below and HARDCODED (separately) in migrations 0069, 0104, 0106 and 0176 —
# migrations never import app constants. Keep them in sync when extending the
# vocabulary.
VALIDATION_RESULT_VALUES: tuple[str, ...] = (
    "accepted",
    "passed",
    "hmac_failed",
    "schema_validation_failed",
    "deduplicated",
    "concurrency_limit_reached",
    "flood_rejected",
    "timestamp_expired",
    "validation_failed",
    "rate_limited",
    "no_match",
    "condition_met",
    "poll_error",
    "signal_fired",
    "event_type_not_accepted",
    "spend_limit_reached",
    "no_pipeline",
    "test",
    "paused",
    "auto_deactivated",
    "guardrail_blocked",
    # FAR-604: latest-wins queue coalescing folded a delivery into an
    # existing pending run (run_id points at the coalesced run).
    "coalesced",
    # FAR-604: dispatcher backpressure refused run creation (queue over
    # depth/age limits); error_detail carries the depths.
    "backpressure_skipped",
    # FAR-1144: the value-filter gate (event_filters) previously reused
    # ``event_type_not_accepted``, making the event log indistinguishable
    # from the event-type gate (accepted_events).  A distinct label lets
    # operators tell which gate rejected a delivery.
    "event_value_filter_not_accepted",
)

_TRIGGER_EVENT_VALIDATION_SQL = f"validation_result IN {tuple(VALIDATION_RESULT_VALUES)}"


class TriggerEvent(OrgScoped):
    __tablename__ = "trigger_events"
    __table_args__ = (
        CheckConstraint(
            _TRIGGER_EVENT_VALIDATION_SQL,
            name="ck_trigger_events_validation_result",
        ),
        # 0278_trigger_events_trigger_type_check — mirrors
        # ``ck_triggers_type`` on the parent ``triggers`` table. Every write
        # site stores either a literal in this vocabulary (webhook /
        # agent_signal / polling / cron / ongoing / slack_app_mention) or a
        # passthrough of ``Trigger.trigger_type`` (itself CHECK-constrained),
        # so the event log can never disagree with the trigger vocabulary.
        CheckConstraint(
            "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', 'slack_app_mention')",
            name="ck_trigger_events_trigger_type",
        ),
        # Age-based retention (FAR-167) reads ``received_at`` in a bounded
        # select-then-delete sweep (migration 0092).
        Index("ix_trigger_events_received_at", "received_at"),
        # 0277_trigger_events_listing_indexes — the event-listing hot paths
        # (``api/routes/triggers.py::list_trigger_events``,
        # ``api/routes/admin_triggers.py``, ``api/mcp_server.py``) all filter
        # ``organisation_id = $1 [AND trigger_id = $2]`` with optional
        # ``validation_result`` / ``trigger_type`` equality predicates and
        # ``ORDER BY created_at DESC, id DESC LIMIT n+1``. The pre-existing
        # single-column org/trigger indexes forced a bitmap-AND plus a sort;
        # these composites serve the filter prefix and the recency ordering.
        # Both lead on ``organisation_id`` (RLS org-isolated table, so the
        # tenant column must be the index prefix — the 0272 convention).
        Index(
            "ix_trigger_events_org_trigger_created",
            "organisation_id",
            "trigger_id",
            "created_at",
        ),
        Index(
            "ix_trigger_events_org_created",
            "organisation_id",
            "created_at",
        ),
    )

    trigger_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("triggers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    validation_result: Mapped[str] = mapped_column(String(50), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    error_detail: Mapped[str | None] = mapped_column(String(2000))
