"""Feature flag registry — catalogs all known feature flags and their current status.

Tier structure (matching the DB tier_catalog):
    community (0) — Free tier, all features active without a license
    team      (1) — The one paid tier
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, ClassVar, Protocol

from modulo.core.license import LicenseData, is_parked

logger = logging.getLogger(__name__)


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
        name="mcp_server",
        description="Expose pipelines as MCP tools",
        tier="community",
    ),
    FeatureFlag(
        name="saved_views",
        description="Persistent saved views for run and pipeline lists",
        tier="community",
    ),
    FeatureFlag(
        name="dashboard_charts",
        description="Dashboard trend charts (run activity sparklines)",
        tier="community",
    ),
    FeatureFlag(
        name="web_vitals_analytics",
        description="Web Vitals analytics dashboard for monitoring frontend performance",
        tier="community",
    ),
    FeatureFlag(
        name="assistant",
        description="Assistant in-app AI assistant",
        tier="community",
    ),
    FeatureFlag(
        name="model_backend_management",
        description="Manage LLM backend connections and credentials",
        tier="community",
    ),
    FeatureFlag(
        name="user_management",
        description="Basic user management — create, deactivate, and role-assign organisation users",
        tier="community",
    ),
    FeatureFlag(
        name="observability",
        description="OpenTelemetry export and LangSmith integration settings",
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
        name="admin_cost_controls",
        description="Budget overview, team budgets, alert thresholds, and billing settings",
        tier="team",
    ),
    FeatureFlag(
        name="view_modes",
        description=(
            "Multiple named UI views with admin-defined feature visibility per view and user/team/role assignment"
        ),
        tier="team",
    ),
    FeatureFlag(
        name="email_config",
        description="SMTP email configuration for notifications",
        tier="team",
    ),
    FeatureFlag(
        name="error_tracking",
        description="External error tracking and alerting integrations",
        tier="team",
    ),
    FeatureFlag(
        name="scim",
        description="SCIM 2.0 user and group provisioning",
        tier="team",
    ),
    FeatureFlag(
        name="environment_profiles",
        description="Sandbox environment profiles for code execution",
        tier="team",
    ),
    FeatureFlag(
        name="plugin_management",
        description="Manage plugins, connectors, and node categories",
        tier="team",
    ),
    FeatureFlag(
        name="admin_cost_breakdown",
        description="Monthly cost breakdown and anomaly detection across teams",
        tier="team",
    ),
    FeatureFlag(
        name="admin_run_retention",
        description="Configure run retention policies and manual purge",
        tier="team",
    ),
    FeatureFlag(
        name="error_forwarders",
        description="Dispatch errors to external services via webhooks",
        tier="team",
    ),
    FeatureFlag(
        name="schema_version_history",
        description="Version history and diff for schema definitions",
        tier="team",
    ),
    FeatureFlag(
        name="pipeline_delete",
        description="Allow hard-deleting pipelines from the UI",
        tier="team",
    ),
    # ── Team tier (runtime / system config) ─────────────────────────────
    FeatureFlag(
        name="rate_limits",
        description="Configure API rate limits",
        tier="team",
    ),
    FeatureFlag(
        name="runtime_config",
        description="Runtime configuration overrides",
        tier="team",
    ),
    # ── Community tier — analytics page (live; frontend shipped in PR #747) ─
    FeatureFlag(
        name="analytics_page",
        description="Run analytics dashboard (rolling-window run/cost/quality series)",
        tier="community",
    ),
    # ── Community tier — mobile icon-rail experiment (default OFF) ──────────
    FeatureFlag(
        name="mobile_sidebar_rail",
        description="Mobile icon-rail sidebar (experimental)",
        tier="community",
    ),
    # ── Community tier — user-scoped MCP keys (default OFF, FAR-620) ────────
    FeatureFlag(
        name="user_scoped_mcp_keys",
        description="Per-user MCP API keys (keys operate as their creator's identity)",
        tier="community",
    ),
    # ── Community tier — API-key grant-sets (default OFF, FAR-1477 / ADR 058) ──
    FeatureFlag(
        name="api_key_grants",
        description="Explicit capability grant-sets on API keys (role becomes a bundle over grants)",
        tier="community",
    ),
    # ── Community tier - SSO unrestricted JIT provisioning (default OFF, FAR-855) ──
    FeatureFlag(
        name="sso_unrestricted_provisioning",
        description=(
            "Allow SSO providers to auto-provision ANY authenticated identity into the "
            "organisation (auto_provision=true with no allowed_domains). DANGEROUS: "
            "self-service org join for every IdP account. Intentionally not plan-tier gated."
        ),
        tier="community",
    ),
    # Retained in the registry only so persisted/catalogued organisations can
    # upgrade without losing compatibility; retired flags are filtered from all
    # customer-facing catalogue and plan responses below.
    FeatureFlag(
        name="lifecycle_map_journeys",
        description="Retired internal Lifecycle Map journey display",
        tier="community",
    ),
    # ── Community tier — System Maps (Beta, internal dogfood only) ─────────────
    FeatureFlag(
        name="system_maps",
        description="Beta System Maps (Delivery System Maps)",
        tier="community",
    ),
    # ── Community tier — webhook notification delivery log (default OFF, FAR-656) ──
    FeatureFlag(
        name="webhook_notification_log",
        description="Webhook notification delivery log (admin delivery-log UI)",
        tier="community",
    ),
    # ── Community tier — library collections (FAR-760, default OFF) ───────
    FeatureFlag(
        name="library_collection",
        description="Library collections — pin multiple primitives into a browsable bundle",
        tier="community",
    ),
    # ── Community tier — evidence-driven autonomy gating (FAR-1175, default OFF) ──
    FeatureFlag(
        name="autonomy_gating",
        description=(
            "Evidence-driven autonomy gating — a pipeline's HITL gates resolve "
            "against its runtime earned autonomy level (demotion bites in-flight "
            "runs at the next gate; promotion applies only to new runs)"
        ),
        tier="community",
    ),
    # ── Community tier — human-linked MCP authority for human_only HITL gates
    # (ADR 063 step 1, FAR-1957; FAR-2281; default OFF). NOT a plan-tier
    # feature: an admin may enable it per-org. The restrictive state is "OFF",
    # so the resolver fails CLOSED to OFF (deny) on any read error.
    FeatureFlag(
        name="mcp_human_linked_hitl_authority",
        description=(
            "Allow a verified human-linked credential to claim, approve or reject "
            "human_only HITL gates over MCP based on persona (ADR 063 step 1, "
            "FAR-2281). Eligible credentials are consent-derived OAuth tokens "
            "and user-scoped human-linked API keys. When disabled, every MCP "
            "caller is denied on human_only gates. deliver_manual remains "
            "browser-only."
        ),
        tier="community",
    ),
]


# community=Free, team=one paid tier
TIER_RANK: dict[str, int] = {"community": 0, "team": 1}

# Flags shipped default-OFF on every tier regardless of tier rank. Each is
# listed in the catalog for per-org toggling AND hardcoded here so the
# community-tier tier-rank fallback in ``FeatureFlagRegistry._refresh`` can
# never activate it (the "two-mechanism" precedent started by
# ``mobile_sidebar_rail``). Orgs enable them per-org via
# ``feature_overrides``; a system-level ``set_override`` also wins.
# Retained only in persisted data for backwards compatibility; it is not a
# customer-configurable flag. Internal Lifecycle Map access is controlled by the
# route's dev-mode visibility gate instead of an org feature override.
RETIRED_INTERNAL_FLAGS: frozenset[str] = frozenset({"lifecycle_map_journeys"})

# Internal-only flags that are visible/enableable ONLY in dev-mode for
# internal operators (system admin + operator role + in-dev grant). These are
# filtered from customer-facing catalogue and plan responses (see
# effective_feature_flags, get_available_features MCP tool).
INTERNAL_FLAGS: frozenset[str] = frozenset(
    {
        "lifecycle_map_journeys",
        "system_maps",
    }
)


DEFAULT_OFF_FLAGS: frozenset[str] = frozenset(
    {
        "mobile_sidebar_rail",
        "dashboard_charts",
        "saved_views",
        "user_scoped_mcp_keys",
        "webhook_notification_log",
        "library_collection",
        "sso_unrestricted_provisioning",
        "api_key_grants",
        "autonomy_gating",
        "mcp_human_linked_hitl_authority",
        "system_maps",
    }
)


class CommunityTier:
    """Default plan — community-tier features active without a license key.
    Backward-compatible class satisfying the PlanContext protocol.
    """

    def __init__(self) -> None:
        self._registry = FeatureFlagRegistry(current_tier="community", has_license_key=False)

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active

    def list_enabled_features(self) -> list[FeatureFlag]:
        return [f for f in self._registry.list_flags() if f.currently_active]

    def list_all_features(self) -> list[FeatureFlag]:
        """Every known flag (active and inactive) for this context's tier state."""
        return self._registry.list_flags()

    def tier(self) -> str:
        return self._registry.current_tier

    def has_license_key(self) -> bool:
        return self._registry.has_license_key


class LicenseKeyTier:
    """Licensed plan — activates features based on license tier and explicit feature list.
    Backward-compatible class satisfying the PlanContext protocol.
    """

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
        return [f for f in self._registry.list_flags() if f.currently_active or f.name in self._features]

    def list_all_features(self) -> list[FeatureFlag]:
        """Every known flag; licence-named features read active regardless of tier state."""
        return [
            replace(flag, currently_active=True) if flag.name in self._features else flag
            for flag in self._registry.list_flags()
        ]

    def tier(self) -> str:
        return self._registry.current_tier

    def has_license_key(self) -> bool:
        return self._registry.has_license_key


class PlanContext(Protocol):
    """Interface for plan-based feature gating."""

    def feature_enabled(self, name: str) -> bool: ...

    def list_enabled_features(self) -> list[FeatureFlag]: ...

    def list_all_features(self) -> list[FeatureFlag]: ...

    def tier(self) -> str: ...

    def has_license_key(self) -> bool: ...


class DbPlanContext:
    """Plan context resolved from the DB-backed tier catalog."""

    def __init__(self, registry: FeatureFlagRegistry) -> None:
        self._registry = registry

    @classmethod
    async def from_db(
        cls,
        session: Any,
        plan_id: str,
        has_license_key: bool = False,
        license_features: set[str] | None = None,
    ) -> DbPlanContext:
        """Resolve a plan context from the tier catalog in the database."""
        registry = await FeatureFlagRegistry.from_db(session, plan_id, has_license_key)

        if license_features:
            for flag in registry.list_flags():
                if flag.name in license_features:
                    flag.currently_active = True

        return cls(registry)

    def tier(self) -> str:
        return self._registry.current_tier

    def has_license_key(self) -> bool:
        return self._registry.has_license_key

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active

    def list_enabled_features(self) -> list[FeatureFlag]:
        return [f for f in self._registry.list_flags() if f.currently_active]

    def list_all_features(self) -> list[FeatureFlag]:
        """Every known flag (active and inactive); licence features are folded in by ``from_db``."""
        return self._registry.list_flags()


def _verified_license_key(raw_key: str, error_log: str) -> LicenseData | None:
    """Verify one key rung of the licence ladder; ``None`` when absent or invalid.

    Session-free. An absent key resolves to ``None`` without touching the
    verifier; an unexpected parse failure is logged under ``error_log`` and
    treated as absent — the exact fall-through behaviour (and per-rung
    observability: each rung passes its own ``error_log``) of the key rungs of
    ``resolve_effective_license``, the shared ladder helper, which resolves
    both key rungs through ``_verified_license_key`` (this per-key verifier).
    Cancellation propagates (``CancelledError`` is a ``BaseException``, never
    swallowed by the ``except Exception``).

    A key carrying a FUTURE ``starts_at`` (FAR-2146) verifies but is not yet
    active: it resolves to ``None`` here, so the rung falls through and the
    instance keeps running on whatever licence is in force today. Without this gate a
    configured env-var or org-level successor would grant Team capability the
    day it was written into configuration, before the window the customer
    bought — the free-time defect the future-only rule exists to prevent. It
    becomes effective automatically once ``starts_at`` arrives, because the
    gate is evaluated on every resolution.
    """
    from modulo.core.license import parse_and_verify

    if not raw_key:
        return None
    try:
        validation = parse_and_verify(raw_key)
    except Exception:
        logger.warning(error_log, exc_info=True)
        return None
    if validation.valid and validation.license_data is not None:
        if is_parked(validation.license_data):
            logger.info(
                "license.key_parked_until_start org_id=%s starts_at=%s",
                validation.license_data.org_id,
                validation.license_data.starts_at,
            )
            return None
        return validation.license_data
    return None


def _license_claims_org(data: LicenseData, org: Any) -> bool:
    """Whether an org-scoped licence may license ``org`` (FAR-2060).

    An empty ``org_id`` claim is instance-wide and applies anywhere. A non-empty
    claim names ONE organisation, and applying it to any other tenant would let
    org B's key license org A — so the claim is enforced here, at the point the
    key is APPLIED, not only at install time (a stored key can predate this
    check, and ``parse_and_verify`` never inspects the claim).
    """
    claimed = getattr(data, "org_id", "") or ""
    if not claimed:
        return True
    return claimed == str(getattr(org, "id", ""))


def resolve_effective_license(settings: Any, org: Any | None) -> LicenseData | None:
    """The canonical licence ladder, session-free — first valid rung wins.

    Rungs (absent/invalid falls through; a key is verified via
    ``parse_and_verify``, never trusted on mere presence — FAR-1733):

    1. Org-level key (``org.settings_json["license_key"]``) — applied ONLY when
       its ``org_id`` claim is empty or names this org (FAR-2060). A mismatch is
       treated as absent at this rung: the key is real, but it licenses a
       different tenant.
    2. System-level in-memory licence (``store_license()``) — the runtime install
       persisted in ``system_config`` (FAR-2153) and restored at startup. This
       rung OVERRIDES rung 3: an explicit operator install beats the deployment
       env key, and removing it falls back to the env key.
    3. System-level env-var key (``settings.modulo_license_key``) — the
       deployment FLOOR.

    ONE walk, shared by ``_resolve_paid_plan`` (the plan context built here),
    ``admin_license._resolve_effective_license`` (the admin licence view) and
    ``viewmodel._resolve_license_expiry`` / ``_resolve_tier`` /
    ``_resolve_license_info`` (the member-facing plan bootstrap), so a future
    rung change cannot silently drift between the copies. Returns ``None`` when
    no valid licence resolves.

    Grace: capability expiry is ``expires_at + LICENCE_GRACE_DAYS`` (a fixed,
    non-configurable delivery window in ``modulo.core.license``), so a key that
    expired fewer than ``LICENCE_GRACE_DAYS`` ago still resolves here and keeps
    granting its tier. Past the grace every rung falls through to the
    unlicensed (Community) default — the lapse is never a lockout, because
    authentication does not depend on this ladder.

    A key whose signed ``starts_at`` is still in the future (FAR-2146) also
    falls through: it is valid but not yet active, so it must not grant
    capability early. It starts resolving here automatically once its start
    arrives — no operator action, because the gate is evaluated per call.

    Local imports keep ``modulo.core.license.{get_license,parse_and_verify}``
    patchable at call time (tests patch the source module, not this one).
    """
    from modulo.core.license import get_license

    # 1. Org-level license key
    if org is not None:
        org_settings = getattr(org, "settings_json", None)
        org_key = org_settings.get("license_key") if isinstance(org_settings, dict) else None
        if org_key:
            data = _verified_license_key(str(org_key), "Failed to parse org-level license key")
            if data is not None and _license_claims_org(data, org):
                return data
            if data is not None:
                logger.warning(
                    "license.org_id_mismatch",
                    extra={
                        "org_id": str(getattr(org, "id", "")),
                        "claimed_org_id": data.org_id,
                    },
                )

    # 2. System-level in-memory license (stored pre-verified; expiry re-checked by get_license)
    lic = get_license()
    if lic is not None:
        return lic

    # 3. System-level env-var license
    return _verified_license_key(
        getattr(settings, "modulo_license_key", "") or "",
        "Failed to parse env-var license key",
    )


async def _resolve_paid_plan(settings: Any, session: Any, org: Any | None) -> PlanContext | None:
    """Resolve a licensed (paid) plan context, or ``None`` when no valid license exists.

    License ladder (first valid source wins, absent/invalid falls through):
    1. Org-level license key (from ``org.settings_json["license_key"]``)
    2. System-level in-memory license (``store_license()``; the
       ``system_config`` install restored/refreshed from persistence)
    3. System-level env-var license (``settings.modulo_license_key``)

    Rung 2 OVERRIDES rung 3: the deployment's env key is the FLOOR and an
    explicit operator install is the override, so removing an install falls back
    to the env key (FAR-2153). The persisted system licence is re-read here (TTL
    throttled) before the walk, so a licence installed by another process
    becomes visible in this one without a restart.

    The rung WALK is the shared session-free ``resolve_effective_license`` —
    only the session-backed context build is local to this function — so this
    ladder cannot drift from the admin licence view or ``GET /api/v1/plan``.
    """
    if session is not None:
        # FAR-2153: cross-process visibility. TTL-throttled and fail-open, so
        # this is at most one indexed read per window and a DB blip cannot turn
        # a plan resolution into an error.
        from modulo.core.license import refresh_persisted_system_license

        await refresh_persisted_system_license(session)
    data = resolve_effective_license(settings, org)
    if data is None:
        return None
    return await DbPlanContext.from_db(
        session,
        data.tier,
        has_license_key=True,
        license_features=set(data.features),
    )


async def _resolve_trial_state(session: Any, org: Any | None) -> PlanContext:
    """Resolve the unlicensed fallback state once no valid license matched.

    4. Org-level ``plan_id`` — used ONLY for the free "community" tier. Any
       higher tier (e.g. "team") requires a VALID SIGNED LICENSE; the license
       ladder in ``_resolve_paid_plan`` already returned when one exists, so
       reaching here with a paid plan_id means NO license is present. Downgrade
       to community instead of silently granting the paid tier from a bare
       plan_id.
    5. Community tier (default fallback)
    """
    if org is not None:
        org_plan_id: str | None = getattr(org, "plan_id", None)
        if org_plan_id:
            if org_plan_id == "community":
                return await DbPlanContext.from_db(session, "community")
            logger.info(
                "plan.team_without_license_falling_back_to_community",
                extra={"org_plan_id": org_plan_id},
            )

    # 5. Community fallback
    return await DbPlanContext.from_db(session, "community")


async def resolve_plan_context(settings: Any, session: Any, org: Any | None = None) -> PlanContext:
    """Resolve a PlanContext from an org-level license, system-level license, or CommunityTier.

    Resolution order:
    1. Org-level license key (from ``org.settings_json["license_key"]``)
    2. System-level in-memory license (``store_license()``)
    3. System-level env-var license (``settings.modulo_license_key``)
    4. Org-level ``plan_id`` — used ONLY for the free "community" tier. Any
       higher tier (e.g. "team") requires a VALID SIGNED LICENSE; a bare
       ``plan_id`` with no license (steps 1-3 all empty) resolves to community.
    5. Community tier (default fallback)

    Steps 1-3 delegate to ``_resolve_paid_plan`` (license ladder); steps 4-5
    delegate to ``_resolve_trial_state`` (unlicensed fallback).
    """
    licensed = await _resolve_paid_plan(settings, session, org)
    if licensed is not None:
        return licensed
    return await _resolve_trial_state(session, org)


def feature_overrides_from_settings(settings_json: Any) -> dict[str, bool]:
    """The org ``feature_overrides`` map (bool entries only) from a settings dict.

    Shared by the admin feature-flag list (``admin_feature_flags._read_org_overrides``)
    and the member-facing plan bootstrap (``viewmodel._org_feature_overrides``)
    so the bool-only rule cannot drift between them: only ``bool`` entries count
    as an override (``1``/``0`` and other truthy junk are ignored), and a
    missing or malformed settings / ``feature_overrides`` value resolves to
    ``{}`` — junk settings can never flip a flag's reported state.
    """
    if not isinstance(settings_json, dict):
        return {}
    overrides = settings_json.get("feature_overrides")
    if not isinstance(overrides, dict):
        return {}
    return {key: bool(value) for key, value in overrides.items() if isinstance(value, bool)}


def effective_feature_flags(
    plan_ctx: PlanContext,
    org_overrides: Mapping[str, bool] | None = None,
) -> list[FeatureFlag]:
    """The full known flag set with the org ``feature_overrides`` overlay applied.

    FAR-1806: the caller-scoped effective flag payload served to any
    authenticated org member (``GET /api/v1/plan`` and, there,
    ``/viewmodel/current``'s ``feature_flags``). Two gaps make
    ``PlanContext.list_enabled_features()`` unusable as a bootstrap payload:

    1. It is active-only — inactive flags are dropped entirely, so the client
       cannot tell "off" from "unknown" and default-OFF chrome never resolves.
    2. No resolution step overlays the org's ``feature_overrides``; only the
       admin feature-flag list applied that overlay (manually), which is why
       the admin endpoint and ``viewmodel/current`` disagreed on the same org.

    This helper closes both: it starts from ``list_all_features()`` (the full
    known set with tier state computed by the plan context) and overlays
    ``org_overrides`` exactly as the admin list endpoint does, so this payload
    and the admin endpoint report the same effective state for an org.
    Overlay entries naming unknown flags are ignored (matching the admin
    list); inactive flags stay present with ``currently_active=False``.

    Precedence note: the org overlay lands ON TOP of the plan context's state,
    mirroring the admin list — any system-level registry override
    (``FeatureFlagRegistry.set_override``) is already folded into each flag's
    ``currently_active`` by the registry before this overlay applies.

    Internal-only flags (``INTERNAL_FLAGS``) are filtered from customer-facing
    catalogue and plan responses — they are visible only in dev-mode to internal
    operators via the admin feature-flag endpoints and the MCP ``get_available_features``
    tool.

    Returns copies (``dataclasses.replace``) for overlaid flags so the plan
    context's own registry state is never mutated.
    """
    internal_and_retired = RETIRED_INTERNAL_FLAGS | INTERNAL_FLAGS
    flags = [flag for flag in plan_ctx.list_all_features() if flag.name not in internal_and_retired]
    if not org_overrides:
        return flags
    return [
        replace(flag, currently_active=org_overrides[flag.name]) if flag.name in org_overrides else flag
        for flag in flags
    ]


class FeatureFlagRegistry:
    """Knows all feature flags and determines active status from the current tier.

    Falls back to hardcoded ``_KNOWN_FLAGS`` / ``TIER_RANK`` when DB-backed data
    has not been loaded.  Call ``load_from_db()`` to replace with catalog data.
    """

    _overrides: ClassVar[dict[str, bool]] = {}

    def __init__(self, current_tier: str = "community", has_license_key: bool = False) -> None:
        self._current_tier = current_tier
        self._has_license_key = has_license_key
        self._flags = [FeatureFlag(**asdict(f)) for f in _KNOWN_FLAGS]
        self._refresh()

    async def _load_catalog(self, session: Any) -> None:
        """Read the tier/feature-flag catalog rows into this registry.

        Callers must guarantee an active transaction (``load_from_db`` wraps
        this in ``session.begin()`` unless one is already active).
        """
        from modulo.db.crud.tier_catalog import list_feature_flags, list_tiers

        db_tiers = await list_tiers(session)
        if db_tiers:
            self._tier_rank = {t["tier_id"]: t["rank"] for t in db_tiers}

        db_flags = await list_feature_flags(session)
        if db_flags:
            # Keep ALL catalog rows, including ``is_active=false`` ones, so the
            # admin UI can list and toggle them. Rows seeded inactive (e.g. the
            # ``mobile_sidebar_rail`` experiment) are tracked in
            # ``_inactive_flags`` and stay ``currently_active=False`` in
            # ``_refresh`` unless an override exists.
            self._flags = [
                FeatureFlag(
                    name=f["name"],
                    description=f["description"],
                    tier=f["tier_id"],
                    depends_on=f["depends_on"],
                )
                for f in db_flags
            ]
            self._inactive_flags = {f["name"] for f in db_flags if not f["is_active"]}

    async def load_from_db(self, session: Any) -> None:
        """Replace hardcoded flag data with DB-backed data from tier_catalog / feature_flag_catalog.

        The catalog reads are wrapped in an explicit transaction so callers
        using ``autobegin=False`` sessions (the DI default) never hit
        ``InvalidRequestError`` from a bare ``session.execute``. When the
        caller already manages an active transaction the reads run inside it
        (a nested ``session.begin()`` would raise).
        """
        in_transaction = session.in_transaction()
        if asyncio.iscoroutine(in_transaction):
            # Test doubles may expose an async ``in_transaction``; await it.
            in_transaction = await in_transaction
        if in_transaction:
            await self._load_catalog(session)
        else:
            async with session.begin():
                await self._load_catalog(session)
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

    @property
    def current_tier(self) -> str:
        return self._current_tier

    @property
    def has_license_key(self) -> bool:
        return self._has_license_key

    def _effective_tier_ranks(self) -> dict[str, int]:
        """The tier -> rank map this registry ranks flags against.

        Prefers the DB-backed catalog ranks loaded by ``_load_catalog`` and
        falls back to the hardcoded ``TIER_RANK`` before/without a DB load, so
        every rank comparison uses the same map that produced
        ``currently_active``.
        """
        return getattr(self, "_tier_rank", TIER_RANK)

    def tier_rank(self, tier: str) -> int:
        """Rank of ``tier`` using this registry's effective rank map.

        Unknown tiers rank 0 (community-equivalent), matching ``_refresh``.
        """
        return self._effective_tier_ranks().get(tier, 0)

    def _refresh(self) -> None:
        tier_rank = self._effective_tier_ranks()
        current_rank = tier_rank.get(self._current_tier, 0)
        # Flags seeded ``is_active=false`` in the DB catalog (experiments that
        # must ship default-OFF everywhere). ``__init__`` runs ``_refresh()``
        # before ``_load_catalog``, so guard with getattr. The hardcoded
        # ``DEFAULT_OFF_FLAGS`` set wins over the tier-rank fallback AND over a
        # DB catalog row marked active — only an explicit override (org
        # ``feature_overrides`` / system ``set_override``) can turn them on.
        inactive: set[str] = getattr(self, "_inactive_flags", set()) | DEFAULT_OFF_FLAGS

        for flag in self._flags:
            if flag.name in inactive:
                flag.currently_active = False
            else:
                flag_tier_rank = tier_rank.get(flag.tier, 0)
                flag.currently_active = flag_tier_rank <= current_rank

            override = self._overrides.get(flag.name)
            if override is not None:
                flag.currently_active = override

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
        tier_rank = self._effective_tier_ranks()
        community_rank = tier_rank.get("community", 0)
        return [f for f in self._flags if tier_rank.get(f.tier, 0) > community_rank and not f.currently_active]

    async def resolve_flag(
        self,
        flag_name: str,
        org_id: uuid.UUID | None = None,
        *,
        raise_on_error: bool = False,
    ) -> bool:
        """Resolve a flag from two layers: system override > org override > tier default.

        ``raise_on_error=True`` (strict, for enforcement callers that must not
        mistake a transient DB failure for "flag OFF") propagates the org
        override read error instead of falling through to the tier default.
        Default behaviour (swallow + log, fall through) is unchanged.

        Team-level and user-level overrides were removed (FAR-927). The
        resolution order is now:
            system override > org override > tier default.
        """
        sys_override = self._overrides.get(flag_name)
        if sys_override is not None:
            return sys_override

        if org_id is not None:
            org_val = await self._get_org_override(flag_name, org_id, raise_on_error=raise_on_error)
            if org_val is not None:
                return org_val

        for flag in self._flags:
            if flag.name == flag_name:
                return flag.currently_active
        return False

    async def _override_from_entity(
        self,
        flag_name: str,
        load_row_factory: Callable[[], Callable[[Any], Awaitable[Any]]],
        settings_attr: str,
        error_log: str,
        *,
        raise_on_error: bool = False,
    ) -> bool | None:
        """Resolve a ``feature_overrides`` entry from a single org/team/account row.

        Shared session/transaction/error boilerplate for the three entity-scoped
        flag overrides. ``load_row_factory`` returns a session-bound loader for
        the entity (so its lazy crud import happens inside this method's try
        block); ``settings_attr`` names the JSON column that carries
        ``feature_overrides`` (``settings_json`` / ``settings`` / ``preferences``)
        and ``error_log`` is logged when the lookup fails. Returns the override
        value when the entity defines one, else ``None``.
        """
        try:
            from sqlalchemy.ext.asyncio import AsyncSession

            from modulo.api.dependencies import get_or_create_engine
            from modulo.settings import get_settings

            load_row = load_row_factory()
            engine = get_or_create_engine(get_settings())
            async with AsyncSession(engine, autobegin=False) as session:
                # When the caller already owns an active transaction (the common
                # request path where DI provides the session), run the read inside
                # it — a nested ``session.begin()`` would raise. Otherwise open an
                # explicit transaction: the settings column MUST be read inside the
                # open transaction, because after ``begin()`` commits ORM attributes
                # are expired and reading them triggers a refresh requiring a new
                # transaction, which raises ``InvalidRequestError`` on an
                # ``autobegin=False`` session (FAR-820) — silently nullifying every
                # org/team/user override.
                in_transaction = session.in_transaction()
                if asyncio.iscoroutine(in_transaction):
                    # Test doubles may expose an async ``in_transaction``; await it.
                    in_transaction = await in_transaction
                if in_transaction:
                    entity = await load_row(session)
                    settings = getattr(entity, settings_attr, None) if entity is not None else None
                else:
                    async with session.begin():
                        entity = await load_row(session)
                        settings = getattr(entity, settings_attr, None) if entity is not None else None
                if entity and settings:
                    overrides = settings.get("feature_overrides", {})
                    if flag_name in overrides:
                        return bool(overrides[flag_name])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(error_log)
            if raise_on_error:
                raise
        return None

    async def _get_org_override(
        self, flag_name: str, org_id: uuid.UUID, *, raise_on_error: bool = False
    ) -> bool | None:
        """Check org.settings_json.feature_overrides for this flag."""

        def _load_factory() -> Callable[[Any], Awaitable[Any]]:
            from modulo.db.crud.organisation import get_organisation

            async def _load(session: Any) -> Any:
                return await get_organisation(session, org_id)

            return _load

        return await self._override_from_entity(
            flag_name,
            _load_factory,
            "settings_json",
            "Failed to check org flag override",
            raise_on_error=raise_on_error,
        )


_registry: FeatureFlagRegistry | None = None


def get_registry() -> FeatureFlagRegistry:
    """Return a process-global default FeatureFlagRegistry.

    The registry uses the hardcoded ``_KNOWN_FLAGS`` list with a ``"community"``
    tier.  Granular overrides (org) are resolved from the DB at query
    time via ``resolve_flag()``.
    """
    global _registry
    if _registry is None:
        _registry = FeatureFlagRegistry(current_tier="community")
    return _registry


SSO_UNRESTRICTED_PROVISIONING_FLAG = "sso_unrestricted_provisioning"


async def resolve_sso_unrestricted_provisioning(*, org_id: uuid.UUID | None) -> bool:
    """FAR-855: resolve the operator-level SSO unrestricted-provisioning flag.

    This flag is NOT a plan-tier feature: it is default-OFF on every tier
    (``DEFAULT_OFF_FLAGS``) and only an explicit operator-level override —
    system ``FeatureFlagRegistry.set_override`` or an org
    ``feature_overrides`` entry — can enable it. It gates the DANGEROUS
    "anyone who authenticates" SSO join mode (``auto_provision=true`` with an
    empty ``allowed_domains``).

    SECURITY: fails CLOSED. Any error resolving the flag denies the
    permissive mode, because flag resolution failing must never be the
    thing that widens an auth boundary.
    """
    try:
        registry = get_registry()
        return bool(await registry.resolve_flag(SSO_UNRESTRICTED_PROVISIONING_FLAG, org_id=org_id))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("sso_unrestricted_provisioning flag read failed; failing closed")
        return False


async def resolve_mcp_human_linked_hitl_authority(*, org_id: uuid.UUID | None, flag_name: str) -> bool:
    """Resolve the org-wide human-linked MCP authority flag (ADR 063 step 1).

    ``flag_name`` is supplied by the gate site (``api/mcp_server.py``), so the
    gate names the flag it enforces rather than the resolver hiding the name
    inside this registry module — the architecture reference-integrity guard
    treats a reference in a registry file as no reference at all (FAR-1957).

    This flag is NOT a plan-tier feature: it is **default OFF** on every tier
    (``DEFAULT_OFF_FLAGS``). An org admin may explicitly enable it through
    ``org.settings_json.feature_overrides[flag_name]``. When enabled, a verified
    human-linked credential may decide ``human_only`` HITL gates over MCP based
    on persona; possession remains out of scope.

    SECURITY: fails CLOSED to OFF (deny). The permissive state is ON, so a
    failed org-override read must NOT fall through to the permissive tier
    default — hence ``raise_on_error=True`` (which surfaces the read error
    instead of silently returning ``currently_active``) and the ``except``
    returning False. A flag-resolution failure can never widen the auth
    boundary.
    """
    try:
        registry = get_registry()
        return bool(
            await registry.resolve_flag(
                flag_name,
                org_id=org_id,
                raise_on_error=True,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("mcp_human_linked_hitl_authority flag read failed; failing closed")
        return False
