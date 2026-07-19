from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class ErrorForwarderConfig(OrgScoped):
    __tablename__ = "error_forwarder_configs"

    __table_args__ = (UniqueConstraint("organisation_id", "forwarder_type", name="uq_org_forwarder_type"),)

    forwarder_type: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    config_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
