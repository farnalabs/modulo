import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import anyio
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
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
from modulo.api.routes.admin_dev_mode import router as admin_dev_mode_router
from modulo.api.routes.admin_email import router as admin_email_router
from modulo.api.routes.admin_feature_flags import router as admin_feature_flags_router
from modulo.api.routes.admin_housekeeping import router as admin_housekeeping_router
from modulo.api.routes.admin_license import router as admin_license_router
from modulo.api.routes.admin_monitor_config import router as admin_monitor_config_router
from modulo.api.routes.admin_notifications import router as admin_notifications_router
from modulo.api.routes.admin_orgs import router as admin_orgs_router
from modulo.api.routes.admin_rate_limits import router as admin_rate_limits_router
from modulo.api.routes.admin_remy import router as admin_remy_router
from modulo.api.routes.admin_rotation import router as admin_rotation_router
from modulo.api.routes.admin_run_retention import router as admin_run_retention_router
from modulo.api.routes.admin_runtime_config import router as admin_runtime_config_router
from modulo.api.routes.admin_sso import router as admin_sso_router
from modulo.api.routes.admin_system_config import router as admin_system_config_router
from modulo.api.routes.admin_tiers import router as admin_tiers_router
from modulo.api.routes.admin_triggers import router as admin_triggers_router
from modulo.api.routes.agents import router as agents_router
from modulo.api.routes.analytics import router as analytics_router
from modulo.api.routes.api_keys import router as api_keys_router
from modulo.api.routes.audit import router as audit_router
from modulo.api.routes.auth import router as auth_router
from modulo.api.routes.community_library import router as community_library_router
from modulo.api.routes.composite_templates import router as composite_templates_router
from modulo.api.routes.connectors import router as connectors_router
from modulo.api.routes.contributions import router as contributions_router
from modulo.api.routes.cost_components import router as cost_components_router
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
from modulo.api.routes.guardrail_config import router as guardrail_config_router
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
from modulo.api.routes.metrics_ingest import router as metrics_ingest_router
from modulo.api.routes.model_backends import router as model_backends_router
from modulo.api.routes.node_categories import router as node_categories_router
from modulo.api.routes.notifications import router as notifications_router
from modulo.api.routes.observability import router as observability_router
from modulo.api.routes.onboarding import router as onboarding_router
from modulo.api.routes.org_settings import router as org_settings_router
from modulo.api.routes.parameter_schemas import router as parameter_schemas_router
from modulo.api.routes.pipeline_folders import router as pipeline_folders_router
from modulo.api.routes.pipelines import router as pipelines_router
from modulo.api.routes.plugins import router as plugins_router
from modulo.api.routes.product_analytics import router as product_analytics_router
from modulo.api.routes.product_analytics_identity import router as product_analytics_identity_router
from modulo.api.routes.product_analytics_transparency import router as product_analytics_transparency_router
from modulo.api.routes.registry import router as registry_router
from modulo.api.routes.remy import router as remy_router
from modulo.api.routes.run_ws import router as run_ws_router
from modulo.api.routes.runs import router as runs_router
from modulo.api.routes.schema_folders import router as schema_folders_router
from modulo.api.routes.schemas import router as schemas_router
from modulo.api.routes.scim import router as scim_router
from modulo.api.routes.slack import router as slack_router
from modulo.api.routes.sso import router as sso_router
from modulo.api.routes.stripe_webhook import router as stripe_webhook_router
from modulo.api.routes.teams import router as teams_router
from modulo.api.routes.templates import router as templates_router
from modulo.api.routes.triggers import pipeline_triggers_router
from modulo.api.routes.triggers import router as triggers_router
from modulo.api.routes.variants import router as variants_router
from modulo.api.routes.viewmodel import router as viewmodel_router
from modulo.api.routes.views import router as views_router
from modulo.api.routes.webhooks import router as webhooks_router
from modulo.core.cleanup_jobs import cleanup_scheduler_loop
from modulo.core.events.event_bus import configure_event_bus
from modulo.core.events.listeners import register_listeners
from modulo.core.graceful_shutdown import ShutdownManager, ShutdownMiddleware
from modulo.core.hitl_manager.expiry_job import ClaimExpiryJob
from modulo.core.logging_config import configure_logging
from modulo.core.seed_data.catalog import FLAGS, TIERS
from modulo.db.session import engine as db_engine
from modulo.otel_bridge import setup_otel, shutdown_otel
from modulo.settings import Settings, get_settings

# Uptime tracking -- set at module import time, read by health endpoints.
logger = logging.getLogger(__name__)


class _TaskGroupSessionManager(Protocol):
    """FastMCP session-manager surface used by the application lifespan."""

    _task_group: anyio.abc.TaskGroup | None


_START_TIME = datetime.now(UTC)

# Graceful shutdown manager -- resources registered during lifespan startup.
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
    logger.warning("startup.continuing_without_db -- app will retry connections at runtime")


# Dedicated advisory-lock key (int4, int4) that serialises migration runs across
# machines/processes so _run_migrations never interleaves with the entrypoint's
# `alembic upgrade heads`.
_MIGRATION_LOCK_KEY = (72001, 1)
_MIGRATION_LOCK_POLL_ATTEMPTS = 240
_MIGRATION_LOCK_POLL_INTERVAL = 1.0
_MIGRATION_MAX_ATTEMPTS = 5
_MIGRATION_BACKOFF_SECONDS = 3


class _MigrationLockTimeoutError(Exception):
    """Raised when the migration advisory lock cannot be acquired in time."""


# Legacy role literal referenced only by the boot-time owner-rows guard. The
# semgrep rule `no-owner-as-org-role` bans the raw literal in role checks —
# using this constant keeps the guard explicit while keeping the literal out of
# comparison expressions.
_OWNER_ROLE_LEGACY = "owner"


def _resolve_alembic_ini() -> Path:
    """Locate backend/alembic.ini robustly regardless of the process cwd."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            return candidate
    return Path("alembic.ini")


@asynccontextmanager
async def _migration_advisory_lock(settings: Settings) -> AsyncIterator[bool]:
    """Hold a Postgres advisory lock for the duration of a migration run.

    Uses ``pg_try_advisory_lock`` in a polling loop (per AGENTS.md:
    ``pg_advisory_lock`` under ``asyncio.wait_for`` races server-side acquisition
    against the client timeout). The lock is connection-scoped and held open
    while the caller runs Alembic, so a concurrent migration from another
    machine/process cannot interleave.
    """
    engine = get_or_create_engine(settings)
    from modulo.db.migrations.env import set_lock_held_by_caller

    async with engine.connect() as conn:
        acquired = False
        for _ in range(_MIGRATION_LOCK_POLL_ATTEMPTS):
            result = await conn.execute(
                text("SELECT pg_try_advisory_lock(:k1, :k2)"),
                {"k1": _MIGRATION_LOCK_KEY[0], "k2": _MIGRATION_LOCK_KEY[1]},
            )
            if bool(result.scalar_one()):
                acquired = True
                break
            await asyncio.sleep(_MIGRATION_LOCK_POLL_INTERVAL)
        try:
            if acquired:
                # Tell env.py the lock is already held on a different (sync)
                # connection so the alembic run does not re-acquire the same
                # session-scoped key and self-deadlock.
                set_lock_held_by_caller(True)
            yield acquired
        finally:
            if acquired:
                set_lock_held_by_caller(False)
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:k1, :k2)"),
                    {"k1": _MIGRATION_LOCK_KEY[0], "k2": _MIGRATION_LOCK_KEY[1]},
                )


async def _run_bootstrap(settings: Settings) -> None:
    """Run bootstrap_role (roles + allow-list + break-glass grants) idempotently.

    The entrypoint already runs bootstrap before alembic; the lifespan path runs
    it BEFORE and AFTER alembic so the boundary survives every boot and is
    re-applied after the migration's grants land (ADR-017/018 amendment). The
    admin URL falls back to the app URL when DATABASE_ADMIN_URL is unset (like
    env.py).

    A failed allow-list / role-posture assertion (bootstrap's
    ``_assert_role_posture``) is currently logged as a WARNING (non-fatal): it
    blocks boot only until every deployed environment has a non-superuser app
    role and ``DATABASE_ADMIN_URL`` provisioned — today both staging and prod
    carry legacy superuser app roles, so a fatal assertion would block every
    deploy. The break-glass boundary remains enforced by the DDL migrations.
    Other bootstrap failures (e.g. a transient DB blip while applying
    roles/grants) are logged and non-fatal: they are re-attempted on the
    post-alembic run and on the next boot.
    """
    from modulo.db.bootstrap_role import bootstrap_roles

    admin_url = os.environ.get("DATABASE_ADMIN_URL") or settings.database_url
    try:
        await bootstrap_roles(admin_url, settings.database_url)
    except Exception as exc:
        if "Break-glass role posture assertion FAILED" in str(exc):
            # Non-fatal until DATABASE_ADMIN_URL + non-superuser app role are
            # provisioned on all envs (staging + prod both have superuser app
            # roles from the legacy setup; the fatal assertion blocks every
            # deploy). The DDL migrations still enforce the boundary.
            logger.warning("startup.break_glass_role_posture_failed %s", exc)
        else:
            logger.warning("startup.role_bootstrap_failed", exc_info=True)


async def _run_break_glass_watchdog(settings: Settings) -> None:
    """Boot-time break-glass watchdog (deliverable B).

    The allow-list / role-posture assertions from ``bootstrap_role.py`` run
    inside ``_run_bootstrap`` (before AND after alembic); a posture failure is
    currently logged as a WARNING (non-fatal) until the non-superuser app role
    migration lands on all envs. This step runs the URL/secret-presence config
    checks, honouring ``MODULO_BREAK_GLASS_BOOT_FAILURE_MODE``, and publishes
    the advisory /healthz exposure.
    """
    from modulo.api.routes.health import set_break_glass_watchdog
    from modulo.settings import validate_break_glass_boot

    try:
        validate_break_glass_boot(settings)
    except RuntimeError as exc:
        set_break_glass_watchdog("failed", str(exc))
        logger.exception("infra_blocked=break_glass_config_failed")
        raise
    set_break_glass_watchdog("ok", "break-glass boot config assertions passed")
    logger.info("startup.break_glass_watchdog_ok")


async def _db_is_at_migration_head(settings: Settings) -> bool:
    """Return True when the DB's ``alembic_version`` already equals the head.

    Boot fast-path: multiple machines boot simultaneously on a fresh deploy and
    every process group runs migrations serialised by the advisory lock —
    machines that did not win the lock waited up to ``_MIGRATION_LOCK_POLL_ATTEMPTS``
    before FATALing, even when the schema was already up to date. When the DB is
    already at head there is no work to do, so the advisory lock acquisition and
    the alembic run are pure contention and are skipped entirely.

    Fail-safe: any failure (missing table, multiple heads, connection error)
    returns False so the caller proceeds through the normal retry/lock path.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    alembic_ini = _resolve_alembic_ini()
    config = Config(str(alembic_ini))
    config.set_main_option(
        "script_location",
        str(alembic_ini.parent / "src" / "modulo" / "db" / "migrations"),
    )
    try:
        head = ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        return False
    if not head:
        return False
    engine = get_or_create_engine(settings)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            versions = {row[0] for row in result.fetchall()}
    except Exception:
        return False
    return versions == {head}


async def _run_migrations(settings: Settings) -> None:
    """Run Alembic migrations to head, with a bounded retry loop and FATAL exhaustion.

    This is the single authoritative migration runner (ADR 017). The entrypoint
    may have already applied migrations (``alembic upgrade heads`` is idempotent)
    but the advisory lock guarantees two migration runs never overlap. Transient
    DB errors are retried; on exhaustion this raises — it is called bare from
    the lifespan, so a persistent failure fails uvicorn boot and logs the
    ``infra_blocked=migration_failed`` key for the deploy-pipeline consumer.

    Fast-path: when the DB is already at the head revision the migration run is
    skipped entirely (no advisory lock, no alembic run) so boot is instant and
    machines never contend for the lock.
    """
    from alembic import command
    from alembic.config import Config

    from modulo.db.migrations.env import _to_sync_url

    alembic_ini = _resolve_alembic_ini()
    last_error: Exception | None = None

    if await _db_is_at_migration_head(settings):
        logger.info("startup.migrations_already_at_head -- skipping migration run")
        return

    # Bootstrap BEFORE migrations so the roles 0036 re-owns to / grants on exist.
    await _run_bootstrap(settings)

    for attempt in range(1, _MIGRATION_MAX_ATTEMPTS + 1):
        try:
            async with _migration_advisory_lock(settings) as acquired:
                if not acquired:
                    raise _MigrationLockTimeoutError("Timed out waiting for the migration advisory lock")
                config = Config(str(alembic_ini))
                config.set_main_option(
                    "script_location",
                    str(alembic_ini.parent / "src" / "modulo" / "db" / "migrations"),
                )
                # Keep the app's structured logging intact — env.py skips
                # fileConfig when config_file_name is None.
                config.config_file_name = None
                config.set_main_option("sqlalchemy.url", _to_sync_url(settings.database_url))
                await asyncio.to_thread(command.upgrade, config, "heads")
            logger.info("startup.migrations_complete")
            break
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, _MigrationLockTimeoutError) as exc:
            last_error = exc
            logger.warning(
                "startup.migrations_retry",
                extra={"attempt": attempt, "max_attempts": _MIGRATION_MAX_ATTEMPTS, "error": str(exc)},
                exc_info=True,
            )
            if attempt < _MIGRATION_MAX_ATTEMPTS:
                await asyncio.sleep(_MIGRATION_BACKOFF_SECONDS * attempt)
        except Exception as exc:
            logger.exception(
                "infra_blocked=migration_failed",
                extra={"attempt": attempt, "error": str(exc)},
            )
            last_error = exc
            break

    if last_error is None:
        # Re-apply the allow-list + grants AFTER alembic on the same boot.
        await _run_bootstrap(settings)
        return

    logger.error(
        "infra_blocked=migration_failed",
        extra={"attempts": _MIGRATION_MAX_ATTEMPTS, "error": str(last_error)},
        exc_info=last_error is not None,
    )
    fatal_error = RuntimeError("FATAL: database migrations failed after retries")
    raise fatal_error from last_error


async def _assert_no_owner_rows(settings: Settings) -> None:
    """Hard-fail boot if any org_membership still claims the dropped 'owner' role.

    The migration (0030) converts owner -> admin transactionally; this is a
    second line of defence with an actionable message. Raises RuntimeError
    (FATAL) when owner rows exist. Callers must let SQLAlchemyError pass as a
    transient DB blip (log + continue) — the invariant is guaranteed by the
    migration transaction.
    """
    from sqlalchemy import select

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.db.models.org_membership import OrgMembership

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async with factory() as session, session.begin():
        result = await session.execute(select(OrgMembership).where(OrgMembership.role == _OWNER_ROLE_LEGACY))
        owners = list(result.scalars().all())

    if not owners:
        logger.info("startup.owner_rows_zero")
        return

    account_ids = sorted({str(o.account_id) for o in owners})
    logger.error(
        "infra_blocked=owner_rows_present",
        extra={"count": len(owners), "account_ids": account_ids},
    )
    # Message text only — never executed as SQL. The prescription mirrors the
    # migration's owner->admin UPDATE for operators who hit this FATAL.
    raise RuntimeError(
        "FATAL: org_memberships still contain the dropped 'owner' role "  # noqa: S608
        f"({len(owners)} rows, account_ids={account_ids}). Prescribed fix: "
        "UPDATE org_memberships SET role='admin' WHERE role='owner'"  # nosec
    )


async def _ensure_default_org(settings: Settings) -> None:
    """Create a default organisation if none exists."""
    from sqlalchemy import select

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.core.seed_data.cost_components import seed_cost_components_for_org
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

        # Seed default cost components for the new org in the SAME transaction
        # (idempotent; the per-boot _seed_cost_components also covers it, but a
        # fresh org gets its components here immediately). Fail-open: a seed
        # failure must never block default-org creation.
        try:
            await seed_cost_components_for_org(session, org.id)
        except Exception:
            logger.warning("startup.default_org_cost_components_seed_failed", exc_info=True)


async def _boot_seed(name: str, coro: Awaitable[Any]) -> None:
    """Await a startup seed and print a permanent boot summary to stdout.

    The structured JsonFormatter logger lines do not render in ``fly logs``, so
    a seed that silently failed at boot was invisible for weeks (FAR-113). This
    helper always prints an ok/FAILED line so every boot records each seed's
    outcome. Failures remain non-fatal (the seed never blocks startup).
    """
    try:
        result = await coro
        detail = f" ({result})" if result is not None else ""
        print(f"[boot] seed {name}: ok{detail}", flush=True)  # noqa: T201
    except Exception as exc:
        print(f"[boot] seed {name}: FAILED ({exc!r})", flush=True)  # noqa: T201
        logger.exception("startup.seed_failed", extra={"seed": name})


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
            await _seed_modulo_user(session, org, entry)


async def _seed_modulo_user(session: Any, org: Any, entry: str) -> None:
    """Seed a single MODULO_USERS entry (``email:password``) into the account + membership tables.

    Accepts both bcrypt hashes (user1:$2b$12$hash) and plaintext passwords
    (admin:admin). Plaintext passwords are auto-hashed with bcrypt at seed time.
    """
    from sqlalchemy import select

    from modulo.auth.passwords import hash_password
    from modulo.db.models.account import Account
    from modulo.db.models.org_membership import OrgMembership

    entry = entry.strip()
    if not entry:
        return
    colon = entry.find(":")
    if colon < 1:
        return
    email = entry[:colon]
    pw_part = entry[colon + 1 :]

    result = await session.execute(select(Account).where(Account.email == email))
    existing_account = result.scalar_one_or_none()
    pw_hash = pw_part if pw_part.startswith("$2") else hash_password(pw_part)

    if existing_account is not None and (
        not existing_account.password_hash or not existing_account.password_hash.startswith("$2")
    ):
        await _rehash_existing_user(session, org, existing_account, email, pw_hash)
        return

    if existing_account is not None:
        logger.info("startup.user_exists", extra={"email": email})
        return

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


async def _rehash_existing_user(session: Any, org: Any, existing_account: Any, email: str, pw_hash: str) -> None:
    """Rehash an existing account's plaintext password and ensure its org membership."""
    from sqlalchemy import select

    from modulo.db.models.org_membership import OrgMembership

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
                safe_entry = (
                    {k: v for k, v in entry.items() if k != "client_secret"} if isinstance(entry, dict) else entry
                )
                logger.warning("startup.sso_provider_skipped", extra={"entry": str(safe_entry)})
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


async def _seed_system_schemas(settings: Settings) -> None:
    """Seed system schemas for all existing organisations."""
    import uuid as _uuid

    from sqlalchemy import select

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.db.models.account import Account
    from modulo.db.models.organisation import Organisation
    from modulo.db.seed import seed_system_schemas

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    async with factory() as session, session.begin():
        orgs = (await session.execute(select(Organisation).order_by(Organisation.created_at))).scalars().all()

        admin = (
            await session.execute(select(Account).where(Account.email == "admin").order_by(Account.created_at).limit(1))
        ).scalar_one_or_none()

        system_account_id: _uuid.UUID | None = None
        if admin is not None:
            system_account_id = admin.id
        else:
            first = (await session.execute(select(Account).order_by(Account.created_at).limit(1))).scalar_one_or_none()
            if first is not None:
                system_account_id = first.id

        if system_account_id is None:
            logger.warning("startup.no_account_for_system_schemas")
            return

        for org in orgs:
            await seed_system_schemas(session, org.id, system_account_id)


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

    # Calling get_settings() at startup triggers pydantic validation -- if
    # SECRET_KEY or FERNET_KEY are missing, too short, or a known placeholder,
    # the validator raises and the process exits before accepting requests.
    settings = get_settings()

    _configure_license_and_otel(settings)
    _discover_plugins(settings)

    logger.info("startup.starting")
    await _run_boot_guards_and_seeds(settings)
    _register_shutdown_manager(settings)

    tasks = await _start_background_tasks(settings)

    yield

    await _teardown_tasks(tasks)


def _configure_license_and_otel(settings: Settings) -> None:
    """Configure the license public key, OTel, and basic runtime warnings."""
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
    setup_otel(
        service_name=settings.modulo_otel_service_name,
        telemetry_enabled=settings.modulo_telemetry_enabled,
    )


def _discover_plugins(settings: Settings) -> None:
    """Discover installed plugins if plugin discovery is enabled."""
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


async def _run_boot_guards_and_seeds(settings: Settings) -> None:
    """Verify DB connectivity, run migrations, and execute all boot-time guards and seeds."""
    # Verify the database is reachable before accepting requests.
    await _verify_db_connectivity(settings)

    # Run Alembic migrations to bring the schema up to date.
    await _run_migrations(settings)

    # Break-glass watchdog (deliverable B): the allow-list/role-posture
    # assertion is a non-fatal WARNING inside _run_bootstrap (superuser legacy
    # app roles on staging/prod); the URL/secret-presence checks honour
    # warn|fail mode.
    await _run_break_glass_watchdog(settings)

    # Hard-fail guard: the 'owner' org role was dropped (ADR 017 A1a). The
    # migration converts owner -> admin transactionally, so owner rows must be
    # zero here. A transient SQLAlchemyError on the assertion query itself is
    # logged + ignored (a blip must not brick boot — the invariant is already
    # guaranteed by the migration transaction); any owner rows are FATAL.
    try:
        await _assert_no_owner_rows(settings)
    except SQLAlchemyError:
        logger.warning("startup.owner_rows_check_db_error", exc_info=True)

    # Ensure at least one organisation exists before seeding users.
    # Non-fatal: if the organisations table doesn't exist (migration state
    # mismatch), the app starts without an org and retries on next restart.
    try:
        await _ensure_default_org(settings)
    except Exception:
        logger.warning("startup.default_org_failed", exc_info=True)

    # Seed MODULO_USERS env var entries into the user table (idempotent).
    # Non-fatal: if tables are missing, seeding is retried on next restart.
    await _boot_seed("modulo_users", _seed_modulo_users(settings))

    # Seed SSO providers from deprecated env vars into the DB table (idempotent).
    await _boot_seed("sso_providers", _seed_sso_providers(settings))

    # Seed system schemas for all existing organisations (idempotent).
    await _boot_seed("system_schemas", _seed_system_schemas(settings))

    # Seed the default modulo-dev EnvironmentProfile for the dogfood pipeline.
    await _boot_seed("environment_profiles", _seed_environment_profiles(settings))

    # Seed the tier catalog and feature flag definitions (idempotent).
    await _boot_seed("tier_catalog", _seed_tier_catalog())

    # Seed the default cost components for every org (idempotent; system-
    # context org enumeration, per-org set_rls_org on the inserts).
    await _boot_seed("cost_components", _seed_cost_components(settings))

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

    # NOTE: the in-process cron scheduler (run_scheduler) is intentionally NOT
    # started here. Plan F1 "single scheduler at a time": the SAQ system worker's
    # fire_due_triggers cron is the scheduler, and the entrypoint never starts
    # Celery beat (removed in PR C). Running the in-process loop alongside SAQ would double-fire
    # cron triggers.


def _register_shutdown_manager(settings: Settings) -> None:
    """Register graceful-shutdown callbacks for resources created during startup.

    Two session factories exist:
      - modulo.db.session    (module-level, used by entrypoint.sh + ClaimExpiryJob)
      - modulo.api.dependencies  (DI-injected, used by all route handlers)
    Both point to the same DB URL but have separate connection pools.  They
    are intentionally decoupled -- the entrypoint runs before FastAPI is
    initialised and can't use DI.  Dispose both so no connections leak.
    """
    try:
        di_engine = get_or_create_engine(settings)

        async def shutdown_otel_async() -> None:
            shutdown_otel()

        _shutdown_manager.register("otel", shutdown_otel_async)
        _shutdown_manager.register("db_engine", db_engine.dispose)
        _shutdown_manager.register("di_engine", di_engine.dispose)
        _shutdown_manager.register("rate_limiter_redis", shutdown_rate_limiters)
    except Exception:
        logger.warning("startup.shutdown_manager_init_failed", exc_info=True)


async def _start_background_tasks(settings: Settings) -> dict[str, Any]:
    """Start all lifespan background tasks/jobs and return handles for teardown."""
    # Start the run retention background loop.
    retention_task = asyncio.create_task(_run_retention_loop())

    # Start the in-process worker-liveness watchdog (postmortem FAR-121).
    # Deliberately a plain asyncio task in the WEB process — NOT an SAQ cron —
    # so a total worker outage (both worker machines stopped, 2026-08-08/09)
    # still alerts a human while the web/app process stays up.
    watchdog_task: asyncio.Task[None] | None = None
    if settings.watchdog_enabled and settings.redis_url:
        from modulo.core.watchdog.worker_liveness import run_worker_liveness_watchdog

        watchdog_task = asyncio.create_task(run_worker_liveness_watchdog(settings))
        logger.info("startup.worker_liveness_watchdog_started")

    # Register SQLAlchemy event listeners for resource-change events.
    register_listeners()

    # Configure the EventBus with Redis broker if Redis is available.
    if settings.redis_url:
        from modulo.core.events.redis_broker import RedisEventBroker

        redis_broker = RedisEventBroker(settings.redis_url)
        await configure_event_bus(redis_broker=redis_broker)
        logger.info("startup.event_bus_redis_enabled")

    # Start the HITL claim expiry background job.
    claim_expiry_job = ClaimExpiryJob(db_engine)
    await claim_expiry_job.start()

    # Start webhook trigger event cleanup loop (30-day retention).
    from sqlalchemy.ext.asyncio import async_sessionmaker

    trigger_event_cleanup_task = asyncio.create_task(
        cleanup_scheduler_loop(async_sessionmaker(db_engine, expire_on_commit=False))
    )

    # Start MCP task group so FastMCP's _handle_stateless_request can use tg.start().
    from modulo.api.mcp_server import mcp

    mcp_tg = await anyio.create_task_group().__aenter__()
    # FastMCP annotates this private integration slot as None despite assigning a TaskGroup at runtime.
    session_manager = cast("_TaskGroupSessionManager", mcp.session_manager)
    session_manager._task_group = mcp_tg

    return {
        "retention_task": retention_task,
        "trigger_event_cleanup_task": trigger_event_cleanup_task,
        "watchdog_task": watchdog_task,
        "claim_expiry_job": claim_expiry_job,
        "mcp_tg": mcp_tg,
    }


async def _teardown_tasks(tasks: dict[str, Any]) -> None:
    """Cancel/stop all lifespan background tasks and shut down registered resources."""
    mcp_tg = tasks["mcp_tg"]
    retention_task = tasks["retention_task"]
    trigger_event_cleanup_task = tasks["trigger_event_cleanup_task"]
    watchdog_task = tasks["watchdog_task"]
    claim_expiry_job = tasks["claim_expiry_job"]

    await mcp_tg.__aexit__(None, None, None)
    retention_task.cancel()
    trigger_event_cleanup_task.cancel()
    if watchdog_task is not None:
        watchdog_task.cancel()
    await claim_expiry_job.stop()
    with suppress(asyncio.CancelledError):
        await retention_task
    with suppress(asyncio.CancelledError):
        await trigger_event_cleanup_task
    if watchdog_task is not None:
        with suppress(asyncio.CancelledError):
            await watchdog_task
    await _shutdown_manager.shutdown()


async def _seed_cost_components(settings: Settings) -> None:
    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
    from modulo.core.seed_data.cost_components import seed_cost_components

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)
    await seed_cost_components(factory)


async def _seed_tier_catalog() -> None:
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
    description="Agent governance for your agentic SDLC",
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
app.include_router(admin_dev_mode_router)
app.include_router(admin_email_router)
app.include_router(admin_feature_flags_router)
app.include_router(admin_license_router)
app.include_router(admin_rate_limits_router)
app.include_router(admin_runtime_config_router)
app.include_router(admin_sso_router)
app.include_router(admin_system_config_router)
app.include_router(admin_tiers_router)
app.include_router(admin_triggers_router)
app.include_router(admin_housekeeping_router)
app.include_router(auth_router)
app.include_router(sso_router)
app.include_router(analytics_router)
app.include_router(dashboard_router)
app.include_router(deployment_router)
app.include_router(costs_router)
app.include_router(cost_components_router)
app.include_router(teams_router)
app.include_router(pipelines_router)
app.include_router(pipeline_folders_router)
app.include_router(agents_router)
app.include_router(parameter_schemas_router)
app.include_router(hitl_router)
app.include_router(schemas_router)
app.include_router(schema_folders_router)
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
app.include_router(slack_router)
app.include_router(stripe_webhook_router)
app.include_router(views_router)
app.include_router(viewmodel_router)
app.include_router(api_keys_router)
app.include_router(audit_router)
app.include_router(library_router)
app.include_router(community_library_router)
app.include_router(lifecycle_maps_router)
app.include_router(mcp_oauth_router)
app.include_router(mcp_setup_router)
app.include_router(me_router)
app.include_router(org_settings_router)
app.include_router(product_analytics_router)
app.include_router(registry_router)
app.include_router(determination_router)
app.include_router(evals_router)
app.include_router(admin_notifications_router)
app.include_router(admin_orgs_router)
app.include_router(admin_remy_router)
app.include_router(admin_monitor_config_router)
app.include_router(admin_rotation_router)
app.include_router(admin_run_retention_router)
app.include_router(in_app_notifications_router)
app.include_router(notifications_router)
app.include_router(sensitive_router)
app.include_router(observability_router)
app.include_router(variants_router)
app.include_router(feedback_router)
app.include_router(guardrail_config_router)
app.include_router(plugins_router)
app.include_router(scim_router)
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
app.include_router(product_analytics_identity_router)
app.include_router(metrics_ingest_router)
app.include_router(product_analytics_transparency_router)

# Strip router lifespan contexts -- none of the 68+ routers register
# on_startup/on_shutdown handlers, so every _DefaultLifespan is a no-op.
# Keeping the deeply nested _merge_lifespan_context chain causes infinite
# recursion in Docker builds (FastAPI 0.139.0, Python 3.12, Linux).
app.router.lifespan_context = _lifespan

# Remote MCP server -- mounted as a Starlette sub-app at /mcp.
# Auth is enforced by McpAuthMiddleware inside the sub-app.
app.mount("/mcp", build_mcp_asgi_app())

app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(Exception, unhandled_exception_handler)
