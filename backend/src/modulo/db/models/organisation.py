import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Integer, Numeric, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class Organisation(Base):
    __tablename__ = "organisations"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended', 'deleted')", name="ck_organisations_status"),
        CheckConstraint(
            "NOT triggers_paused OR triggers_paused_at IS NOT NULL",
            name="ck_organisations_triggers_paused_at",
        ),
        CheckConstraint(
            "NOT guardrails_kill_switch OR guardrails_kill_switch_at IS NOT NULL",
            name="ck_organisations_guardrails_kill_switch_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # This is deliberately not an FK: the first organisation must exist before its first user.
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    otel_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    plan_id: Mapped[str | None] = mapped_column(String(255))
    # Tenancy-bounded authorization kill-switch (ADR 017 DECISION 3). Dedicated
    # boolean column — NOT settings_json — atomic at statement level and
    # multi-backend safe. Default TRUE: enforcement is on unless explicitly off.
    authz_enforce: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Org-wide "pause all pipeline triggers" kill-switch. When TRUE, NEW
    # trigger-initiated runs (webhook/cron/polling/agent_signal/replay) are
    # blocked at the create_run gate; manual runs, MCP trigger_pipeline, and
    # scheduled reports are NOT paused. ``triggers_paused_at`` records when the
    # pause was last enabled (CHECK-constrained to be non-NULL while paused).
    triggers_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    triggers_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Guardrails org-wide kill-switch (FAR-223 item 9): when TRUE every bound
    # guardrail downgrades to OBSERVE at run start (shadow-only — compute +
    # log, never block, never redact). It is NEVER a full disable — observe
    # mode still computes and logs. ``guardrails_kill_switch_at`` records when
    # it was last enabled (CHECK-constrained to be non-NULL while on, mirroring
    # the triggers-pause precedent).
    guardrails_kill_switch: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    guardrails_kill_switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    daily_spend_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    # Hard spend ceilings (FAR-391, spec §5.1 cost controls). Stored in integer
    # CENTS so the gate comparison is exact and allocation-free (no float drift).
    #   max_run_cost_cents      — per-run hard ceiling; None = unlimited.
    #   spend_ceiling_cents      — org lifetime budget; None = unlimited.
    #   org_cumulative_spend_cents — running consumed total (incremented at each
    #                               run's terminal ledger write).
    # A ceiling of 0 is a deliberate kill-switch (blocks every billable run).
    max_run_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spend_ceiling_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    org_cumulative_spend_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    deletion_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deletion_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    export_bundle_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)
    # Guardrail config-as-code snapshot pin (FAR-219 T3): applied/proposed
    # content hashes, serialized YAML and status. Absence = never applied.
    guardrail_pins_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)
