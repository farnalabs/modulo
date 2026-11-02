"""Collect the support-bundle v1 member CONTENT (FAR-2305).

Two install shapes share one collector:

* **native** — the launcher data dir supplies the doctor output, the bundled
  child log tails and the persisted degraded record.
* **app container** — there is no data dir. Settings come from the process
  environment, deployment facts from the environment, and the DB-backed
  members from the app's own session. When the DB is unreachable the other
  members are still emitted and each omission is recorded honestly in
  ``MANIFEST.txt`` (``missing: [...]``) — an unreachable DB never fails the
  whole bundle.

Collection is the ONLY place this package touches the launcher, the DB or the
API, and every such import is lazy: ``python -m modulo.support.bundle`` must
work with no launcher and no data dir. Redaction and the fail-closed gate live
in :mod:`modulo.support.redaction`; format assembly lives in
:mod:`modulo.support.bundle`.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from modulo.support.redaction import collect_seed_values

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from modulo.core.license import LicenseData
    from modulo.db.models.organisation import Organisation
    from modulo.settings import Settings

#: Per-component log tail budget (matches the pre-v1 doctor report).
DEFAULT_MAX_LOG_BYTES: Final[int] = 256 * 1024
#: Hard cap on any free-text member read into memory.
MAX_DOCTOR_BYTES: Final[int] = 1024 * 1024
MAX_DEGRADED_BYTES: Final[int] = 256 * 1024
MAX_ERROR_GROUPS: Final[int] = 50
MAX_FAILED_RUNS: Final[int] = 50
MAX_ERROR_MESSAGE_CHARS: Final[int] = 500
HEALTH_TIMEOUT_SECONDS: Final[float] = 20.0

LOG_PREFIX: Final[str] = "logs/"
LOG_SUFFIX: Final[str] = ".log.tail"

#: Statuses treated as a failure for ``runs.json`` (each row still reports its
#: own status, so a broadened set is never presented as "failed").
FAILED_RUN_STATUSES: Final[tuple[str, ...]] = (
    "failed",
    "stalled",
    "eval_failed",
    "budget_exceeded",
    "cost_ceiling_exceeded",
    "compensation_failed",
    "router_no_match",
    "rejected",
)

#: Fixed table list for ``db.txt`` row counts (counts only — never rows).
ROW_COUNT_TABLES: Final[tuple[str, ...]] = (
    "runs",
    "pipelines",
    "pipeline_snapshots",
    "agents",
    "schemas",
    "connector_instances",
    "model_backends",
    "error_groups",
    "error_events",
    "audit_events",
    "accounts",
    "organisations",
)

#: Direct credential env vars seeded as exact-match scrub patterns.
_DIRECT_SECRET_ENV: Final[tuple[str, ...]] = (
    "FERNET_KEY",
    "SECRET_KEY",
    "MODULO_LICENSE_KEY",
    "MODULO_LICENSE_PRIVATE_KEY",
    "MODULO_USERS",
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "DB_PASSWORD",
)
#: URL-bearing env vars whose inline password is also seed-scrubbed.
_URL_SECRET_ENV: Final[tuple[str, ...]] = (
    "DATABASE_URL",
    "MODULO_SYSTEM_DATABASE_URL",
    "REDIS_URL",
    "MODULO_REDIS_URL",
)

#: Settings whose VALUES are safe to print verbatim (the reviewed safe-list).
_SAFE_SETTINGS: Final[frozenset[str]] = frozenset(
    {
        "modulo_db",
        "modulo_public_url",
        "modulo_frontend_url",
        "modulo_max_local_concurrency",
        "saq_worker_concurrency",
        "saq_worker_db_pool_size",
        "environment",
    }
)

#: Port env vars reported by name+value (never any other env value).
_PORT_ENV: Final[tuple[tuple[str, str], ...]] = (
    ("API_PORT", "PORT"),
    ("MODULO_API_PORT", "MODULO_API_PORT"),
    ("POSTGRES_PORT", "POSTGRES_PORT"),
    ("REDIS_PORT", "REDIS_PORT"),
)
_IMAGE_ENV: Final[tuple[str, ...]] = ("MODULO_IMAGE", "IMAGE_TAG", "IMAGE_DIGEST", "FLY_IMAGE_REF", "GIT_SHA")
_RESTART_ENV: Final[tuple[str, ...]] = ("MODULO_RESTART_COUNT", "RESTART_COUNT")
_CA_BUNDLE_ENV: Final[tuple[str, ...]] = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "MODULO_CA_BUNDLE")


@dataclass
class BundleSources:
    """Everything the format layer needs: raw member text + known omissions.

    ``degraded`` records a redaction LAYER that could not run (for example an
    unreadable 0600 secrets file disabling the seed-exact layer). A degraded
    best-effort layer is allowed to fail open, but the reason is rendered into
    ``MANIFEST.txt`` / ``report.json`` so the omission is visible in the
    artefact — never silent.
    """

    install_shape: str
    members: dict[str, str] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)
    truncated: dict[str, str] = field(default_factory=dict)
    secret_values: dict[str, str] = field(default_factory=dict)
    degraded: dict[str, str] = field(default_factory=dict)
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES


@dataclass
class AsyncCollected:
    """DB/health members collected in one event loop (each independently optional)."""

    health: dict[str, Any] | None = None
    database: dict[str, Any] | None = None
    error_groups: list[dict[str, Any]] | None = None
    failed_runs: list[dict[str, Any]] | None = None
    licence: dict[str, Any] | None = None
    clock_drift_seconds: float | None = None
    missing: dict[str, str] = field(default_factory=dict)


_ASYNC_MEMBERS: Final[tuple[tuple[str, str], ...]] = (
    ("health.json", "health check"),
    ("db.txt", "database inventory"),
    ("errors.json", "error groups"),
    ("runs.json", "failed runs"),
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def collect_sources(
    *,
    install_shape: str,
    data_dir: Path | None = None,
    doctor_output: str | None = None,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    collect_async: bool = True,
    extra_secret_values: Mapping[str, str] | None = None,
) -> BundleSources:
    """Collect every v1 member this install shape can produce.

    Never raises for an unavailable optional member: the member is omitted and
    the reason lands in ``sources.missing``. It also never includes the raw
    ``secrets.json`` file.
    """
    sources = BundleSources(install_shape=install_shape, max_log_bytes=max_log_bytes)
    settings = _load_settings(sources)
    sources.secret_values = _seed_values(data_dir, settings, extra_secret_values, sources.degraded)

    if install_shape == "native" and data_dir is not None:
        _collect_native(sources, data_dir, doctor_output)
    else:
        if doctor_output is None:
            sources.missing["doctor.txt"] = "doctor output is only available from the native launcher"
        else:
            sources.members["doctor.txt"] = _cap_text(doctor_output, MAX_DOCTOR_BYTES, "doctor.txt", sources.truncated)
        sources.missing["degraded.json"] = "degraded.json is a native-install member"

    collected = _run_async_collection() if collect_async else _all_async_missing("async collection disabled")
    if collected.licence is None:
        collected.licence = _sync_licence_payload_safe()
    sources.missing.update(collected.missing)

    sources.members["version.txt"] = _version_text(collected.clock_drift_seconds)
    sources.members["deployment.txt"] = _deployment_text(install_shape)
    if settings is not None:
        sources.members["settings.txt"] = _settings_text(settings, collected.licence)
    if collected.health is not None:
        sources.members["health.json"] = _json_text(collected.health)
    if collected.database is not None:
        sources.members["db.txt"] = _db_text(collected.database)
    if collected.error_groups is not None:
        sources.members["errors.json"] = _json_text({"error_groups": collected.error_groups})
    if collected.failed_runs is not None:
        sources.members["runs.json"] = _json_text({"failed_runs": collected.failed_runs})
    if collected.licence is not None:
        sources.members["licence.txt"] = _licence_text(collected.licence)
    else:
        sources.missing["licence.txt"] = "licence state unavailable (settings could not be loaded)"
    return sources


# ---------------------------------------------------------------------------
# Native install members
# ---------------------------------------------------------------------------


def _collect_native(sources: BundleSources, data_dir: Path, doctor_output: str | None) -> None:
    from modulo.launcher.supervisor import (
        DEGRADED_FILENAME,
        log_paths,
        read_degraded_record,
        read_log_tail,
    )

    if doctor_output is None:
        sources.missing["doctor.txt"] = "no doctor output was supplied"
    else:
        sources.members["doctor.txt"] = _cap_text(doctor_output, MAX_DOCTOR_BYTES, "doctor.txt", sources.truncated)

    for component, path in sorted(log_paths(data_dir).items()):
        raw = read_log_tail(path, max_bytes=sources.max_log_bytes)
        sources.members[f"{LOG_PREFIX}{component}{LOG_SUFFIX}"] = collapse_repeated_lines(raw)

    degraded = read_degraded_record(data_dir / DEGRADED_FILENAME)
    if degraded is None:
        sources.missing["degraded.json"] = "not degraded (no persisted degraded.json record)"
        return
    rendered = _degraded_text(degraded)
    sources.members["degraded.json"] = _cap_text(rendered, MAX_DEGRADED_BYTES, "degraded.json", sources.truncated)


def redaction_map_from_data_dir(data_dir: Path) -> dict[str, str]:
    """Secret VALUE -> ``<redacted>`` map seeded from ``secrets.json``.

    The keys are only ever used as scrub patterns; they are never written to
    the bundle. An unreadable/missing secrets file returns an empty map (the
    structural + pattern layers still apply); :func:`_read_secrets_seed`
    additionally reports WHY the layer was skipped so the bundle can record it.
    """
    seed, _reason = _read_secrets_seed(data_dir)
    return seed


def _read_secrets_seed(data_dir: Path) -> tuple[dict[str, str], str | None]:
    """Return ``(seed map, degraded reason)`` for the data dir's secrets file.

    A present-but-unreadable file is a DEGRADED seed-exact layer, not a silent
    empty map: the caller records the reason in the manifest/report. Only the
    secrets file's own parse/read failures are swallowed — an unexpected error
    is a programming defect and must surface.
    """
    secrets_path = data_dir / "secrets.json"
    if not secrets_path.is_file():
        return {}, None
    from modulo.launcher.secrets_file import SecretsFileError, _parse

    try:
        secrets = _parse(secrets_path.read_bytes())
    except (SecretsFileError, OSError) as exc:
        return {}, f"seed-exact layer skipped: secrets.json unreadable ({exc})"
    return (
        collect_seed_values(
            secrets.postgres_password,
            secrets.redis_password,
            secrets.state_hmac_key_hex,
        ),
        None,
    )


def _seed_values(
    data_dir: Path | None,
    settings: Settings | None,
    extra: Mapping[str, str] | None,
    degraded: dict[str, str] | None = None,
) -> dict[str, str]:
    environ = os.environ
    direct: list[str | None] = [environ.get(name) for name in _DIRECT_SECRET_ENV]
    if settings is not None:
        direct.extend(
            [
                getattr(settings, "fernet_key", None),
                getattr(settings, "secret_key", None),
                getattr(settings, "modulo_license_key", None),
                getattr(settings, "modulo_license_private_key", None),
            ]
        )
    url_passwords = [_url_password(environ.get(name)) for name in _URL_SECRET_ENV]
    if settings is not None:
        url_passwords.extend(
            [
                _url_password(str(getattr(settings, "database_url", "") or "")),
                _url_password(str(getattr(settings, "redis_url", "") or "")),
            ]
        )
    seed = collect_seed_values(*direct, *url_passwords)
    if extra:
        seed.update(extra)
    if data_dir is not None:
        secrets_seed, reason = _read_secrets_seed(data_dir)
        seed.update(secrets_seed)
        if reason is not None and degraded is not None:
            degraded["seed_exact"] = reason
    return seed


def _url_password(url: str | None) -> str | None:
    """Extract a URL's inline password (never the whole URL)."""
    if not url:
        return None
    from urllib.parse import unquote, urlsplit

    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    return unquote(parsed.password) if parsed.password else None


