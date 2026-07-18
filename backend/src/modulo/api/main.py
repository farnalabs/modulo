import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Protocol, cast

import anyio
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from modulo.api.dependencies import (
    get_or_create_engine,
    get_or_create_session_factory,
    pg_connection_string,
)
from modulo.api.exception_handlers import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from modulo.api.mcp_server import build_mcp_asgi_app
from modulo.api.middleware.catch_all import CatchAllMiddleware
from modulo.api.middleware.correlation_id import CorrelationIdMiddleware
from modulo.api.middleware.cors_logging import CorsLoggingMiddleware
from modulo.api.middleware.csrf import CsrfMiddleware
from modulo.api.middleware.deprecation_headers import DeprecationHeaderMiddleware
from modulo.api.middleware.rate_limiter import AuthRateLimitMiddleware, RateLimitMiddleware, shutdown_rate_limiters
from modulo.api.middleware.request_timeout import RequestTimeoutMiddleware
from modulo.api.middleware.security_headers import SecurityHeadersMiddleware
from modulo.api.middleware.sensitive_mask import router as sensitive_router
from modulo.api.routes.admin import router as admin_router
from modulo.api.routes.admin_email import router as admin_email_router
from modulo.api.routes.admin_feature_flags import router as admin_feature_flags_router
from modulo.api.routes.admin_license import router as admin_license_router
from modulo.api.routes.admin_monitor_config import router as admin_monitor_config_router
from modulo.api.routes.admin_notifications import router as admin_notifications_router
from modulo.api.routes.admin_orgs import router as admin_orgs_router
from modulo.api.routes.admin_rate_limits import router as admin_rate_limits_router
from modulo.api.routes.admin_remy import router as admin_remy_router
from modulo.api.routes.admin_rotation import router as admin_rotation_router
from modulo.api.routes.admin_runtime_config import router as admin_runtime_config_router
from modulo.api.routes.admin_sso import router as admin_sso_router
from modulo.api.routes.admin_system_config import router as admin_system_config_router
from modulo.api.routes.admin_tiers import router as admin_tiers_router
from modulo.api.routes.admin_triggers import router as admin_triggers_router
from modulo.api.routes.agents import router as agents_router
from modulo.api.routes.api_keys import router as api_keys_router
from modulo.api.routes.audit import router as audit_router
from modulo.api.routes.auth import router as auth_router
from modulo.api.routes.changelog import router as changelog_router
from modulo.api.routes.composite_templates import router as composite_templates_router
from modulo.api.routes.connectors import router as connectors_router
from modulo.api.routes.contributions import router as contributions_router
from modulo.api.routes.costs import router as costs_router
from modulo.api.routes.dashboard import router as dashboard_router
from modulo.api.routes.deployment import router as deployment_router
from modulo.api.routes.determination import router as determination_router
from modulo.api.routes.environment_profiles import router as environment_profiles_router
from modulo.api.routes.environments import router as environments_router
from modulo.api.routes.error_forwarder_config import router as error_forwarder_config_router
from modulo.api.routes.error_notification_rules import router as error_notification_rules_router
from modulo.api.routes.errors import router as errors_router
from modulo.api.routes.evals import router as evals_router
from modulo.api.routes.events import router as events_router
from modulo.api.routes.feedback import router as feedback_router
from modulo.api.routes.health import router as health_router
from modulo.api.routes.hitl import router as hitl_router
from modulo.api.routes.in_app_notifications import router as in_app_notifications_router
from modulo.api.routes.library import router as library_router
from modulo.api.routes.lifecycle_maps import router as lifecycle_maps_router
from modulo.api.routes.manifest import router as manifest_router
from modulo.api.routes.mcp_oauth import router as mcp_oauth_router
from modulo.api.routes.mcp_setup import router as mcp_setup_router
from modulo.api.routes.me import router as me_router
from modulo.api.routes.metrics import router as metrics_router
from modulo.api.routes.model_backends import router as model_backends_router
from modulo.api.routes.node_categories import router as node_categories_router
from modulo.api.routes.notifications import router as notifications_router
from modulo.api.routes.observability import router as observability_router
from modulo.api.routes.onboarding import router as onboarding_router
from modulo.api.routes.parameter_schemas import router as parameter_schemas_router
from modulo.api.routes.pipeline_folders import router as pipeline_folders_router
from modulo.api.routes.pipelines import router as pipelines_router
from modulo.api.routes.plugins import router as plugins_router
from modulo.api.routes.registry import router as registry_router
from modulo.api.routes.remy import router as remy_router
from modulo.api.routes.run_ws import router as run_ws_router
from modulo.api.routes.runs import router as runs_router
from modulo.api.routes.schemas import router as schemas_router
from modulo.api.routes.scim import router as scim_router
from modulo.api.routes.sso import router as sso_router
from modulo.api.routes.stages import router as stages_router
from modulo.api.routes.teams import router as teams_router
from modulo.api.routes.templates import router as templates_router
from modulo.api.routes.triggers import pipeline_triggers_router
from modulo.api.routes.triggers import router as triggers_router
from modulo.api.routes.variants import router as variants_router
from modulo.api.routes.viewmodel import router as viewmodel_router
from modulo.api.routes.views import router as views_router
from modulo.api.routes.webhooks import router as webhooks_router
from modulo.core.events.event_bus import configure_event_bus
from modulo.core.events.listeners import register_listeners
from modulo.core.graceful_shutdown import ShutdownManager, ShutdownMiddleware
from modulo.core.hitl_manager.expiry_job import ClaimExpiryJob
from modulo.core.in_process_scheduler import dispose_scheduler_engine, start_schedulers
from modulo.core.logging_config import configure_logging
from modulo.core.seed_data.catalog import FLAGS, TIERS
from modulo.db.session import engine as db_engine
from modulo.otel_bridge import setup_otel, shutdown_otel
from modulo.settings import Settings, get_settings

