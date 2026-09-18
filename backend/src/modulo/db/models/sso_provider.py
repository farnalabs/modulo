from typing import Any

from sqlalchemy import JSON, Boolean, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class SsoProvider(OrgScoped):
    __tablename__ = "sso_providers"

    provider_type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    client_secret: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    discovery_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    metadata_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    metadata_xml: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # FAR-855: auto_provision is now the JOIN-GATE mode switch, NOT a blanket
    # JIT grant. Default is False ("invitation only") both in Python and in the
    # DB server_default (migration 0246) so new org sign-ins deny by default;
    # only memberships held or granted (SCIM / invitation / domain allowlist)
    # let a previously-unknown SSO identity join.
    auto_provision: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # FAR-855: case-insensitive EXACT-match allowlist of verified email
    # domains, consulted only when auto_provision is True. Empty list = mode 3
    # ("anyone who authenticates") which additionally requires the
    # sso_unrestricted_provisioning feature flag (default OFF).
    allowed_domains: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    default_role: Mapped[str] = mapped_column(String(32), default="runner", server_default="runner")
    group_mappings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, server_default="[]")
    preset: Mapped[str] = mapped_column(String(32), nullable=False, server_default="custom")
    tenant_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
