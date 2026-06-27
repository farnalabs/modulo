"""Feature flag registry — catalogs all known feature flags and their current status."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


class PlanContext:
    """Resolved plan context — determines which features are available for the current deployment."""

    def __init__(self, settings: Any = None, *, has_license_key: bool = False, tier: str = "free") -> None:
        if settings is not None:
            has_license_key = bool(settings.modulo_license_key)
            tier = "enterprise" if has_license_key else "free"
        self._registry = FeatureFlagRegistry(current_tier=tier, has_license_key=has_license_key)

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active


class FeatureFlagRegistry:
    """Knows all feature flags and determines active status from license state."""

    def __init__(self, current_tier: str = "free", has_license_key: bool = False) -> None:
        self._current_tier = current_tier
        self._has_license_key = has_license_key
        self._flags = _KNOWN_FLAGS
        self._refresh()

    def _refresh(self) -> None:
        tier_order = {"free": 0, "enterprise": 1, "v1": 2, "v2": 3}
        current_rank = tier_order.get(self._current_tier, 0)

        for flag in self._flags:
            flag_tier_rank = tier_order.get(flag.tier, 0)
            flag.currently_active = self._has_license_key and flag_tier_rank <= current_rank

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
