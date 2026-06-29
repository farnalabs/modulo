"""Feature flag registry — catalogs all known feature flags and their current status."""

from __future__ import annotations

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
    # ── Free tier ──────────────────────────────────────────────────────
    FeatureFlag(
        name="parallel_branches",
        description="Run branching logic in parallel within a pipeline",
        tier="free",
    ),
    FeatureFlag(
        name="eval_system",
        description="Built-in eval runner for LLM output quality gates",
        tier="free",
    ),
    FeatureFlag(
        name="webhook_trigger",
        description="Trigger pipelines via incoming webhooks",
        tier="free",
    ),
    FeatureFlag(
        name="cron_trigger",
        description="Schedule pipeline runs on a cron expression",
        tier="free",
    ),
    FeatureFlag(
        name="mcp_server",
        description="Expose pipelines as MCP tools",
        tier="free",
    ),
    FeatureFlag(
        name="community_library",
        description="Browse and import community-contributed pipeline primitives",
        tier="free",
    ),
    FeatureFlag(
        name="saved_views",
        description="Persistent saved views for run and pipeline lists",
        tier="free",
    ),
    # ── Enterprise tier ────────────────────────────────────────────────
    FeatureFlag(
        name="sso",
        description="Single sign-on via OIDC / SAML 2.0 providers",
        tier="enterprise",
    ),
    FeatureFlag(
        name="team_rbac",
        description="Team-level role-based access control",
        tier="enterprise",
    ),
    FeatureFlag(
        name="audit_viewer",
        description="Tamper-evident audit log viewer",
        tier="enterprise",
    ),
    FeatureFlag(
        name="admin_spend_limits",
        description="Per-organisation daily spend limits and budgets",
        tier="enterprise",
    ),
    FeatureFlag(
        name="view_modes",
        description="Multiple named UI views with admin-defined feature visibility per view and user/team/role assignment",
        tier="enterprise",
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
]


TIER_RANK: dict[str, int] = {"free": 0, "enterprise": 1, "v1": 2, "v2": 3}


class PlanContext(Protocol):
    """Interface for plan-based feature gating."""

    def feature_enabled(self, name: str) -> bool: ...

    def list_enabled_features(self) -> list[FeatureFlag]: ...


class FreeTier:
    """Default plan — free-tier features active without a license key."""

    def __init__(self) -> None:
        self._registry = FeatureFlagRegistry(current_tier="free", has_license_key=False)

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active

    def list_enabled_features(self) -> list[FeatureFlag]:
        return [f for f in self._registry.list_flags() if f.currently_active]


class LicenseKeyTier:
    """Enterprise/licensed plan — activates features based on license tier and explicit feature list."""

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


def resolve_plan_context(settings: Any) -> PlanContext:
    """Resolve a PlanContext from a stored license, env-var license key, or fall back to FreeTier."""
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
                tier="enterprise",
                features=[],
                expires_at="",
                org_id="",
                raw_payload={},
                raw_key=raw_key,
            )
        )

    return FreeTier()


class FeatureFlagRegistry:
    """Knows all feature flags and determines active status from the current tier."""

    def __init__(self, current_tier: str = "free", has_license_key: bool = False) -> None:
        self._current_tier = current_tier
        self._has_license_key = has_license_key
        self._flags = _KNOWN_FLAGS
        self._refresh()

    def _refresh(self) -> None:
        current_rank = TIER_RANK.get(self._current_tier, 0)

        for flag in self._flags:
            flag_tier_rank = TIER_RANK.get(flag.tier, 0)
            flag.currently_active = flag_tier_rank <= current_rank

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

    def tier_gap_flags(self) -> list[FeatureFlag]:
        """Return flags whose tier is above free but inactive because license is free."""
        if self._current_tier != "free":
            return []
        return [f for f in self._flags if f.tier != "free" and not f.currently_active]
