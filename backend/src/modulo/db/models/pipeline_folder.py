import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.account import Account


class PipelineFolder(OrgScoped):
    __tablename__ = "pipeline_folders"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("pipeline_folders.id", ondelete="CASCADE"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    parent: Mapped[Optional["PipelineFolder"]] = relationship(
        "PipelineFolder", remote_side="PipelineFolder.id", back_populates="children"
    )
    children: Mapped[list["PipelineFolder"]] = relationship(
        "PipelineFolder", remote_side="PipelineFolder.parent_id", back_populates="parent"
    )
    creator: Mapped["Account"] = relationship()
