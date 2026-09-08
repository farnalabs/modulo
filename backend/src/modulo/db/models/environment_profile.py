import uuid
from typing import Any, Final

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin

# Single source of truth for the provider_type vocabulary (FAR-595). The
# model CHECK below, the API request-validation pattern
# (modulo.api.routes.environment_profiles), and the vocabulary scanner test
# (tests/unit/core/runtime_provider/test_provider_type_coverage.py) all
# derive from this constant — never hardcode the list elsewhere.
PROVIDER_TYPES: Final[frozenset[str]] = frozenset({"local_docker", "e2b", "local", "runner_docker"})


def _provider_type_check_sql() -> str:
    """Build the CHECK SQL text from PROVIDER_TYPES (sorted for determinism)."""
    ordered = ", ".join(f"'{value}'" for value in sorted(PROVIDER_TYPES))
    return f"provider_type IN ({ordered})"


class EnvironmentProfile(SoftDeleteMixin, OrgScoped):
    __tablename__ = "environment_profiles"
    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_env_profiles_visibility"),
        CheckConstraint(
            _provider_type_check_sql(),
            name="ck_env_profiles_provider_type",
        ),
        CheckConstraint(
            "persistence_policy IN ('ephemeral', 'retained', 'cache')",
            name="ck_env_profiles_persistence_policy",
        ),
        CheckConstraint(
            "network_policy IN ('none', 'outbound', 'selected')",
            name="ck_env_profiles_network_policy",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # No server_default (dropped by migration 0176): every writer sets
    # provider_type explicitly post-D2 (FAR-587 writer inventory).
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False)
    image_ref: Mapped[str | None] = mapped_column(String(500))
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    network_policy: Mapped[str] = mapped_column(String(20), nullable=False, server_default="outbound")
    initialisation_strategy: Mapped[str] = mapped_column(String(30), nullable=False, server_default="git_clone")
    secret_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    persistence_policy: Mapped[str] = mapped_column(String(20), nullable=False, server_default="ephemeral")
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="active")
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), index=True
    )
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