# ---------------------------------------------------------------------------
# Settings / deployment / version / degraded rendering
# ---------------------------------------------------------------------------


def _load_settings(sources: BundleSources) -> Settings | None:
    try:
        from modulo.settings import get_settings

        return get_settings()
    except Exception as exc:  # a settings failure must not lose the bundle
        sources.missing["settings.txt"] = f"settings unavailable ({type(exc).__name__})"
        return None


def _settings_text(settings: Settings, licence: dict[str, Any] | None) -> str:
    from pydantic.fields import FieldInfo

    lines: list[str] = [
        "Settings inventory — full key list; values ONLY for the reviewed safe-list.",
        "Unsafe keys are reported as 'set' or 'unset' (never their value).",
        "",
    ]
    for name, info in type(settings).model_fields.items():
        if not isinstance(info, FieldInfo):  # pragma: no cover - pydantic contract
            continue
        label = info.alias or name.upper()
        if name in _SAFE_SETTINGS:
            lines.append(f"{label}={getattr(settings, name)}")
        else:
            state = "set" if name in settings.model_fields_set else "unset"
            lines.append(f"{label}: {state}")

    lines.append("")
    lines.append("# reviewed safe-list / derived")
    for label, env_name in _PORT_ENV:
        lines.append(f"{label}={os.environ.get(env_name, 'unset')}")
    payload = licence if licence is not None else _sync_licence_payload(settings)
    lines.append(f"PLAN_TIER={payload.get('tier', 'unknown')}")
    enabled = payload.get("enabled_flags")
    lines.append(f"FEATURE_FLAGS={','.join(enabled) if isinstance(enabled, list) else 'unknown'}")
    return "\n".join(lines)