# Uptime tracking — set at module import time, read by health endpoints.
logger = logging.getLogger(__name__)


class _TaskGroupSessionManager(Protocol):
    """FastMCP session-manager surface used by the application lifespan."""

    _task_group: anyio.abc.TaskGroup | None


_START_TIME = datetime.now(UTC)

# Graceful shutdown manager — resources registered during lifespan startup.
_shutdown_manager = ShutdownManager()


async def _verify_db_connectivity(settings: Settings) -> None:
    """Check database connectivity without preventing application startup."""
    engine = get_or_create_engine(settings)
    for attempt in range(1, 4):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            logger.info("startup.db_connected")
            return
        except Exception as exc:
            logger.warning(
                "startup.db_connectivity_attempt_failed",
                extra={"attempt": attempt, "error": str(exc)},
                exc_info=True,
            )
            if attempt < 3:
                await asyncio.sleep(attempt * 2)
    logger.error("startup.db_unreachable")
    logger.warning("startup.continuing_without_db — app will retry connections at runtime")


async def _run_migrations(settings: Settings) -> None:
    """Migrations are run by entrypoint.sh before uvicorn starts."""
    logger.info("startup.migrations_handled_by_entrypoint")


async def _ensure_default_org(settings: Settings) -> None:
    """Create a default organisation if none exists."""
    from sqlalchemy import select

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.db.models.organisation import Organisation

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async with factory() as session, session.begin():
        result = await session.execute(select(Organisation).limit(1))
        if result.scalar_one_or_none() is not None:
            logger.info("startup.org_exists")
            return

        org = Organisation(
            name="Default Organisation",
            slug="default",
        )
        session.add(org)
        await session.flush()
        logger.info("startup.default_org_created", extra={"org_id": str(org.id)})


