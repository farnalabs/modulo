from sqlalchemy import ARRAY, Boolean, Column, ForeignKey, Integer, Text

from modulo.db.models.base import Base


class TierCatalog(Base):
    __tablename__ = "tier_catalog"

    tier_id = Column(Text, primary_key=True)
    label = Column(Text, nullable=False)
    rank = Column(Integer, nullable=False)
    requires_license = Column(Boolean, server_default="false")
    description = Column(Text)


class FeatureFlagCatalog(Base):
    __tablename__ = "feature_flag_catalog"

    name = Column(Text, primary_key=True)
    description = Column(Text)
    tier_id = Column(Text, ForeignKey("tier_catalog.tier_id"), nullable=False)
    depends_on: list[str] | None = Column(ARRAY(Text))  # type: ignore[assignment]
    is_active = Column(Boolean, server_default="true")
