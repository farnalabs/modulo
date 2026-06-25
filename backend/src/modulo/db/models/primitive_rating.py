import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class PrimitiveRating(OrgScoped):
    __tablename__ = "primitive_ratings"
    __table_args__ = (CheckConstraint("thumbs_up IS NOT NULL", name="ck_primitive_ratings_thumbs"),)

    primitive_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("library_primitives.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    thumbs_up: Mapped[bool] = mapped_column(nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