def _first_env(names: tuple[str, ...]) -> str | None:
    return next((value for name in names if (value := os.environ.get(name, "").strip())), None)


def _deployment_text(install_shape: str) -> str:
    lines = [f"install_shape: {install_shape}"]
    if install_shape == "native":
        from modulo.launcher.supervisor import CHILD_LOG_NAMES

        lines.append(f"service_names: api, worker, {', '.join(CHILD_LOG_NAMES)}")
    else:
        services = os.environ.get("MODULO_SERVICES", "").strip()
        lines.append(f"service_names: {services or 'unset'}")

    for name in _IMAGE_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            lines.append(f"image_{name.lower()}: {value}")
    restart = _first_env(_RESTART_ENV)
    lines.append(f"restart_count: {restart or 'unknown'}")

    proxy_names = sorted(
        name for name in os.environ if name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    )
    lines.append(f"proxy_variable_names: {', '.join(proxy_names) if proxy_names else 'none'}")

    ca_bundle = _first_env(_CA_BUNDLE_ENV)
    lines.append(f"ca_bundle_path: {ca_bundle or 'unset'}")
    return "\n".join(lines)


def _version_text(clock_drift_seconds: float | None) -> str:
    from modulo.version import get_version

    sha = os.environ.get("GIT_SHA", "").strip()
    offset = datetime.now().astimezone().utcoffset()
    drift = f"{clock_drift_seconds:.3f} s" if clock_drift_seconds is not None else "unknown"
    return "\n".join(
        [
            f"modulo version: {get_version()}",
            f"build commit: {sha[:12] or 'unknown'}",
            f"python: {platform.python_version()}",
            f"os: {platform.platform()}",
            f"arch: {platform.machine()}",
            f"utc offset: {offset}",
            f"clock drift: {drift}",
        ]
    )


