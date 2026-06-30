"""Feature flag registry — catalogs all known feature flags and their current status."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from modulo.core.license import LicenseData


@dataclass
class FeatureFlag:
    name: str
    description: str
    tier: str
    currently_active: bool = False
    depends_on: list[str] | None = None


_KNOWN_FLAGS: list[FeatureFlag] = [
    # ── Community tier ─────────────────────────────────────────────────
    FeatureFlag(
        name="parallel_branches",
        description="Run branching logic in parallel within a pipeline",
        tier="community",
    ),
    FeatureFlag(
        name="eval_system",
        description="Built-in eval runner for LLM output quality gates",
        tier="community",
    ),
    FeatureFlag(
        name="webhook_trigger",
        description="Trigger pipelines via incoming webhooks",
        tier="community",
    ),
    FeatureFlag(
        name="cron_trigger",
        description="Schedule pipeline runs on a cron expression",
        tier="community",
    ),
    FeatureFlag(
        name="mcp_server",
        description="Expose pipelines as MCP tools",
        tier="community",
    ),
    FeatureFlag(
        name="community_library",
        description="Browse and import community-contributed pipeline primitives",
        tier="community",
    ),
    FeatureFlag(
        name="saved_views",
        description="Persistent saved views for run and pipeline lists",
        tier="community",
    ),
    # ── Team tier ──────────────────────────────────────────────────────
    FeatureFlag(
        name="sso",
        description="Single sign-on via OIDC / SAML 2.0 providers",
        tier="team",
    ),
    FeatureFlag(
        name="team_rbac",
        description="Team-level role-based access control",
        tier="team",
    ),
    FeatureFlag(
        name="audit_viewer",
        description="Tamper-evident audit log viewer",
        tier="team",
    ),
    FeatureFlag(
        name="admin_spend_limits",
        description="Per-organisation daily spend limits and budgets",
        tier="team",
    ),
    FeatureFlag(
        name="observability",
        description="OpenTelemetry export and LangSmith integration settings",
        tier="team",
    ),
    FeatureFlag(
        name="view_modes",
        description="Multiple named UI views with admin-defined feature visibility per view and user/team/role assignment",
        tier="team",
    ),
    # ── v1 tier ────────────────────────────────────────────────────────
    FeatureFlag(
        name="polling_trigger",
        description="Trigger pipelines by polling external endpoints",
        tier="v1",
    ),
    FeatureFlag(
        name="agent_signal_trigger",
        description="Trigger pipelines via agent-to-agent signals",
        tier="v1",
    ),
    # ── v1 extended ────────────────────────────────────────────────────
    FeatureFlag(
        name="schema_union_types",
        description="Union types and polymorphic schemas",
        tier="v1",
    ),
    FeatureFlag(
        name="migration_cli",
        description="CLI tool for migrating pipelines across instances",
        tier="v1",
    ),
    FeatureFlag(
        name="helm_deployment",
        description="Helm chart for production Kubernetes deployment",
        tier="v1",
    ),
    # ── v2 tier ────────────────────────────────────────────────────────
    FeatureFlag(
        name="checkpoint_encryption",
        description="Encrypt pipeline checkpoints at rest",
        tier="v2",
    ),
    FeatureFlag(
        name="audit_crypto_chain",
        description="Cryptographic chaining of audit events for tamper evidence",
        tier="v2",
    ),
    FeatureFlag(
        name="community_registry",
        description="Publish and discover community pipeline primitives",
        tier="v2",
    ),
    FeatureFlag(
        name="prompt_optimization",
        description="Automated prompt tuning and optimisation",
        tier="v2",
    ),
    FeatureFlag(
        name="pipeline_diff_rollback",
        description="Diff-based pipeline version comparison and rollback",
        tier="v2",
    ),
    FeatureFlag(
        name="model-backend-management",
        description="Manage LLM backend connections and credentials",
        tier="team",
    ),
    FeatureFlag(
        name="environment-profiles",
        description="Sandbox environment profiles for code execution",
        tier="team",
    ),
    FeatureFlag(
        name="plugin-management",
        description="Manage plugins, connectors, and node categories",
        tier="team",
    ),
]


TIER_RANK: dict[str, int] = {"community": 0, "team": 1, "v1": 2, "v2": 3}


class PlanContext(Protocol):
    """Interface for plan-based feature gating."""

    def feature_enabled(self, name: str) -> bool: ...

    def list_enabled_features(self) -> list[FeatureFlag]: ...


class CommunityTier:
    """Default plan — community-tier features active without a license key."""

    def __init__(self) -> None:
        self._registry = FeatureFlagRegistry(current_tier="community", has_license_key=False)

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active

    def list_enabled_features(self) -> list[FeatureFlag]:
        return [f for f in self._registry.list_flags() if f.currently_active]


class LicenseKeyTier:
    """Licensed plan — activates features based on license tier and explicit feature list."""

    def __init__(self, license_data: LicenseData) -> None:
        self._tier = license_data.tier
        self._features = set(license_data.features)
        self._registry = FeatureFlagRegistry(current_tier=license_data.tier, has_license_key=True)

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active or name in self._features

    def list_enabled_features(self) -> list[FeatureFlag]:
        return [
            f
            for f in self._registry.list_flags()
            if f.currently_active or f.name in self._features
        ]


class TeamPlanContext:
    """Team plan — enables team-tier features without a license key."""

    def __init__(self) -> None:
        self._registry = FeatureFlagRegistry(current_tier="team", has_license_key=False)

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active

    def list_enabled_features(self) -> list[FeatureFlag]:
        return [f for f in self._registry.list_flags() if f.currently_active]


class FullAccessPlanContext:
    """Full-access plan — enables all features (team, v1, v2)."""

    def __init__(self) -> None:
        self._registry = FeatureFlagRegistry(current_tier="v2", has_license_key=True)

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active

    def list_enabled_features(self) -> list[FeatureFlag]:
        return [f for f in self._registry.list_flags() if f.currently_active]


def get_plan_context(plan_id: str) -> PlanContext:
    """Return the right PlanContext for a plan_id."""
    registry: dict[str, type[PlanContext]] = {
        "community": CommunityTier,
        "team": TeamPlanContext,
        "full-access": FullAccessPlanContext,
    }
    cls = registry.get(plan_id, CommunityTier)
    return cls()


def resolve_plan_context(settings: Any) -> PlanContext:
    """Resolve a PlanContext from a stored license, env-var license key, or fall back to CommunityTier."""
    from modulo.core.license import get_license, parse_and_verify

    lic = get_license()
    if lic is not None:
        return LicenseKeyTier(lic)

    raw_key: str = getattr(settings, "modulo_license_key", "") or ""
    if raw_key:
        validation = parse_and_verify(raw_key)
        if validation.valid and validation.license_data is not None:
            return LicenseKeyTier(validation.license_data)
        return LicenseKeyTier(
            LicenseData(
                tier="team",
                features=[],
                expires_at="",
                org_id="",
                raw_payload={},
                raw_key=raw_key,
            )
        )

    return CommunityTier()


class FeatureFlagRegistry:
    """Knows all feature flags and determines active status from the current tier.

    Falls back to hardcoded ``_KNOWN_FLAGS`` / ``TIER_RANK`` when DB-backed data
    has not been loaded.  Call ``load_from_db()`` to replace with catalog data.
    """

    _overrides: dict[str, bool] = {}

    def __init__(self, current_tier: str = "community", has_license_key: bool = False) -> None:
        self._current_tier = current_tier
        self._has_license_key = has_license_key
        self._flags = _KNOWN_FLAGS
        self._refresh()

    async def load_from_db(self, session: Any) -> None:
        """Replace hardcoded flag data with DB-backed data from tier_catalog / feature_flag_catalog."""
        from modulo.db.crud.tier_catalog import list_feature_flags, list_tiers

        db_tiers = await list_tiers(session)
        self._tier_rank = {t["tier_id"]: t["rank"] for t in db_tiers}

        db_flags = await list_feature_flags(session)
        self._flags = [
            FeatureFlag(
                name=f["name"],
                description=f["description"],
                tier=f["tier_id"],
                depends_on=f["depends_on"],
            )
            for f in db_flags
            if f["is_active"]
        ]
        self._refresh()

    @classmethod
    async def from_db(
        cls,
        session: Any,
        current_tier: str,
        has_license_key: bool = False,
    ) -> FeatureFlagRegistry:
        """Create a registry pre-loaded from the DB tier/feature catalog."""
        instance = cls(current_tier=current_tier, has_license_key=has_license_key)
        await instance.load_from_db(session)
        return instance

    def _refresh(self) -> None:
        tier_rank: dict[str, int] = getattr(self, "_tier_rank", TIER_RANK)
        current_rank = tier_rank.get(self._current_tier, 0)

        for flag in self._flags:
            flag_tier_rank = tier_rank.get(flag.tier, 0)
            flag.currently_active = flag_tier_rank <= current_rank

        for name, enabled in self._overrides.items():
            for flag in self._flags:
                if flag.name == name:
                    flag.currently_active = enabled
                    break

    def refresh(self, current_tier: str, has_license_key: bool) -> None:
        self._current_tier = current_tier
        self._has_license_key = has_license_key
        self._refresh()

    def list_flags(self) -> list[FeatureFlag]:
        return list(self._flags)

    def get_flag(self, name: str) -> FeatureFlag | None:
        for flag in self._flags:
            if flag.name == name:
                return flag
        return None

    def set_override(self, name: str, enabled: bool) -> None:
        self._overrides[name] = enabled
        self._refresh()

    def clear_override(self, name: str) -> None:
        self._overrides.pop(name, None)
        self._refresh()

    def get_override(self, name: str) -> bool | None:
        return self._overrides.get(name)

    def tier_gap_flags(self) -> list[FeatureFlag]:
        """Return flags whose tier is above community but inactive because license is community."""
        if self._current_tier != "community":
            return []
        tier_rank: dict[str, int] = getattr(self, "_tier_rank", TIER_RANK)
        community_rank = tier_rank.get("community", 0)
        return [f for f in self._flags if tier_rank.get(f.tier, 0) > community_rank and not f.currently_active]


async def get_plan_for_org(
    session: Any,
    org_id: uuid.UUID | None,
) -> str:
    """Get the effective plan for an org."""
    from modulo.db.crud.organisation import get_organisation
    from modulo.db.crud.system_config import get_config

    if org_id is not None:
        org = await get_organisation(session, org_id)
        if org is not None and org.plan_id:
            return org.plan_id

    config = await get_config(session, "default_plan")
    if config is not None:
        return str(config.value)

    return "community"
