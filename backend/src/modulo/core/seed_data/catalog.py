"""Default tier catalog and feature flag definitions for seeding.

These constants seed the DB tables tier_catalog and feature_flag_catalog
at application startup.  They are seed data only — the runtime feature flag
registry lives in modulo.core.feature_flags.
"""

TIERS: list[dict[str, str | int | bool]] = [
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

FLAGS: list[dict[str, str | None]] = [
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
        "name": "eval_maturity",
        "description": (
            "Generic eval suite/dataset maturity model (FAR-374). Gates the new "
            "EvalSuite entity, endpoints, and UI behind a flag so the legacy "
            "suite_id behaviour is untouched until explicitly enabled."
        ),
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
        "name": "dashboard_charts",
        "description": "Dashboard trend charts (run activity sparklines)",
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
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "admin_cost_controls",
        "description": "Budget overview, team budgets, alert thresholds",
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
        "name": "remy_ui_driving",
        "description": "Remy browser UI driving",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "model_backend_management",
        "description": "Manage LLM backend connections and credentials",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "user_management",
        "description": "Basic user management — create, deactivate, and role-assign organisation users",
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
        "name": "ongoing_trigger",
        "description": "Keep a pipeline topped up to a target number of in-flight runs",
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
        "description": "Monthly cost breakdown and anomaly detection",
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
    {
        "name": "analytics_page",
        "description": "Run analytics dashboard (rolling-window run/cost/quality series)",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "error_tracking",
        "description": "External error tracking and alerting integrations",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "runtime_config",
        "description": "Runtime configuration overrides",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "rate_limits",
        "description": "Configure API rate limits",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "email_config",
        "description": "SMTP email configuration for notifications",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "scim",
        "description": "SCIM 2.0 user and group provisioning",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "external_secrets",
        "description": "External secrets backends (Vault, AWS, 1Password, Azure Key Vault)",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "checkpoint_encryption",
        "description": "Encrypt pipeline checkpoints at rest",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "audit_crypto_chain",
        "description": "Cryptographic chaining of audit events for tamper evidence",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "community_registry",
        "description": "Publish and discover community pipeline primitives",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "prompt_optimization",
        "description": "Automated prompt tuning and optimisation",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "pipeline_diff_rollback",
        "description": "Diff-based pipeline version comparison and rollback",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "pipeline_delete",
        "description": "Allow hard-deleting pipelines from the UI",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "schema_union_types",
        "description": "Union types and polymorphic schemas",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "migration_cli",
        "description": "CLI tool for migrating pipelines across instances",
        "tier_id": "team",
        "depends_on": None,
    },
    {
        "name": "notification_log",
        "description": "In-app notification delivery log",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "api_changelog",
        "description": "API changelog and version history",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "web_vitals_analytics",
        "description": "Web Vitals analytics dashboard for monitoring frontend performance",
        "tier_id": "community",
        "depends_on": None,
    },
    {
        "name": "mobile_sidebar_rail",
        "description": "Mobile icon-rail sidebar (experimental)",
        "tier_id": "community",
        "depends_on": None,
    },
]
