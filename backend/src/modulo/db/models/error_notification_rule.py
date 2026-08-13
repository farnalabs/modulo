import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, OrgScoped, TimestampMixin


class ErrorNotificationRule(OrgScoped):
    __tablename__ = "error_notification_rules"

    __table_args__ = (
        CheckConstraint("condition_level IN ('error', 'warning', 'critical')", name="ck_enr_condition_level"),
        CheckConstraint("action_type IN ('in_app', 'email', 'webhook')", name="ck_enr_action_type"),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    condition_level: Mapped[str] = mapped_column(String(20), nullable=False, server_default="error")
    condition_min_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    condition_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    action_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="in_app")
    webhook_url: Mapped[str | None] = mapped_column(Text)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    # The per-signal ingestion marker (FAR-151, §15.8): which signal this rule
    # matches (``agent.failed``, ``agent.no_op``, ``agent.stall``,
    # ``contract.schema``, or a harness/sandbox/connector error class). NULL for
    # legacy level-based rules — a NULL-signal rule keeps matching by level only.
    signal: Mapped[str | None] = mapped_column(String(100))
    # True for seeded default rules (FAR-151, §15.6). The version-bump re-seed
    # force-updates ONLY rows still ``is_default=true`` (never edited); editing a
    # seeded rule flips this (route surface — out of scope for the service seed).
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class DeletedDefault(Base, TimestampMixin):
    """Tombstone for a deleted default alert rule (per-org, per-signal).

    ``restore_defaults`` skips tombstoned signals; a per-rule restore clears the
    tombstone so a re-seed re-adds the rule (FAR-151, §15.6).
    """

    __tablename__ = "deleted_defaults"

    __table_args__ = (
        UniqueConstraint("organisation_id", "signal", name="uq_deleted_defaults_org_signal"),
        CheckConstraint("signal <> ''", name="ck_deleted_defaults_signal_nonempty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    signal: Mapped[str] = mapped_column(String(100), nullable=False)