def _degraded_text(record: Mapping[str, Any]) -> str:
    crashes_raw = record.get("crashes")
    crashes: list[dict[str, Any]] = []
    if isinstance(crashes_raw, list):
        for crash in crashes_raw:
            if not isinstance(crash, Mapping):
                continue
            backtrace = crash.get("backtrace")
            crashes.append(
                {
                    "child": crash.get("child"),
                    "exit_code": crash.get("exit_code"),
                    "at": crash.get("at"),
                    "backtrace": backtrace if isinstance(backtrace, str) else None,
                }
            )
    return _json_text(
        {
            "degraded_at": record.get("degraded_at"),
            "reason": record.get("reason"),
            "crashes": crashes,
        }
    )


# ---------------------------------------------------------------------------
# Async (health + DB) collection
# ---------------------------------------------------------------------------


def _run_async_collection() -> AsyncCollected:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _all_async_missing("called from a running event loop")
    try:
        return asyncio.run(collect_async_members())
    except Exception as exc:  # last-resort: a broken collector must not lose the bundle
        return _all_async_missing(f"async collection failed ({type(exc).__name__})")


def _all_async_missing(reason: str) -> AsyncCollected:
    collected = AsyncCollected()
    for name, description in _ASYNC_MEMBERS:
        collected.missing[name] = f"{description} unavailable ({reason})"
    return collected