async def _seed_modulo_users(settings: Settings) -> None:
    """Seed MODULO_USERS env var entries into the account + membership tables.

    Accepts both bcrypt hashes (user1:$2b$12$hash) and plaintext passwords
    (admin:admin). Plaintext passwords are auto-hashed with bcrypt at seed time.
    Skips if MODULO_USERS is empty or no organisation exists.
    """
    if not settings.modulo_users:
        return

    from sqlalchemy import select

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.auth.passwords import hash_password
    from modulo.db.models.account import Account
    from modulo.db.models.org_membership import OrgMembership
    from modulo.db.models.organisation import Organisation

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async with factory() as session, session.begin():
        org_result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
        org = org_result.scalar_one_or_none()
        if org is None:
            logger.warning("startup.no_org_for_user_seed")
            return

        for entry in settings.modulo_users.split(","):
            entry = entry.strip()
            if not entry:
                continue
            colon = entry.find(":")
            if colon < 1:
                continue
            email = entry[:colon]
            pw_part = entry[colon + 1 :]

            result = await session.execute(select(Account).where(Account.email == email))
            existing_account = result.scalar_one_or_none()
            pw_hash = pw_part if pw_part.startswith("$2") else hash_password(pw_part)

            if existing_account is not None and (
                not existing_account.password_hash or not existing_account.password_hash.startswith("$2")
            ):
                existing_account.password_hash = pw_hash
                logger.info("startup.user_rehashed", extra={"email": email})

                # Ensure OrgMembership exists and role is correct
                mem_result = await session.execute(
                    select(OrgMembership).where(
                        OrgMembership.account_id == existing_account.id,
                        OrgMembership.organisation_id == org.id,
                    )
                )
                membership = mem_result.scalar_one_or_none()
                admin_role = "admin" if email in ("admin", "admin@modulo.run") else None
                if membership is not None:
                    if admin_role and membership.role != "admin":
                        membership.role = "admin"
                        logger.info("startup.user_role_set_admin", extra={"email": email})
                    else:
                        logger.info("startup.user_exists", extra={"email": email})
                else:
                    new_membership = OrgMembership(
                        account_id=existing_account.id,
                        organisation_id=org.id,
                        role=admin_role or "runner",
                    )
                    session.add(new_membership)
                    logger.info("startup.user_membership_created", extra={"email": email})
                continue

            if existing_account is not None:
                logger.info("startup.user_exists", extra={"email": email})
                continue

            account = Account(
                email=email,
                display_name=email.split("@")[0],
                password_hash=pw_hash,
                auth_provider="local",
            )
            session.add(account)
            await session.flush()

            membership = OrgMembership(
                account_id=account.id,
                organisation_id=org.id,
                role="admin" if email in ("admin", "admin@modulo.run") else "runner",
            )
            session.add(membership)
            logger.info("startup.user_seeded", extra={"email": email})


