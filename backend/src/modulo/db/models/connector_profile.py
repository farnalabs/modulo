import uuid

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class ConnectorProfile(OrgScoped):
    """Structured runtime profile for a generic REST connector instance (FAR-412).

    1:1 with :class:`~modulo.db.models.connector_instance.ConnectorInstance`. The
    declarative endpoint (base_url, method, path, headers, params, body,
    records_path) lives in ``connector_instances.config_json``; the credential
    secret (token / api_key / password) lives ONLY in
    ``connector_instances.credentials_ciphertext`` — it is NOT duplicated here.
    This table captures the non-secret auth mode + runtime/transport knobs the
    connection profile exposes to the operator.

    Org-scoped and RLS-guarded via ``rls_org_isolation`` (see OrgScoped).
    """

    __tablename__ = "connector_profiles"
    __table_args__ = (
        CheckConstraint(
            "auth_mode IN ('bearer', 'api_key', 'basic')",
            name="ck_connector_profiles_auth_mode",
        ),
        CheckConstraint(
            "auth_in IS NULL OR auth_in IN ('header', 'query')",
            name="ck_connector_profiles_auth_in",
        ),
        CheckConstraint(
            "auth_in <> 'query' OR auth_query_param_name IS NOT NULL",
            name="ck_connector_profiles_auth_query_param",
        ),
        CheckConstraint(
            "auth_in <> 'header' OR auth_query_param_name IS NULL",
            name="ck_connector_profiles_auth_header_inverse",
        ),
    )

    connector_instance_id: Mapped[uuid.UUID] = mapped_column(
        # ``unique=True`` alone yields the backing index; the separate plain index
        # on this column was redundant (the UNIQUE constraint already indexes it),
        # so it is dropped to avoid a duplicate index (FAR-412).
        Uuid(),
        ForeignKey("connector_instances.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    auth_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    auth_in: Mapped[str | None] = mapped_column(String(10))
    auth_query_param_name: Mapped[str | None] = mapped_column(String(128))
    idempotent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    idempotency_key_header: Mapped[str | None] = mapped_column(String(128))
    response_max_bytes: Mapped[int | None] = mapped_column(Integer)
    timeout_seconds: Mapped[float | None] = mapped_column(Float)
    verify_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