async def collect_async_members() -> AsyncCollected:
    """Collect health and DB-backed members; each is independently optional.

    Runs as the one-shot CLI's only event loop, so pooled connections are
    disposed before returning: a support command must not leave a connection
    (or a Python ResourceWarning) behind.
    """
    collected = AsyncCollected()
    try:
        await _collect_async_into(collected)
    finally:
        await _dispose_shared_engine()
    return collected


async def _collect_async_into(collected: AsyncCollected) -> None:
    try:
        from modulo.api.routes.health import evaluate_readiness

        response = await asyncio.wait_for(evaluate_readiness(), timeout=HEALTH_TIMEOUT_SECONDS)
        collected.health = {
            "status": response.status,
            "checks": [{"name": name, "status": check.status} for name, check in sorted(response.checks.items())],
            "omitted_fields": ["detail", "latency_ms"],
            "note": "status is the closed vocabulary; free-text detail is never included",
        }
    except Exception as exc:  # health is an optional member
        collected.missing["health.json"] = f"health check unavailable ({type(exc).__name__})"

    try:
        from modulo.db.session import AsyncSessionLocal
        from modulo.settings import get_settings
    except Exception as exc:
        _mark_db_missing(collected, exc)
        collected.licence = _sync_licence_payload_safe()
        return

    try:
        settings = get_settings()
    except Exception as exc:
        _mark_db_missing(collected, exc)
        return

    await _restore_persisted_licence(settings)

    try:
        async with AsyncSessionLocal() as session, session.begin():
            organisation = await _resolve_organisation(session, settings)
            from modulo.db.rls import set_rls_execution_context, set_rls_org

            await set_rls_org(session, organisation.id if organisation is not None else None)
            await set_rls_execution_context(session)
            collected.licence = _licence_payload(settings, organisation)
            await _guarded(session, collected, "db.txt", lambda: _collect_db(collected, session))
            await _guarded(session, collected, "errors.json", lambda: _collect_error_groups(collected, session))
            await _guarded(session, collected, "runs.json", lambda: _collect_failed_runs(collected, session))
            drift = await _clock_drift(session)
            collected.clock_drift_seconds = drift
    except Exception as exc:
        _mark_db_missing(collected, exc)
        if collected.licence is None:
            collected.licence = _sync_licence_payload_safe()


