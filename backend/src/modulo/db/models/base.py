import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, ForeignKey, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQL referential action shared by every nullable FK across the model layer:
# when the referenced row is deleted, the column is nulled rather than cascading.
# Single home so model modules import it instead of re-declaring the literal.
ONDELETE_SET_NULL: Final[str] = "SET NULL"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Mixin that adds soft-delete support. Set deleted_at to mark as deleted.

    Queries should filter ``WHERE deleted_at IS NULL`` unless explicitly requesting deleted records.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)


class OrgScoped(Base, TimestampMixin):
    """Mixin for entities that belong to an organisation.

    All subclasses are automatically covered by RLS policy
    ``rls_org_isolation`` which filters on ``organisation_id``.
    """

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
