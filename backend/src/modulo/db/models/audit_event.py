import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, OrgScoped


class AuditEvent(OrgScoped):
    __tablename__ = "audit_events"
    # FAR-748: the sweep-alarm detection aggregate filters by
    # (organisation_id, event_type, account_id, created_at); the composite
    # index pins that shape instead of relying on the org-narrowed subset of
    # the single-column indexes.
    #
    # 0292: every list/export path filters by resource_type
    # (core/audit_logger._apply_filters) and point lookups by
    # (resource_type, resource_id) serve the run-workspace probe
    # (api/routes/runs.py) and the MCP audit resource lookups. The new
    # composite leads on the tenant column so it serves both shapes; it is
    # declared here for parity with the migration so create_all'd schemas
    # (SQLite unit tests) carry the same index.
    __table_args__ = (
        Index(
            "ix_audit_events_org_type_actor_time",
            "organisation_id",
            "event_type",
            "account_id",
            "created_at",
        ),
        Index(
            "ix_audit_events_org_resource",
            "organisation_id",
            "resource_type",
            "resource_id",
        ),
    )

    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    # DB stores JSONB (0147_json_to_jsonb_standardize); the PG variant keeps
    # SQLite/MariaDB parity via generic JSON (the run.py cost_breakdown precedent).
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    request_id: Mapped[str | None] = mapped_column(String(255))
    previous_hash: Mapped[str | None] = mapped_column(Text)


class AuditChainHead(Base):
    """Tracks the most recent audit event hash per organisation."""

    __tablename__ = "audit_chain_heads"
    # The ``event_count >= 0`` guard is migration-owned (0165's
    # ``ck_audit_event_event_count``, see
    # ``tests/integration/test_initial_migration.py::_MIGRATION_OWNED_CHECKS``),
    # so it stays out of the ORM rather than being declared twice. The server
    # default (``event_count`` is NOT NULL) is kept here for create_all'd
    # SQLite/MariaDB schemas; 0292 sets the Postgres-side default.

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    last_event_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("audit_events.id", ondelete="SET NULL"), index=True
    )
    event_count: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