async def _seed_demo_data(settings: Settings) -> None:
    """Seed rich demo data when MODULO_DEMO_MODE is enabled.

    Creates a demo account with admin role, sample pipelines,
    schemas, model backends, connectors, library primitives,
    stages, and other resources for find-and-fix exploration.
    """
    if not settings.modulo_demo_mode:
        return

    from sqlalchemy import select

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.auth.passwords import hash_password
    from modulo.db.models.account import Account
    from modulo.db.models.model_backend import ModelBackend
    from modulo.db.models.org_membership import OrgMembership
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.schema import Schema, SchemaVersion
    from modulo.db.models.stage import Stage
    from modulo.db.rls import set_rls_org

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async with factory() as session, session.begin():
        org_result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
        org = org_result.scalar_one_or_none()
        if org is None:
            logger.warning("startup.demo_no_org")
            return

        org_id = org.id

        # Set org to Team Plan so all team-tier features are active
        if org.plan_id != "team":
            org.plan_id = "team"
            logger.info("startup.demo_org_plan_set_to_team")

        # Seed or update demo account with admin role
        demo_email = "demo"
        result = await session.execute(select(Account).where(Account.email == demo_email))
        demo_account = result.scalar_one_or_none()
        if demo_account is None:
            demo_account = Account(
                email=demo_email,
                display_name="Demo User",
                password_hash=hash_password("demo"),
                auth_provider="local",
            )
            session.add(demo_account)
            await session.flush()

            membership = OrgMembership(
                account_id=demo_account.id,
                organisation_id=org_id,
                role="admin",
            )
            session.add(membership)
            logger.info("startup.demo_user_seeded")

        # Ensure admin user also has admin role
        admin_result = await session.execute(select(Account).where(Account.email == "admin"))
        admin_account = admin_result.scalar_one_or_none()
        if admin_account is not None:
            admin_membership_result = await session.execute(
                select(OrgMembership).where(
                    OrgMembership.account_id == admin_account.id,
                    OrgMembership.organisation_id == org_id,
                )
            )
            admin_membership = admin_membership_result.scalar_one_or_none()
            if admin_membership is not None and admin_membership.role != "admin":
                admin_membership.role = "admin"
                logger.info("startup.admin_role_upgraded")

        await session.flush()

        # Set RLS context for subsequent queries
        await set_rls_org(session, org_id)

        # Seed a sample pipeline
        existing_pipelines = await session.execute(select(Pipeline).where(Pipeline.organisation_id == org_id).limit(1))
        if existing_pipelines.scalar_one_or_none() is None:
            pipeline = Pipeline(
                organisation_id=org_id,
                account_id=demo_account.id,
                name="Demo Pipeline",
                description="A sample pipeline for exploration",
                graph_nodes_json=[{"id": "node-1", "type": "input", "label": "Start"}],
            )
            session.add(pipeline)
            logger.info("startup.demo_pipeline_seeded")

        # Seed a sample schema + version
        existing_schemas = await session.execute(select(Schema).where(Schema.organisation_id == org_id).limit(1))
        if existing_schemas.scalar_one_or_none() is None:
            schema = Schema(
                organisation_id=org_id,
                account_id=demo_account.id,
                name="Demo Schema",
                description="A sample schema for exploration",
            )
            session.add(schema)
            await session.flush()

            schema_version = SchemaVersion(
                organisation_id=org_id,
                account_id=demo_account.id,
                schema_id=schema.id,
                version="v1",
                version_number=1,
                definition_json={"type": "object", "properties": {}},
            )
            session.add(schema_version)
            logger.info("startup.demo_schema_seeded")

        # Seed a sample model backend
        existing_mbs = await session.execute(
            select(ModelBackend).where(ModelBackend.organisation_id == org_id).limit(1)
        )
        if existing_mbs.scalar_one_or_none() is None:
            mb = ModelBackend(
                organisation_id=org_id,
                account_id=demo_account.id,
                name="Stub Model (Demo)",
                display_name="Stub Model (Demo)",
                provider="stub",
                model_id="demo-model",
                credentials_ciphertext=b"demo-encrypted",
            )
            session.add(mb)
            logger.info("startup.demo_model_backend_seeded")

        # Seed a sample stage
        existing_stages = await session.execute(select(Stage).where(Stage.organisation_id == org_id).limit(1))
        if existing_stages.scalar_one_or_none() is None:
            stage = Stage(
                organisation_id=org_id,
                account_id=demo_account.id,
                name="Development",
                description="Development stage for testing pipelines",
                position=0,
            )
            session.add(stage)
            logger.info("startup.demo_stage_seeded")

        logger.info("startup.demo_data_ready")


async def _seed_sso_providers(settings: Settings) -> None:
    """Seed SSO providers from MODULO_OIDC_PROVIDERS env var into DB table.

    This is a one-time migration from the deprecated env-var approach to the
    DB-backed admin UI approach. Skips if MODULO_OIDC_PROVIDERS is empty,
    or if any providers already exist in the DB.
    """
    if not settings.modulo_oidc_providers or settings.modulo_oidc_providers == "[]":
        return

    from sqlalchemy import select

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.auth.secret_storage import encrypt_stored_secret
    from modulo.db.models.sso_provider import SsoProvider

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async with factory() as session, session.begin():
        existing = await session.execute(select(SsoProvider).limit(1))
        if existing.scalar_one_or_none() is not None:
            return

        try:
            entries = json.loads(settings.modulo_oidc_providers)
        except (json.JSONDecodeError, TypeError):
            logger.warning("startup.sso_providers_invalid_json")
            return

        required_fields = ("provider_id", "client_id", "client_secret", "discovery_url")
        for entry in entries:
            if not isinstance(entry, dict) or any(key not in entry for key in required_fields):
                logger.warning("startup.sso_provider_skipped", extra={"entry": str(entry)})
                continue

            provider = SsoProvider(
                provider_type="oidc",
                name=entry.get("provider_id", entry.get("name", "Imported OIDC Provider")),
                client_id=entry["client_id"],
                client_secret=encrypt_stored_secret(entry["client_secret"], settings.fernet_key),
                discovery_url=entry["discovery_url"],
                scopes=json.dumps(["openid", "profile", "email"]),
                enabled=True,
                auto_provision=True,
                default_role=settings.modulo_sso_default_role,
            )
            session.add(provider)
            logger.info(
                "startup.sso_provider_seeded",
                extra={"provider_id": entry["provider_id"]},
            )


