from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, Index, LargeBinary, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped

# Postgres stores these columns as jsonb (migrations 0147, 0246); the variant
# keeps ORM-created schemas (SQLite/create_all test path) on generic JSON
# while production DDL uses JSONB. Same convention as agent.py.
_JSONB_COL = JSON().with_variant(JSONB(), "postgresql")


class SsoProvider(OrgScoped):
    __tablename__ = "sso_providers"
    __table_args__ = (
        # API-validated since introduction (admin_sso: ^(oidc|saml)$); every
        # test fixture and raw-SQL seed uses only these two values. Enforced
        # at the DB level so a bad write outside the API cannot persist.
        # No CHECK on preset (extensible registry — a new preset must not
        # require a DDL migration) and none on default_role (wider in
        # practice: integration seeds use 'viewer').
        CheckConstraint("provider_type IN ('oidc', 'saml')", name="ck_sso_providers_type"),
        # Pre-auth hot path: get_enabled_saml_provider /
        # list_enabled_oidc_providers / list_enabled_saml_providers filter
        # globally (no org scope) on exactly this shape at every login.
        Index("ix_sso_providers_type_enabled", "provider_type", "enabled"),
        # Declared here to mirror migration 0158 (FAR-464 option a): the
        # provider_id slug is GLOBALLY unique so pre-auth system-session
        # resolution by slug is deterministic. Partial + nullable so rows
        # without a slug never collide.
        Index(
            "uq_sso_providers_provider_id",
            "provider_id",
            unique=True,
            postgresql_where=text("provider_id IS NOT NULL"),
            sqlite_where=text("provider_id IS NOT NULL"),
        ),
    )

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
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # FAR-855: auto_provision is now the JOIN-GATE mode switch, NOT a blanket
    # JIT grant. Default is False ("invitation only") both in Python and in the
    # DB server_default (migration 0246) so new org sign-ins deny by default;
    # only memberships held or granted (SCIM / invitation / domain allowlist)
    # let a previously-unknown SSO identity join.
    auto_provision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # FAR-855: case-insensitive EXACT-match allowlist of verified email
    # domains, consulted only when auto_provision is True. Empty list = mode 3
    # ("anyone who authenticates") which additionally requires the
    # sso_unrestricted_provisioning feature flag (default OFF).
    allowed_domains: Mapped[list[str]] = mapped_column(_JSONB_COL, nullable=False, default=list, server_default="[]")
    default_role: Mapped[str] = mapped_column(String(32), nullable=False, default="runner", server_default="runner")
    group_mappings: Mapped[list[dict[str, Any]]] = mapped_column(
        _JSONB_COL, nullable=False, default=list, server_default="[]"
    )
    preset: Mapped[str] = mapped_column(String(32), nullable=False, server_default="custom")
    tenant_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
