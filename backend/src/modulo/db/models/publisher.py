from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class Publisher(OrgScoped):
    __tablename__ = "publishers"
    __table_args__ = (
        UniqueConstraint("organisation_id", "public_key_hex", name="uq_publishers_org_key"),
        UniqueConstraint("organisation_id", "name", name="uq_publishers_org_name"),
        # Vocabulary guard for trust_tier (FAR-1758): ``crud/publisher.py``
        # VALID_TIERS and the admin route's ``_RE_GREEN_OR_AMBER`` pattern both
        # allow exactly ``green`` / ``amber``; the DB now enforces the same set
        # so a raw-SQL or direct-ORM write cannot widen it. Migration 0311 adds
        # the matching DB-side constraint (NOT VALID then VALIDATE).
        CheckConstraint(
            "trust_tier IN ('green', 'amber')",
            name="ck_publishers_trust_tier",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    # NO single-column index: every lookup is
    # ``WHERE organisation_id = ? AND public_key_hex = ?``, already served by
    # uq_publishers_org_key. The redundant ``ix_publishers_public_key_hex``
    # (0005/0109) is dropped by migration 0311.
    public_key_hex: Mapped[str] = mapped_column(String(128), nullable=False)
    trust_tier: Mapped[str] = mapped_column(String(10), nullable=False, server_default="amber")
    verified_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    website_url: Mapped[str | None] = mapped_column(String(2000))