async def _seed_environment_profiles(settings: Settings) -> None:
    """Seed a default modulo-dev EnvironmentProfile for the default org.

    Creates a reusable sandbox profile for the dogfood pipeline. Skips if
    a profile named 'modulo-dev' already exists.
    """
    from sqlalchemy import select

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.db.crud.environment_profile import create_environment_profile
    from modulo.db.models.account import Account
    from modulo.db.models.environment_profile import EnvironmentProfile
    from modulo.db.models.organisation import Organisation

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async with factory() as session, session.begin():
        org_result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
        org = org_result.scalar_one_or_none()
        if org is None:
            logger.warning("startup.no_org_for_env_profile_seed")
            return

        existing = await session.execute(
            select(EnvironmentProfile).where(
                EnvironmentProfile.organisation_id == org.id,
                EnvironmentProfile.name == "modulo-dev",
            )
        )
        if existing.scalar_one_or_none() is not None:
            logger.info("startup.env_profile_modulo_dev_exists")
            return

        admin_result = await session.execute(
            select(Account).where(Account.email == "admin").order_by(Account.created_at).limit(1)
        )
        admin = admin_result.scalar_one_or_none()
        if admin is None:
            admin_result = await session.execute(select(Account).order_by(Account.created_at).limit(1))
            admin = admin_result.scalar_one_or_none()
            if admin is None:
                logger.warning("startup.no_admin_for_env_profile_seed")
                return

        await create_environment_profile(
            session,
            org_id=org.id,
            name="modulo-dev",
            description="Default sandbox for Modulo dogfood development. Python 3.12, git, pip.",
            provider_type="local_docker",
            image_ref="python:3.12-slim",
            capabilities=["git", "python>=3.12", "shell", "network:github.com", "network:pypi.org"],
            network_policy="outbound",
            initialisation_strategy="git_clone",
            persistence_policy="ephemeral",
            account_id=admin.id,
        )
        logger.info("startup.env_profile_modulo_dev_seeded")


async def _init_checkpointer(conn_string: str, fernet_key: str, fernet_key_old: str = "") -> None:
    """Ensure the langgraph.* checkpointer schema exists on startup."""
    import uuid

    try:
        from modulo.core.pipeline_engine.modulo_saver import ModuloPostgresSaver

        async with ModuloPostgresSaver.from_conn_string(
            conn_string,
            organisation_id=uuid.UUID(int=0),
            fernet_key=fernet_key,
            fernet_key_old=fernet_key_old or None,
        ) as saver:
            await saver.setup()
            logger.info("startup.checkpointer_initialised")
    except Exception:
        logger.warning("startup.checkpointer_init_failed", exc_info=True)


