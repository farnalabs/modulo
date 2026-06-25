from sqlalchemy import LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class Secret(OrgScoped):
    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("organisation_id", "key", name="uq_secrets_org_key"),
    )

    key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
