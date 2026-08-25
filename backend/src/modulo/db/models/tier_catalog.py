from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class TierCatalog(Base):
    __tablename__ = "tier_catalog"

    tier_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    requires_license: Mapped[bool] = mapped_column(Boolean, server_default="false")
    description: Mapped[str | None] = mapped_column(String(2000))


class FeatureFlagCatalog(Base):
    __tablename__ = "feature_flag_catalog"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    description: Mapped[str | None] = mapped_column(String(2000))
    tier_id: Mapped[str] = mapped_column(String(255), ForeignKey("tier_catalog.tier_id"), nullable=False, index=True)
    depends_on: Mapped[list[str] | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
