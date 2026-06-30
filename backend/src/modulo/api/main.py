import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

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
    validation_exception_handler,
)
from modulo.api.mcp_server import build_mcp_asgi_app
from modulo.api.middleware.catch_all import CatchAllMiddleware
from modulo.api.middleware.correlation_id import CorrelationIdMiddleware
from modulo.api.middleware.cors_logging import CorsLoggingMiddleware
from modulo.api.middleware.csrf import CsrfMiddleware
from modulo.api.middleware.deprecation_headers import DeprecationHeaderMiddleware
from modulo.api.middleware.rate_limiter import AuthRateLimitMiddleware, RateLimitMiddleware, shutdown_rate_limiters
from modulo.api.middleware.security_headers import SecurityHeadersMiddleware
from modulo.api.middleware.sensitive_mask import router as sensitive_router
from modulo.api.routes.admin import router as admin_router
from modulo.api.routes.admin_feature_flags import router as admin_feature_flags_router
from modulo.api.routes.admin_license import router as admin_license_router
from modulo.api.routes.admin_notifications import router as admin_notifications_router
from modulo.api.routes.admin_rate_limits import router as admin_rate_limits_router
from modulo.api.routes.admin_rotation import router as admin_rotation_router
from modulo.api.routes.admin_runtime_config import router as admin_runtime_config_router
from modulo.api.routes.admin_sso import router as admin_sso_router
from modulo.api.routes.admin_triggers import router as admin_triggers_router
from modulo.api.routes.agents import router as agents_router
from modulo.api.routes.api_keys import router as api_keys_router
from modulo.api.routes.audit import router as audit_router
from modulo.api.routes.auth import router as auth_router
from modulo.api.routes.changelog import router as changelog_router
from modulo.api.routes.connectors import router as connectors_router
from modulo.api.routes.contributions import router as contributions_router
from modulo.api.routes.costs import router as costs_router
from modulo.api.routes.dashboard import router as dashboard_router
from modulo.api.routes.deployment import router as deployment_router
from modulo.api.routes.determination import router as determination_router
from modulo.api.routes.environments import router as environments_router
from modulo.api.routes.evals import router as evals_router
from modulo.api.routes.feedback import router as feedback_router
from modulo.api.routes.health import router as health_router
from modulo.api.routes.hitl import router as hitl_router
from modulo.api.routes.library import router as library_router
from modulo.api.routes.mcp_oauth import router as mcp_oauth_router
from modulo.api.routes.me import router as me_router
from modulo.api.routes.model_backends import router as model_backends_router
from modulo.api.routes.node_categories import router as node_categories_router
from modulo.api.routes.notifications import router as notifications_router
from modulo.api.routes.observability import router as observability_router
from modulo.api.routes.onboarding import router as onboarding_router
from modulo.api.routes.pipelines import router as pipelines_router
from modulo.api.routes.plugins import router as plugins_router
from modulo.api.routes.registry import router as registry_router
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
from modulo.core.graceful_shutdown import ShutdownManager, ShutdownMiddleware
from modulo.core.hitl_manager.expiry_job import ClaimExpiryJob
from modulo.core.in_process_scheduler import dispose_scheduler_engine, start_schedulers
from modulo.core.logging_config import configure_logging
from modulo.db.session import engine as db_engine
from modulo.otel_bridge import setup_otel, shutdown_otel
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Uptime tracking — set at module import time, read by health endpoints.
_START_TIME = datetime.now(UTC)

# Graceful shutdown manager — resources registered during lifespan startup.
_shutdown_manager = ShutdownManager()


async def _verify_db_connectivity(settings: Settings) -> None:
    """Verify the database is reachable at startup.

    Raises RuntimeError if the DB is unreachable after several retries,
    preventing the app from starting in a broken state.
    """
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
            )
            if attempt < 3:
                await asyncio.sleep(attempt * 2)
    logger.error("startup.db_unreachable")


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

    async with factory() as session:
        async with session.begin():
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
    """Seed MODULO_USERS env var entries into the user table on first boot.

    Accepts both bcrypt hashes (user1:$2b$12$hash) and plaintext passwords
    (admin:admin). Plaintext passwords are auto-hashed with bcrypt at seed time.
    Skips if MODULO_USERS is empty or no organisation exists.
    """
    if not settings.modulo_users:
        return

    from sqlalchemy import select

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.auth.passwords import hash_password
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.user import User

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async with factory() as session:
        async with session.begin():
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

                result = await session.execute(select(User).where(User.email == email, User.organisation_id == org.id))
                existing = result.scalar_one_or_none()
                pw_hash = pw_part if pw_part.startswith("$2") else hash_password(pw_part)

                if existing is not None:
                    if not existing.password_hash or not existing.password_hash.startswith("$2"):
                        existing.password_hash = pw_hash
                        logger.info("startup.user_rehashed", extra={"email": email})
                    admin_role = "admin" if email in ("admin", "admin@modulo.run") else None
                    if admin_role and existing.org_role != "admin":
                        existing.org_role = "admin"
                        logger.info("startup.user_role_set_admin", extra={"email": email})
                    else:
                        logger.info("startup.user_exists", extra={"email": email})
                    continue

                user = User(
                    organisation_id=org.id,
                    email=email,
                    display_name=email.split("@")[0],
                    password_hash=pw_hash,
                    org_role="admin" if email in ("admin", "admin@modulo.run") else "runner",
                    auth_provider="local",
                )
                session.add(user)
                logger.info("startup.user_seeded", extra={"email": email})


