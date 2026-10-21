import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class AssistantContextSource(Base):
    __tablename__ = "assistant_context_sources"
    __table_args__ = (
        # Rows are always ORGANISATION-scoped: ``account_id IS NULL`` is the
        # org default, a set ``account_id`` is that member's override inside
        # the same org. The pre-FAR rename constraint was an XOR
        # ("org set, account NULL" OR "org NULL, account set"), which rejected
        # the exact row shape the user-override write path inserts
        # (``AssistantContextSourceService._upsert_context_source``) — every
        # ``PUT /me/assistant/context-sources/{key}`` failed with a CHECK
        # violation. Migration 0324 replaces it; it is added ``NOT VALID``
        # where legacy org-less rows exist so old data never blocks deploy.
        CheckConstraint("organisation_id IS NOT NULL", name="ck_assistant_context_sources_owner"),
        CheckConstraint(
            "source_mode IN ('always_on', 'tool', 'off')",
            name="ck_assistant_context_sources_mode",
        ),
        UniqueConstraint(
            "organisation_id",
            "account_id",
            "source_key",
            name="uq_assistant_context_sources_key",
        ),
        # ``uq_assistant_context_sources_key`` cannot protect the org-default
        # rows: Postgres treats every NULL ``account_id`` as distinct, so
        # duplicate org defaults accumulate and the upsert's
        # ``scalar_one_or_none()`` then raises ``MultipleResultsFound`` for
        # that org forever. A partial unique index scoped to the NULL-account
        # rows is the only constraint that can enforce them.
        Index(
            "uq_assistant_context_sources_org_key_default",
            "organisation_id",
            "source_key",
            unique=True,
            postgresql_where=text("account_id IS NULL"),
            # postgresql_where is ignored on SQLite, which would otherwise
            # build a FULL unique index and reject a member override that
            # shares a source_key with its org default (house pattern:
            # lifecycle_maps.uq_lifecycle_maps_org_name).
            sqlite_where=text("account_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="always_on")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )
