"""Feature flag registry — catalogs all known feature flags and their current status.

Tier structure (matching the DB tier_catalog):
    community (0) — Free tier, all features active without a license
    team      (1) — The one paid tier
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Protocol

from modulo.core.license import LicenseData

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
        name="eval_maturity",
        description=(
            "Generic eval suite/dataset maturity model (FAR-374). Gates the new "
            "EvalSuite entity, endpoints, and UI behind a flag so the legacy "
            "suite_id behaviour is untouched until explicitly enabled."
        ),
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
    FeatureFlag(
        name="dashboard_charts",
        description="Dashboard trend charts (run activity sparklines)",
        tier="community",
    ),
    FeatureFlag(
        name="polling_trigger",
        description="Trigger pipelines by polling external endpoints",
        tier="community",
    ),
    FeatureFlag(
        name="agent_signal_trigger",
        description="Trigger pipelines via agent-to-agent signals",
        tier="community",
    ),
    FeatureFlag(
        name="ongoing_trigger",
        description="Keep a pipeline topped up to a target number of in-flight runs",
        tier="community",
    ),
    FeatureFlag(
        name="web_vitals_analytics",
        description="Web Vitals analytics dashboard for monitoring frontend performance",
        tier="community",
    ),
    FeatureFlag(
        name="remy",
        description="Remy in-app AI assistant",
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
        name="external_secrets",
        description="External secrets backends (Vault, AWS, 1Password, Azure Key Vault)",
        tier="team",
    ),
    FeatureFlag(
        name="schema_union_types",
        description="Union types and polymorphic schemas",
        tier="team",
    ),
    FeatureFlag(
        name="migration_cli",
        description="CLI tool for migrating pipelines across instances",
        tier="team",
    ),
    FeatureFlag(
        name="checkpoint_encryption",
        description="Encrypt pipeline checkpoints at rest",
        tier="team",
    ),
    FeatureFlag(
        name="audit_crypto_chain",
        description="Cryptographic chaining of audit events for tamper evidence",
        tier="team",
    ),
    FeatureFlag(
        name="community_registry",
        description="Publish and discover community pipeline primitives",
        tier="team",
    ),
    FeatureFlag(
        name="prompt_optimization",
        description="Automated prompt tuning and optimisation",
        tier="team",
    ),
    FeatureFlag(
        name="pipeline_diff_rollback",
        description="Diff-based pipeline version comparison and rollback",
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
        name="remy_ui_driving",
        description="Remy browser UI driving — allows Remy to navigate, click, and fill forms on your behalf.",
        tier="community",
    ),
    FeatureFlag(
        name="pipeline_delete",
        description="Allow hard-deleting pipelines from the UI",
        tier="team",
    ),
    # ── In-Dev / community-visible but hidden from sidebar ──────────────
    FeatureFlag(
        name="notification_log",
        description="In-app notification delivery log",
        tier="community",
    ),
    FeatureFlag(
        name="api_changelog",
        description="API changelog and version history",
        tier="community",
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
    # Seeded with ``is_active=false`` so it is listed-but-inactive on every tier
    # (community-tier flags otherwise activate everywhere via the tier-rank
    # comparison). Org admins can enable it per-org through the Feature Flags UI
    # (sets an org ``feature_overrides`` entry, which wins in ``_refresh``).
    FeatureFlag(
        name="mobile_sidebar_rail",
        description="Mobile icon-rail sidebar (experimental)",
        tier="community",
    ),
]


# community=Free, team=one paid tier
TIER_RANK: dict[str, int] = {"community": 0, "team": 1}


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

    def tier(self) -> str:
        return self._registry.current_tier

    def has_license_key(self) -> bool:
        return self._registry.has_license_key


class PlanContext(Protocol):
    """Interface for plan-based feature gating."""

    def feature_enabled(self, name: str) -> bool: ...

    def list_enabled_features(self) -> list[FeatureFlag]: ...

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
    """
    from modulo.core.license import get_license, parse_and_verify

    # 1. Org-level license key
    if org is not None:
        org_settings = getattr(org, "settings_json", None)
        org_license_key = org_settings.get("license_key") if isinstance(org_settings, dict) else None
        if org_license_key:
            try:
                validation = parse_and_verify(org_license_key)
                if validation.valid and validation.license_data is not None:
                    return await DbPlanContext.from_db(
                        session,
                        validation.license_data.tier,
                        has_license_key=True,
                        license_features=set(validation.license_data.features),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Failed to parse org-level license key", exc_info=True)

    # 2. System-level in-memory license
    lic = get_license()
    if lic is not None:
        return await DbPlanContext.from_db(
            session,
            lic.tier,
            has_license_key=True,
            license_features=set(lic.features),
        )

    # 3. System-level env-var license
    raw_key: str = getattr(settings, "modulo_license_key", "") or ""
    if raw_key:
        try:
            validation = parse_and_verify(raw_key)
            if validation.valid and validation.license_data is not None:
                return await DbPlanContext.from_db(
                    session,
                    validation.license_data.tier,
                    has_license_key=True,
                    license_features=set(validation.license_data.features),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to parse env-var license key", exc_info=True)

    # 4. Org-level plan_id (per-org, from DB). Community is the free tier and
    #    activates without a license. Any higher tier (e.g. "team") may only
    #    grant paid features when a VALID SIGNED LICENSE is present — steps 1-3
    #    already returned when one exists, so reaching here with a paid plan_id
    #    means NO license is present. Downgrade to community instead of silently
    #    granting the paid tier from a bare plan_id.
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

    def _refresh(self) -> None:
        tier_rank: dict[str, int] = getattr(self, "_tier_rank", TIER_RANK)
        current_rank = tier_rank.get(self._current_tier, 0)
        # Flags seeded ``is_active=false`` in the DB catalog (experiments that
        # must ship default-OFF everywhere). ``__init__`` runs ``_refresh()``
        # before ``_load_catalog``, so guard with getattr. ``mobile_sidebar_rail``
        # is always in the inactive set — it is default-OFF by definition, so it
        # must not come active via the tier-rank fallback when the DB catalog is
        # empty; only an explicit ``_overrides`` entry can turn it on.
        inactive: set[str] = getattr(self, "_inactive_flags", set()) | {
            "mobile_sidebar_rail",
            "dashboard_charts",
            "saved_views",
        }

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
        tier_rank: dict[str, int] = getattr(self, "_tier_rank", TIER_RANK)
        community_rank = tier_rank.get("community", 0)
        return [f for f in self._flags if tier_rank.get(f.tier, 0) > community_rank and not f.currently_active]

    async def resolve_flag(
        self,
        flag_name: str,
        org_id: uuid.UUID | None = None,
        team_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> bool:
        """Resolve a flag with org/team/user overrides in resolution order:

        user > team > org > system default.
        """
        sys_override = self._overrides.get(flag_name)
        if sys_override is not None:
            return sys_override

        if user_id is not None:
            user_val = await self._get_user_override(flag_name, user_id)
            if user_val is not None:
                return user_val

        if team_id is not None:
            team_val = await self._get_team_override(flag_name, team_id)
            if team_val is not None:
                return team_val

        if org_id is not None:
            org_val = await self._get_org_override(flag_name, org_id)
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
                in_transaction = session.in_transaction()
                if asyncio.iscoroutine(in_transaction):
                    in_transaction = await in_transaction
                if in_transaction:
                    entity = await load_row(session)
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
        return None

    async def _get_org_override(self, flag_name: str, org_id: uuid.UUID) -> bool | None:
        """Check org.settings_json.feature_overrides for this flag."""

        def _load_factory() -> Callable[[Any], Awaitable[Any]]:
            from modulo.db.crud.organisation import get_organisation

            async def _load(session: Any) -> Any:
                return await get_organisation(session, org_id)

            return _load

        return await self._override_from_entity(
            flag_name, _load_factory, "settings_json", "Failed to check org flag override"
        )

    async def _get_team_override(self, flag_name: str, team_id: uuid.UUID) -> bool | None:
        """Check team.settings.feature_overrides for this flag."""

        def _load_factory() -> Callable[[Any], Awaitable[Any]]:
            from modulo.db.crud.team import get_team

            async def _load(session: Any) -> Any:
                return await get_team(session, team_id)

            return _load

        return await self._override_from_entity(
            flag_name, _load_factory, "settings", "Failed to check team flag override"
        )

    async def _get_user_override(self, flag_name: str, user_id: uuid.UUID) -> bool | None:
        """Check account.preferences.feature_overrides for this flag."""

        def _load_factory() -> Callable[[Any], Awaitable[Any]]:
            from modulo.db.crud.account import get_account_by_id

            async def _load(session: Any) -> Any:
                return await get_account_by_id(session, user_id)

            return _load

        return await self._override_from_entity(
            flag_name, _load_factory, "preferences", "Failed to check user flag override"
        )


_registry: FeatureFlagRegistry | None = None


def eval_maturity_enabled(plan: PlanContext | None) -> bool:
    """Fail-closed read of the ``eval_maturity`` flag (FAR-374).

    Returns ``False`` whenever the flag cannot be resolved — on a missing plan
    context, a raised error, or any uncertainty. Callers MUST treat ``False`` as
    "use the legacy ``suite_id`` behaviour"; this guarantees we never route to
    the new EvalSuite path on error.

    Snapshot this value once at the start of a run/request (not per call): every
    call site that touches EvalSuite grouping should read it a single time and
    thread the boolean through, so a mid-run flag flip cannot split a single
    operation across legacy/new behaviour.
    """
    if plan is None:
        return False
    try:
        return bool(plan.feature_enabled("eval_maturity"))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("eval_maturity flag read failed; failing closed to legacy path")
        return False


def get_registry() -> FeatureFlagRegistry:
    """Return a process-global default FeatureFlagRegistry.

    The registry uses the hardcoded ``_KNOWN_FLAGS`` list with a ``"community"``
    tier.  Granular overrides (org/team/user) are resolved from the DB at query
    time via ``resolve_flag()``.
    """
    global _registry
    if _registry is None:
        _registry = FeatureFlagRegistry(current_tier="community")
    return _registry