async def _run_retention_loop(interval_seconds: int = 3600) -> None:
    """Background loop: batch-delete terminal runs older than 90 days."""
    settings = get_settings()
    factory = get_or_create_session_factory(get_or_create_engine(settings))
    while True:
        try:
            async with factory() as session, session.begin():
                from modulo.db.crud.run import batch_delete_old_terminal_runs

                deleted = await batch_delete_old_terminal_runs(session)
                if deleted:
                    logger.info("retention.deleted_old_runs", extra={"count": deleted})
        except Exception:
            logger.exception("retention.job_failed")
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Configure structured JSON logging first so all startup logs are structured.
    configure_logging()

    # Calling get_settings() at startup triggers pydantic validation — if
    # SECRET_KEY or FERNET_KEY are missing, too short, or a known placeholder,
    # the validator raises and the process exits before accepting requests.
    settings = get_settings()

    if settings.modulo_public_url in ("", "http://localhost:8000"):
        logger.warning("startup.default_public_url")

    if settings.modulo_license_public_key:
        from modulo.core.license import set_public_key

        set_public_key(settings.modulo_license_public_key)
        logger.info("startup.license_public_key_configured")
    elif not settings.debug:
        logger.warning("startup.default_license_key_in_use")
    from modulo.core.license import check_production_public_key

    try:
        check_production_public_key(settings)
    except Exception:
        logger.warning("startup.production_public_key_check_failed", exc_info=True)

    if settings.modulo_db.lower() == "sqlite":
        logger.warning("startup.sqlite_mode")

    if not settings.redis_url:
        raise RuntimeError(
            "REDIS_URL is required. Modulo uses Redis for event coordination, rate limiting, "
            "caching, and session state. Provision Upstash Redis and set REDIS_URL in fly.toml."
        )
    logger.info("startup.redis_configured — Celery beat available for distributed scheduling")
    _scheduler_tasks = await start_schedulers(engine=db_engine)

    setup_otel(
        service_name=settings.modulo_otel_service_name,
        telemetry_enabled=settings.modulo_telemetry_enabled,
    )

    # Discover installed plugins if plugin discovery is enabled.
    if settings.modulo_plugin_discovery:
        from modulo.core.plugin_registry import get_plugin_registry

        registry = get_plugin_registry()
        discovered = registry.discover_plugins()
        if discovered:
            logger.info(
                "startup.plugins_discovered",
                extra={"count": len(discovered), "plugins": [p.PLUGIN_ID for p in discovered]},
            )
        else:
            logger.info("startup.no_plugins_discovered")
    else:
        logger.info("startup.plugin_discovery_disabled")

    logger.info("startup.starting")

    # Verify the database is reachable before accepting requests.
    await _verify_db_connectivity(settings)

    # Run Alembic migrations to bring the schema up to date.
    await _run_migrations(settings)

    # Ensure at least one organisation exists before seeding users.
    # Non-fatal: if the organisations table doesn't exist (migration state
    # mismatch), the app starts without an org and retries on next restart.
    try:
        await _ensure_default_org(settings)
    except Exception:
        logger.warning("startup.default_org_failed", exc_info=True)

    # Seed MODULO_USERS env var entries into the user table (idempotent).
    # Non-fatal: if tables are missing, seeding is retried on next restart.
    try:
        await _seed_modulo_users(settings)
    except Exception:
        logger.warning("startup.user_seed_failed", exc_info=True)

    # Seed demo data if MODULO_DEMO_MODE is enabled.
    try:
        await _seed_demo_data(settings)
    except Exception:
        logger.warning("startup.demo_seed_failed", exc_info=True)

    # Seed SSO providers from deprecated env vars into the DB table (idempotent).
    try:
        await _seed_sso_providers(settings)
    except Exception:
        logger.warning("startup.sso_providers_seed_failed", exc_info=True)

    # Seed the default modulo-dev EnvironmentProfile for the dogfood pipeline.
    try:
        await _seed_environment_profiles(settings)
    except Exception:
        logger.warning("startup.env_profile_seed_failed", exc_info=True)

    # Seed the tier catalog and feature flag definitions (idempotent).
    try:
        await _seed_tier_catalog(settings)
    except Exception:
        logger.warning("startup.tier_catalog_seed_failed", exc_info=True)

    # Initialise the LangGraph checkpointer schema (langgraph.* tables).
    try:
        await _init_checkpointer(
            pg_connection_string(settings.database_url),
            settings.fernet_key,
            fernet_key_old=settings.fernet_key_old,
        )
    except Exception:
        logger.warning("startup.checkpointer_init_failed_during_lifespan", exc_info=True)

    # Initialise the runtime-config store so it captures env-var state at boot.
    from modulo.core.runtime_config.store import get_runtime_config_store

    get_runtime_config_store()

    # Start the background pipeline worker (after migrations and checkpointer init).
    try:
        from modulo.api.mcp_server import set_background_worker as set_mcp_bg_worker
        from modulo.api.routes.runs import set_background_worker as set_runs_bg_worker
        from modulo.core.background_pipeline_worker import BackgroundPipelineWorker

        _bg_worker = BackgroundPipelineWorker(
            database_url=str(settings.database_url),
            checkpointer_conn_string=pg_connection_string(settings.database_url),
        )
        await _bg_worker.start()
        set_runs_bg_worker(_bg_worker)
        set_mcp_bg_worker(_bg_worker)
        from modulo.api.routes.health import set_worker_ref as set_health_bg_worker

        set_health_bg_worker(_bg_worker)
    except Exception:
        logger.warning("startup.background_worker_init_failed", exc_info=True)

    # Initialise the graceful shutdown manager with the configured timeout.
    # Two session factories exist:
    #   - modulo.db.session    (module-level, used by entrypoint.sh + ClaimExpiryJob)
    #   - modulo.api.dependencies  (DI-injected, used by all route handlers)
    # Both point to the same DB URL but have separate connection pools.  They
    # are intentionally decoupled — the entrypoint runs before FastAPI is
    # initialised and can't use DI.  Dispose both so no connections leak.
    try:
        _di_engine = get_or_create_engine(settings)

        async def shutdown_otel_async() -> None:
            shutdown_otel()

        _shutdown_manager.register("otel", shutdown_otel_async)
        _shutdown_manager.register("db_engine", db_engine.dispose)
        _shutdown_manager.register("di_engine", _di_engine.dispose)
        _shutdown_manager.register("rate_limiter_redis", shutdown_rate_limiters)
        _shutdown_manager.register("scheduler_engine", dispose_scheduler_engine)
    except Exception:
        logger.warning("startup.shutdown_manager_init_failed", exc_info=True)

    # Start the run retention background loop.
    retention_task = asyncio.create_task(_run_retention_loop())

    # Register SQLAlchemy event listeners for resource-change events.
    register_listeners()

    # Configure the EventBus with Redis broker if Redis is available.
    if settings.redis_url:
        from modulo.core.events.redis_broker import RedisEventBroker

        redis_broker = RedisEventBroker(settings.redis_url)
        await configure_event_bus(redis_broker=redis_broker)
        logger.info("startup.event_bus_redis_enabled")

    # Start the HITL claim expiry background job.
    _claim_expiry_job = ClaimExpiryJob(db_engine)
    await _claim_expiry_job.start()

    # Start MCP task group so FastMCP's _handle_stateless_request can use tg.start().
    from modulo.api.mcp_server import mcp

    _mcp_tg = await anyio.create_task_group().__aenter__()
    # FastMCP annotates this private integration slot as None despite assigning a TaskGroup at runtime.
    session_manager = cast(_TaskGroupSessionManager, mcp.session_manager)
    session_manager._task_group = _mcp_tg

    yield

    await _mcp_tg.__aexit__(None, None, None)
    retention_task.cancel()
    await _claim_expiry_job.stop()
    for st in _scheduler_tasks:
        st.cancel()
    try:
        await retention_task
        for st in _scheduler_tasks:
            with suppress(asyncio.CancelledError):
                await st
    except asyncio.CancelledError:
        pass
    await dispose_scheduler_engine()
    with suppress(NameError):
        await _bg_worker.stop()
    await _shutdown_manager.shutdown()


