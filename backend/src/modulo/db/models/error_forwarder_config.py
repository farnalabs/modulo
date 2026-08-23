from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class ErrorForwarderConfig(SoftDeleteMixin, OrgScoped):
    __tablename__ = "error_forwarder_configs"

    __table_args__ = (
        Index(
            "uq_org_forwarder_type",
            "organisation_id",
            "forwarder_type",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    forwarder_type: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    config_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