async def _dispose_shared_engine() -> None:
    try:
        from modulo.db.session import engine as shared_engine

        await shared_engine.dispose()
    except Exception:  # best-effort cleanup; never fail the bundle
        return


def _mark_db_missing(collected: AsyncCollected, exc: BaseException) -> None:
    reason = f"database unavailable ({type(exc).__name__})"
    for name, description in _ASYNC_MEMBERS:
        if name == "health.json":
            continue
        collected.missing.setdefault(name, f"{description} unavailable — {reason}")


async def _guarded(
    session: AsyncSession,
    collected: AsyncCollected,
    member: str,
    collector: Callable[[], Awaitable[None]],
) -> None:
    """Run one DB member in its own savepoint so a failure cannot poison the rest."""
    try:
        async with session.begin_nested():
            await collector()
    except Exception as exc:
        collected.missing[member] = f"{member} unavailable ({type(exc).__name__})"


async def _resolve_organisation(session: AsyncSession, settings: Settings) -> Organisation | None:
    from modulo.db.crud.organisation import resolve_login_tenant

    _, organisation = await resolve_login_tenant(session, settings)
    return organisation


async def _restore_persisted_licence(settings: Settings) -> None:
    try:
        from modulo.core.license_bootstrap import restore_persisted_license

        await restore_persisted_license(settings)
    except Exception:  # licence is optional; the env floor still resolves
        return


async def _collect_db(collected: AsyncCollected, session: AsyncSession) -> None:
    dialect = await _dialect(session)
    collected.database = {
        "dialect": dialect,
        "migration_head": await _applied_migration_heads(session),
        "expected_head": await asyncio.to_thread(_expected_migration_heads),
        "server_version": await _server_version(session, dialect),
        "row_counts": await _row_counts(session),
    }


async def _dialect(session: AsyncSession) -> str:
    get_bind = getattr(session, "get_bind", None)
    bind: Any = get_bind() if callable(get_bind) else None
    if hasattr(bind, "__await__"):
        bind = await bind
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    return dialect if isinstance(dialect, str) else "unknown"


async def _applied_migration_heads(session: AsyncSession) -> list[str] | None:
    from sqlalchemy import text

    try:
        rows = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    except Exception:
        return None
    return sorted(str(row) for row in rows)