async def _seed_tier_catalog(settings: object) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from modulo.db.session import engine as db_engine

    async with AsyncSession(db_engine, autobegin=False) as session, session.begin():
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


app = FastAPI(
    title="Modulo",
    description="Governed orchestration for your agentic SDLC",
    version="0.1.0",
    lifespan=_lifespan,
)

_settings = get_settings()
_cors_origins = [o.strip() for o in _settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CorsLoggingMiddleware,  # type: ignore[arg-type]
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Origin",
        "X-Request-ID",
        "X-CSRF-Token",
    ],
    max_age=_settings.cors_max_age,
)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(CsrfMiddleware)
app.add_middleware(RateLimitMiddleware)  # type: ignore[arg-type]
app.add_middleware(AuthRateLimitMiddleware)  # type: ignore[arg-type]
app.add_middleware(DeprecationHeaderMiddleware)  # type: ignore[arg-type]
DeprecationHeaderMiddleware.deprecate(
    "/api/v1/system-admin/config",
    sunset="2027-01-01",
    migration_url="/docs/operations/migrations/v1-config-to-admin",
)
app.add_middleware(SecurityHeadersMiddleware)  # type: ignore[arg-type]
app.add_middleware(CatchAllMiddleware)
app.add_middleware(ShutdownMiddleware, manager=_shutdown_manager)
app.add_middleware(RequestTimeoutMiddleware, timeout_seconds=120, overrides={"/healthz": 5, "/healthz/ready": 15})

