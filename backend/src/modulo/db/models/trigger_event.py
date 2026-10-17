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
    # FAR-1916: a ``work_item_refs_required`` pipeline refused a delivery that
    # supplied no work-item refs (FAR-794 slice 2b). The engine wrote this
    # value at its raise site but it was never added to the constraint
    # vocabulary, so the write raised a CHECK violation (IntegrityError -> a
    # 503 where a 422 was intended) and no refusal event could ever persist.
    # Added here plus the widening migration 0318 — the value a write site
    # emits MUST be in the vocabulary in the same change (AGENTS.md lesson).
    "required_refs_missing",
    # The Slack ``app_mention`` payload could not be parsed
    # (``slack_app_mention._parse_mention``), so the delivery was refused and
    # the failure audited. The write site emitted this value but it was never
    # added to the vocabulary, so the INSERT raised a CHECK violation
    # (IntegrityError) and no parse-failure event could ever persist. Added
    # here plus the widening migration 0319.
    "parse_failed",
)

_TRIGGER_EVENT_VALIDATION_SQL = f"validation_result IN {tuple(VALIDATION_RESULT_VALUES)}"


class TriggerEvent(OrgScoped):
    __tablename__ = "trigger_events"
    __table_args__ = (
        CheckConstraint(
            _TRIGGER_EVENT_VALIDATION_SQL,
            name="ck_trigger_events_validation_result",
        ),
        # Age-based retention (FAR-167) reads ``received_at`` in a bounded
        # select-then-delete sweep (migration 0092).
        Index("ix_trigger_events_received_at", "received_at"),
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
