import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped

_ACTIVE_WHERE = "deactivated_at IS NULL"


class OrgMembership(OrgScoped):
    __tablename__ = "org_memberships"
    __table_args__ = (
        UniqueConstraint("account_id", "organisation_id", name="uq_org_memberships_account_org"),
        CheckConstraint("role IN ('admin', 'operator', 'runner', 'viewer')", name="ck_org_memberships_role"),
        # Active-admin lookup for (organisation, role): backs the
        # last-admin guard count (assert_not_last_admin) and the SCIM
        # admin scan, both filtering
        # WHERE organisation_id = ? AND role = 'admin' plus the shared
        # deactivated_at IS NULL liveness scope. The pre-existing
        # single-column organisation_id index cannot serve the role
        # equality; the partial predicate keeps tombstoned rows out.
        Index(
            "ix_org_memberships_org_role_active",
            "organisation_id",
            "role",
            postgresql_where=text(_ACTIVE_WHERE),
            sqlite_where=text(_ACTIVE_WHERE),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="runner")
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