app.include_router(health_router)
app.include_router(admin_router)
app.include_router(admin_email_router)
app.include_router(admin_feature_flags_router)
app.include_router(admin_license_router)
app.include_router(admin_rate_limits_router)
app.include_router(admin_runtime_config_router)
app.include_router(admin_sso_router)
app.include_router(admin_system_config_router)
app.include_router(admin_tiers_router)
app.include_router(admin_triggers_router)
app.include_router(auth_router)
app.include_router(changelog_router)
app.include_router(sso_router)
app.include_router(dashboard_router)
app.include_router(deployment_router)
app.include_router(costs_router)
app.include_router(teams_router)
app.include_router(pipelines_router)
app.include_router(pipeline_folders_router)
app.include_router(agents_router)
app.include_router(parameter_schemas_router)
app.include_router(hitl_router)
app.include_router(schemas_router)
app.include_router(model_backends_router)
app.include_router(node_categories_router)
app.include_router(composite_templates_router)
app.include_router(connectors_router)
app.include_router(contributions_router)
app.include_router(runs_router)
app.include_router(run_ws_router)
app.include_router(triggers_router)
app.include_router(pipeline_triggers_router)
app.include_router(webhooks_router)
app.include_router(views_router)
app.include_router(viewmodel_router)
app.include_router(api_keys_router)
app.include_router(audit_router)
app.include_router(library_router)
app.include_router(lifecycle_maps_router)
app.include_router(mcp_oauth_router)
app.include_router(mcp_setup_router)
app.include_router(me_router)
app.include_router(registry_router)
app.include_router(determination_router)
app.include_router(evals_router)
app.include_router(admin_notifications_router)
app.include_router(admin_orgs_router)
app.include_router(admin_remy_router)
app.include_router(admin_monitor_config_router)
app.include_router(admin_rotation_router)
app.include_router(in_app_notifications_router)
app.include_router(notifications_router)
app.include_router(sensitive_router)
app.include_router(observability_router)
app.include_router(variants_router)
app.include_router(feedback_router)
app.include_router(plugins_router)
app.include_router(scim_router)
app.include_router(stages_router)
app.include_router(templates_router)
app.include_router(onboarding_router)
app.include_router(environments_router)
app.include_router(environment_profiles_router)
app.include_router(error_forwarder_config_router)
app.include_router(error_notification_rules_router)
app.include_router(errors_router)
app.include_router(events_router)
app.include_router(remy_router)
app.include_router(manifest_router)
app.include_router(metrics_router)

# Strip router lifespan contexts — none of the 68+ routers register
# on_startup/on_shutdown handlers, so every _DefaultLifespan is a no-op.
# Keeping the deeply nested _merge_lifespan_context chain causes infinite
# recursion in Docker builds (FastAPI 0.139.0, Python 3.12, Linux).
app.router.lifespan_context = _lifespan

# Remote MCP server — mounted as a Starlette sub-app at /mcp.
# Auth is enforced by McpAuthMiddleware inside the sub-app.
app.mount("/mcp", build_mcp_asgi_app())

app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(Exception, unhandled_exception_handler)