def _expected_migration_heads() -> list[str] | None:
    """Repo migration heads (blocking; callers run it in a worker thread)."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from modulo.db.health_checks import resolve_alembic_ini

        alembic_ini = resolve_alembic_ini()
        config = Config(str(alembic_ini))
        config.set_main_option(
            "script_location",
            str(alembic_ini.parent / "src" / "modulo" / "db" / "migrations"),
        )
        return sorted(ScriptDirectory.from_config(config).get_heads())
    except Exception:
        return None


async def _server_version(session: AsyncSession, dialect: str) -> str | None:
    from sqlalchemy import text

    statements = {
        "postgresql": "SELECT current_setting('server_version')",
        "sqlite": "SELECT sqlite_version()",
    }
    statement = statements.get(dialect, "SELECT version()")
    try:
        value = await session.scalar(text(statement))
    except Exception:
        return None
    return str(value) if value is not None else None


async def _row_counts(session: AsyncSession) -> dict[str, int | None]:
    models = _row_count_models()
    counts: dict[str, int | None] = {}
    for table, model in models.items():
        try:
            async with session.begin_nested():
                from sqlalchemy import func, select

                counts[table] = int(await session.scalar(select(func.count()).select_from(model)) or 0)
        except Exception:
            counts[table] = None
    return counts


def _row_count_models() -> dict[str, Any]:
    from modulo.db.models.account import Account
    from modulo.db.models.agent import Agent
    from modulo.db.models.audit_event import AuditEvent
    from modulo.db.models.connector_instance import ConnectorInstance
    from modulo.db.models.error_event import ErrorEvent
    from modulo.db.models.error_group import ErrorGroup
    from modulo.db.models.model_backend import ModelBackend
    from modulo.db.models.organisation import Organisation
    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.pipeline_snapshot import PipelineSnapshot
    from modulo.db.models.run import Run
    from modulo.db.models.schema import Schema

    return {
        "runs": Run,
        "pipelines": Pipeline,
        "pipeline_snapshots": PipelineSnapshot,
        "agents": Agent,
        "schemas": Schema,
        "connector_instances": ConnectorInstance,
        "model_backends": ModelBackend,
        "error_groups": ErrorGroup,
        "error_events": ErrorEvent,
        "audit_events": AuditEvent,
        "accounts": Account,
        "organisations": Organisation,
    }


async def _collect_error_groups(collected: AsyncCollected, session: AsyncSession) -> None:
    from sqlalchemy import select

    from modulo.core.pipeline_engine.error_codes import sanitize_error_text
    from modulo.db.models.error_group import ErrorGroup

    groups = (
        (await session.execute(select(ErrorGroup).order_by(ErrorGroup.last_seen.desc()).limit(MAX_ERROR_GROUPS)))
        .scalars()
        .all()
    )
    rendered: list[dict[str, Any]] = []
    for group in groups:
        event = group.sample_event
        message = ""
        if event is not None and event.message:
            message = sanitize_error_text(event.message)[:MAX_ERROR_MESSAGE_CHARS]
        rendered.append(
            {
                "id": str(group.id),
                "fingerprint": group.fingerprint,
                "level": group.level_peak,
                "source": event.source if event is not None else None,
                "count": group.count,
                "first_seen": group.first_seen.isoformat() if group.first_seen else None,
                "last_seen": group.last_seen.isoformat() if group.last_seen else None,
                "environment": event.environment if event is not None else None,
                "version": event.version if event is not None else None,
                "message": message,
                "omitted": ["context_json", "stacktrace"],
            }
        )
    collected.error_groups = rendered


async def _collect_failed_runs(collected: AsyncCollected, session: AsyncSession) -> None:
    from sqlalchemy import select

    from modulo.db.models.pipeline import Pipeline
    from modulo.db.models.run import Run

    rows = (
        await session.execute(
            select(Run, Pipeline.name)
            .outerjoin(Pipeline, Pipeline.id == Run.pipeline_id)
            .where(Run.status.in_(FAILED_RUN_STATUSES))
            .order_by(Run.created_at.desc())
            .limit(MAX_FAILED_RUNS)
        )
    ).all()
    rendered: list[dict[str, Any]] = []
    for run, pipeline_name in rows:
        duration: float | None = None
        if run.started_at is not None and run.completed_at is not None:
            duration = (run.completed_at - run.started_at).total_seconds()
        rendered.append(
            {
                "run_id": str(run.id),
                "pipeline_id": str(run.pipeline_id),
                "pipeline_name": pipeline_name,
                "run_number": run.run_number,
                "status": run.status,
                "error_code": run.error_code,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "duration_seconds": duration,
                "total_cost_usd": str(run.total_cost_usd) if run.total_cost_usd is not None else None,
                "omitted": ["input_payload", "outputs", "prompts", "node_stdout", "error_detail"],
            }
        )
    collected.failed_runs = rendered


async def _clock_drift(session: AsyncSession) -> float | None:
    from sqlalchemy import text

    try:
        value = await session.scalar(text("SELECT CURRENT_TIMESTAMP"))
    except Exception:
        return None
    database_now = _coerce_datetime(value)
    if database_now is None:
        return None
    local_now = datetime.now(UTC)
    return abs((local_now - database_now).total_seconds())


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace(" ", "T", 1))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# Licence payload + rendering
# ---------------------------------------------------------------------------


def _sync_licence_payload_safe() -> dict[str, Any] | None:
    try:
        from modulo.settings import get_settings

        return _sync_licence_payload(get_settings())
    except Exception:
        return None


def _sync_licence_payload(settings: Settings) -> dict[str, Any]:
    return _licence_payload(settings, None)


def _licence_payload(settings: Settings, organisation: Organisation | None) -> dict[str, Any]:
    from modulo.core.feature_flags import FeatureFlagRegistry, resolve_effective_license

    try:
        data = resolve_effective_license(settings, organisation)
    except Exception:
        data = None
    tier = data.tier if data is not None else "community"
    registry = FeatureFlagRegistry(current_tier=tier, has_license_key=data is not None)
    enabled = sorted(flag.name for flag in registry.list_flags() if flag.currently_active)
    return {
        "tier": tier,
        "state": _licence_state(data),
        "expires_at": data.expires_at if data is not None else None,
        "licence_id": data.licence_id if data is not None else None,
        "renewal_mode": data.renewal_mode if data is not None else None,
        "features": sorted(data.features) if data is not None else [],
        "enabled_flags": enabled,
    }


def _licence_state(data: LicenseData | None) -> str:
    from modulo.core.license import LICENCE_GRACE_DAYS

    if data is None:
        return "none"
    expires = _coerce_datetime(getattr(data, "expires_at", None))
    if expires is None:
        return "unknown"
    now = datetime.now(UTC)
    if expires > now:
        return "renewal_due" if expires - now <= timedelta(days=LICENCE_GRACE_DAYS) else "active"
    return "grace" if now <= expires + timedelta(days=LICENCE_GRACE_DAYS) else "lapsed"


def _licence_text(payload: Mapping[str, Any]) -> str:
    features = payload.get("features")
    enabled = payload.get("enabled_flags")
    return "\n".join(
        [
            f"tier: {payload.get('tier')}",
            f"state: {payload.get('state')}",
            f"expires_at: {payload.get('expires_at')}",
            f"licence_id: {payload.get('licence_id')}",
            f"renewal_mode: {payload.get('renewal_mode')}",
            f"features: {', '.join(features) if isinstance(features, list) else ''}",
            f"enabled_flags: {', '.join(enabled) if isinstance(enabled, list) else ''}",
            "licence_key: never included",
        ]
    )


def _db_text(payload: Mapping[str, Any]) -> str:
    heads = payload.get("migration_head")
    expected = payload.get("expected_head")
    counts = payload.get("row_counts")
    lines = [
        f"dialect: {payload.get('dialect')}",
        f"migration_head: {', '.join(heads) if isinstance(heads, list) else 'unknown'}",
        f"expected_head: {', '.join(expected) if isinstance(expected, list) else 'unknown'}",
        f"server_version: {payload.get('server_version') or 'unknown'}",
        "row_counts:",
    ]
    if isinstance(counts, Mapping):
        for table in ROW_COUNT_TABLES:
            value = counts.get(table)
            lines.append(f"  {table}: {value if value is not None else 'unknown'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def collapse_repeated_lines(text: str) -> str:
    """Collapse runs of identical consecutive lines (a crash loop stays bounded)."""
    lines = text.splitlines()
    if not lines:
        return ""
    collapsed: list[str] = []
    index = 0
    while index < len(lines):
        end = index + 1
        while end < len(lines) and lines[end] == lines[index]:
            end += 1
        collapsed.append(lines[index])
        repeated = end - index - 1
        if repeated:
            collapsed.append(f"  ... (repeated {repeated} more time{'s' if repeated != 1 else ''})")
        index = end
    return "\n".join(collapsed)


def _cap_text(text: str, max_bytes: int, member: str, truncated: dict[str, str]) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated[member] = f"capped at {max_bytes} bytes"
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


__all__ = [
    "AsyncCollected",
    "BundleSources",
    "collapse_repeated_lines",
    "collect_async_members",
    "collect_sources",
    "redaction_map_from_data_dir",
]