async def _seed_demo_data(settings: Settings) -> None:
    """Seed demo data when MODULO_DEMO_MODE is enabled.

    Creates a read-only demo user. Future iterations may seed sample
    pipelines, agents, schemas, and connectors.
    """
    if not settings.modulo_demo_mode:
        return

    from sqlalchemy import select

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.auth.passwords import hash_password
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.user import User

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async with factory() as session:
        async with session.begin():
            org_result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
            org = org_result.scalar_one_or_none()
            if org is None:
                logger.warning("startup.demo_no_org")
                return

            demo_email = "demo"
            result = await session.execute(select(User).where(User.email == demo_email, User.organisation_id == org.id))
            if result.scalar_one_or_none() is None:
                demo_user = User(
                    organisation_id=org.id,
                    email=demo_email,
                    display_name="Demo User",
                    password_hash=hash_password("demo"),
                    org_role="viewer",
                    auth_provider="local",
                )
                session.add(demo_user)
                logger.info("startup.demo_user_seeded")

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
    from modulo.db.models.sso_provider import SsoProvider

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async with factory() as session:
        async with session.begin():
            existing = await session.execute(select(SsoProvider).limit(1))
            if existing.scalar_one_or_none() is not None:
                return

            try:
                entries = json.loads(settings.modulo_oidc_providers)
            except (json.JSONDecodeError, TypeError):
                logger.warning("startup.sso_providers_invalid_json")
                return

            for entry in entries:
                if not all(k in entry for k in ("provider_id", "client_id", "client_secret", "discovery_url")):
                    logger.warning("startup.sso_provider_skipped", extra={"entry": str(entry)})
                    continue

                provider = SsoProvider(
                    provider_type="oidc",
                    name=entry.get("provider_id", entry.get("name", "Imported OIDC Provider")),
                    client_id=entry["client_id"],
                    client_secret=entry["client_secret"],
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
        logger.warning("startup.checkpointer_init_failed")


async def _run_retention_loop(interval_seconds: int = 3600) -> None:
    """Background loop: batch-delete terminal runs older than 90 days."""
    settings = get_settings()
    factory = get_or_create_session_factory(get_or_create_engine(settings))
    while True:
        try:
            async with factory() as session:
                async with session.begin():
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

    if settings.modulo_db.lower() == "sqlite":
        logger.warning("startup.sqlite_mode")

    _scheduler_tasks: list[asyncio.Task] = []
    if not settings.redis_url:
        _scheduler_tasks = await start_schedulers()
        logger.info(
            "startup.no_redis — cron and polling triggers use in-process schedulers. "
            "For multi-replica deployments, configure REDIS_URL."
        )
    else:
        logger.info("startup.redis_configured — Celery beat available for distributed scheduling")

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
    await _ensure_default_org(settings)

    # Seed MODULO_USERS env var entries into the user table (idempotent).
    await _seed_modulo_users(settings)

    # Seed demo data if MODULO_DEMO_MODE is enabled.
    await _seed_demo_data(settings)

    # Seed SSO providers from deprecated env vars into the DB table (idempotent).
    await _seed_sso_providers(settings)

    # Initialise the LangGraph checkpointer schema (langgraph.* tables).
    await _init_checkpointer(
        pg_connection_string(settings.database_url),
        settings.fernet_key,
        fernet_key_old=settings.fernet_key_old,
    )

    # Initialise the runtime-config store so it captures env-var state at boot.
    from modulo.core.runtime_config.store import get_runtime_config_store
    get_runtime_config_store()

    # Initialise the graceful shutdown manager with the configured timeout.
    _shutdown_manager.register("otel", shutdown_otel)
    _shutdown_manager.register("db_engine", db_engine.dispose)
    _shutdown_manager.register("rate_limiter_redis", shutdown_rate_limiters)
    _shutdown_manager.register("scheduler_engine", dispose_scheduler_engine)

    # Start the run retention background loop.
    retention_task = asyncio.create_task(_run_retention_loop())

    # Start the HITL claim expiry background job.
    _claim_expiry_job = ClaimExpiryJob(db_engine)
    await _claim_expiry_job.start()

    yield
    retention_task.cancel()
    await _claim_expiry_job.stop()
    for st in _scheduler_tasks:
        st.cancel()
    try:
        await retention_task
        for st in _scheduler_tasks:
            try:
                await st
            except asyncio.CancelledError:
                pass
    except asyncio.CancelledError:
        pass
    await _shutdown_manager.shutdown()


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
app.add_middleware(SecurityHeadersMiddleware)  # type: ignore[arg-type]
app.add_middleware(CatchAllMiddleware)
app.add_middleware(ShutdownMiddleware, manager=_shutdown_manager)  # type: ignore[arg-type]

app.include_router(health_router)
app.include_router(admin_router)
app.include_router(admin_feature_flags_router)
app.include_router(admin_license_router)
app.include_router(admin_rate_limits_router)
app.include_router(admin_runtime_config_router)
app.include_router(admin_sso_router)
app.include_router(admin_triggers_router)
app.include_router(auth_router)
app.include_router(changelog_router)
app.include_router(sso_router)
app.include_router(dashboard_router)
app.include_router(deployment_router)
app.include_router(costs_router)
app.include_router(teams_router)
app.include_router(pipelines_router)
app.include_router(agents_router)
app.include_router(hitl_router)
app.include_router(schemas_router)
app.include_router(model_backends_router)
app.include_router(node_categories_router)
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
app.include_router(mcp_oauth_router)
app.include_router(me_router)
app.include_router(registry_router)
app.include_router(determination_router)
app.include_router(evals_router)
app.include_router(admin_notifications_router)
app.include_router(admin_rotation_router)
app.include_router(admin_runtime_config_router)
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

# Remote MCP server — mounted as a Starlette sub-app at /mcp.
# Auth is enforced by McpAuthMiddleware inside the sub-app.
app.mount("/mcp", build_mcp_asgi_app())

app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
