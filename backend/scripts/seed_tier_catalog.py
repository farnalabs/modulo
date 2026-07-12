"""Seed the tier catalog tables with default tier definitions.

Usage:
    uv run python -m scripts.seed_tier_catalog

Requires DATABASE_URL env var or a running modulo instance with settings loaded.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from modulo.settings import get_settings

TIERS = [
    {
        "tier_id": "community",
        "label": "Community",
        "rank": 0,
        "requires_license": False,
        "description": "Free tier, no license key required",
    },
    {
        "tier_id": "team",
        "label": "Team",
        "rank": 1,
        "requires_license": True,
        "description": "Self-serve paid tier with team features",
    },
]

# Only flags assigned to a seeded tier go here. Flags not yet assigned to any tier
# (future features, undelivered) stay in _KNOWN_FLAGS (hardcoded fallback) and
# never activate until a license key grants their tier. Adding a new tier means
# INSERT into tier_catalog above + optionally moving flags from _KNOWN_FLAGS here.
FLAGS = [
    {
        "name": "parallel_branches",
        "description": "Run branching logic in parallel within a pipeline",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "eval_system",
        "description": "Built-in eval runner for LLM output quality gates",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "webhook_trigger",
        "description": "Trigger pipelines via incoming webhooks",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "cron_trigger",
        "description": "Schedule pipeline runs on a cron expression",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "mcp_server",
        "description": "Expose pipelines as MCP tools",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "community_library",
        "description": "Browse and import community-contributed pipeline primitives",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "saved_views",
        "description": "Persistent saved views for run and pipeline lists",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "sso",
        "description": "Single sign-on via OIDC / SAML 2.0 providers",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "team_rbac",
        "description": "Team-level role-based access control",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "audit_viewer",
        "description": "Tamper-evident audit log viewer",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "admin_spend_limits",
        "description": "Per-organisation daily spend limits and budgets",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "observability",
        "description": "OpenTelemetry export and LangSmith integration settings",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "admin_cost_controls",
        "description": "Budget overview, team budgets, alert thresholds, and billing settings",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "view_modes",
        "description": "Multiple named UI views with admin-defined feature visibility",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "model_backend_management",
        "description": "Manage LLM backend connections and credentials",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "remy",
        "description": "Remy in-app AI assistant",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "polling_trigger",
        "description": "Trigger pipelines by polling external endpoints",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "agent_signal_trigger",
        "description": "Trigger pipelines via agent-to-agent signals",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "helm_deployment",
        "description": "Helm chart for production Kubernetes deployment",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "environment_profiles",
        "description": "Sandbox environment profiles for code execution",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "plugin_management",
        "description": "Manage plugins, connectors, and node categories",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "admin_cost_breakdown",
        "description": "Monthly cost breakdown and anomaly detection across teams",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "admin_run_retention",
        "description": "Configure run retention policies and manual purge",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "error_forwarders",
        "description": "External error tracking and alerting integrations",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "schema_version_history",
        "description": "Version history and diff for schema definitions",
        "tier_id": "team",
        "depends_on": None,
    },
]


async def seed() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with AsyncSession(engine) as session:
        for tier in TIERS:
            await session.execute(
                text("""
                    INSERT INTO tier_catalog (tier_id, label, rank, requires_license, description)
                    VALUES (:tier_id, :label, :rank, :requires_license, :description)
                    ON CONFLICT (tier_id) DO NOTHING
                """),
                tier,
            )
        for flag in FLAGS:
            await session.execute(
                text("""
                    INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active)
                    VALUES (:name, :description, :tier_id, :depends_on, true)
                    ON CONFLICT (name) DO NOTHING
                """),
                flag,
            )
        await session.commit()
    await engine.dispose()
    print("Tier catalog seeded successfully.")


if __name__ == "__main__":
    asyncio.run(seed())
