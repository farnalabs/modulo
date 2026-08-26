import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class Trigger(SoftDeleteMixin, OrgScoped):
    __tablename__ = "triggers"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('manual', 'webhook', 'cron', 'polling', 'agent_signal', 'ongoing', 'slack_app_mention')",
            name="ck_triggers_type",
        ),
        CheckConstraint("max_concurrent_runs > 0", name="ck_triggers_max_concurrent_runs"),
        # Ongoing triggers (FAR-158) are cost-controlled at the DB level: a daily
        # spend limit is REQUIRED (non-null, > 0) and the target pool size
        # ``max_concurrent_runs`` is bounded 1..20. Both are partial CHECKs — they
        # only apply to ``trigger_type = 'ongoing'`` rows — mirroring migration
        # 0094_ongoing_trigger_type.
        CheckConstraint(
            "trigger_type <> 'ongoing' OR (daily_spend_limit IS NOT NULL AND daily_spend_limit > 0)",
            name="ck_triggers_ongoing_spend_limit",
        ),
        CheckConstraint(
            "trigger_type <> 'ongoing' OR (max_concurrent_runs BETWEEN 1 AND 20)",
            name="ck_triggers_ongoing_target_range",
        ),
        # FAR-377 run-kind discriminator: 'run' (the existing behaviour, fires a
        # pipeline Run) or 'suite_run' (fires a SuiteRun execution instead).
        CheckConstraint("run_kind IN ('run', 'suite_run')", name="ck_triggers_run_kind"),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    daily_spend_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Cron-specific fields (nullable for non-cron trigger types)
    cron_expression: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cron_timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # FAR-190 no-delivery streak engine — the lower boundary of an ongoing
    # trigger's no-delivery streak. The streak is bounded by
    # GREATEST(last_delivery_at, streak_epoch): the epoch is anchored at
    # migration backfill, at creation, and on EVERY active=True transition (via
    # the shared cron_helpers.anchor_trigger_streak_epoch helper) so pre-existing
    # no-delivery history can never mass-deactivate on tick 1 and a re-enabled
    # trigger's streak restarts from its re-enable moment. NULL (rolling-deploy
    # skew) fails SAFE: the engine's boundary COALESCEs it to now(), so a NULL
    # epoch can never be deactivated until the row is re-anchored. server_default
    # mirrors the migration backfill so no insert path can leave an unanchored
    # row.
    streak_epoch: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=True
    )
    # FAR-377 run-kind discriminator. 'run' (DEFAULT) fires a pipeline Run;
    # 'suite_run' fires a SuiteRun execution. Additive — existing rows are 'run'.
    run_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="run")
    # FAR-377: the eval suite a 'suite_run' trigger executes. The eval dataset +
    # schedule config + model backend + scenario inputs live in ``config_json``;
    # ``pipeline_id`` remains NOT NULL as the suite's owning/placeholder pipeline.
    eval_suite_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("eval_suites.id", ondelete="RESTRICT"), nullable=True, index=True
    )
