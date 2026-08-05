"""Health check endpoints — liveness, readiness, and dependency health.

PR B-2 (plan F7): ``/healthz/ready`` gains a machine-scoped SAQ worker check.
Each worker writes its metadata (``{"hostname": FLY_MACHINE_ID}``) to
``saq:{queue}:worker_info:{worker_id}``; this machine's readiness verifies that
a live worker for THIS hostname exists on EACH configured queue independently
(runs AND system — one live queue does not mask a dead sibling). Stale workers
for 4 consecutive probes => 503. Post-cutover (PR C) the gate is ALWAYS active
— there is no Celery path to fall back on — but can be relaxed to degraded
(alert-only) via ``SAQ_HARD_GATE=false`` after the hold (plan F7).
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import asyncpg  # type: ignore[import-untyped]
import redis.asyncio as aioredis
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import text

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_or_create_engine, pg_connection_string
from modulo.settings import Settings, break_glass_boot_findings, get_settings

_log = logging.getLogger(__name__)


router = APIRouter(tags=["health"])

VERSION = "0.1.0"
_START_TIME: datetime = datetime.now(UTC)

# 4 consecutive stale probes before 503 (plan F7): 4 x ~15-30s probe interval
# leaves margin over the 90s worker_info TTL (3 strikes = exactly the TTL was
# fragile). Counter is per-process (each web machine tracks its own).
_STALE_PROBE_LIMIT = 4
_consecutive_stale_probes: int = 0

# Break-glass watchdog state, published at boot by the lifespan and exposed on
# /healthz as ADVISORY only — it never flips readiness.
_break_glass_watchdog: dict[str, str] = {"status": "ok", "detail": "break-glass watchdog not run at boot"}


def set_break_glass_watchdog(status: str, detail: str) -> None:
    """Record the boot-time break-glass watchdog outcome (called by the lifespan)."""
    _break_glass_watchdog["status"] = status
    _break_glass_watchdog["detail"] = detail


class CheckResult(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    latency_ms: float | None = None
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    version: str
    uptime_seconds: float
    checks: dict[str, CheckResult]


def _check_break_glass() -> CheckResult:
    """ADVISORY break-glass watchdog exposure — never contributes to readiness.

    Re-evaluates the URL/secret-presence boot findings against the current
    settings; the allow-list/role-posture assertions are fatal at boot and do
    not recur here.
    """
    settings = get_settings()
    findings = break_glass_boot_findings(settings)
    if findings:
        return CheckResult(status="degraded", detail="; ".join(message for _blocking, message in findings))
    return CheckResult(status="ok", detail=_break_glass_watchdog.get("detail") or "break-glass boot config clean")


def _per_check_timeout(settings: Settings, override_field: str) -> float:
    """Resolve the timeout for one dependency check.

    Per-check overrides default to 0 (fall back to the global
    ``modulo_health_timeout_seconds`` value). This gives operators a single
    knob for the common case and a per-check knob for slow dependencies.
    """
    override: float = getattr(settings, override_field)
    if override and override > 0:
        return override
    return settings.modulo_health_timeout_seconds


def _timeout_result(
    status: Literal["unavailable", "degraded"],
    name: str,
    timeout: float,
    start: float,
) -> CheckResult:
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    return CheckResult(
        status=status,
        latency_ms=latency_ms,
        detail=f"{name} check timed out after {timeout:g}s",
    )


async def _check_database() -> CheckResult:
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_db_timeout_seconds")
    start = time.monotonic()

    async def _probe() -> None:
        engine = get_or_create_engine(settings)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_probe(), timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="ok",
            latency_ms=round(latency_ms, 1),
            detail="database reachable",
        )
    except TimeoutError:
        _log.warning("health._check_database", exc_info=True)
        return _timeout_result("unavailable", "database", timeout, start)
    except Exception as exc:
        _log.warning("health._check_database", exc_info=True)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="unavailable",
            latency_ms=round(latency_ms, 1),
            detail=str(exc),
        )


async def _check_redis() -> CheckResult:
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_redis_timeout_seconds")
    start = time.monotonic()
    r = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=timeout)
        await asyncio.wait_for(r.ping(), timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="ok",
            latency_ms=round(latency_ms, 1),
            detail="redis reachable",
        )
    except TimeoutError:
        _log.warning("health._check_redis", exc_info=True)
        return _timeout_result("degraded", "redis", timeout, start)
    except Exception as exc:
        _log.warning("health._check_redis", exc_info=True)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="degraded",
            latency_ms=round(latency_ms, 1),
            detail=str(exc),
        )
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()


async def _check_checkpointer() -> CheckResult:
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_checkpointer_timeout_seconds")
    start = time.monotonic()

    async def _probe() -> tuple[Literal["ok", "degraded"], str]:
        conn_string = pg_connection_string(settings.database_url)
        conn = await asyncpg.connect(conn_string, timeout=timeout)
        try:
            await conn.fetchrow("SELECT 1 FROM checkpoint_migrations LIMIT 1")
        except Exception as exc:
            _log.warning("health._check_checkpointer", exc_info=True)
            return "degraded", f"checkpoint_migrations table not accessible: {exc}"
        finally:
            with contextlib.suppress(Exception):
                await conn.close()
        return "ok", "checkpointer schema accessible"

    try:
        status, detail = await asyncio.wait_for(_probe(), timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(status=status, latency_ms=round(latency_ms, 1), detail=detail)
    except TimeoutError:
        _log.warning("health._check_checkpointer", exc_info=True)
        return _timeout_result("degraded", "checkpointer", timeout, start)
    except Exception as exc:
        _log.warning("health._check_checkpointer", exc_info=True)
        return CheckResult(
            status="degraded",
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            detail=str(exc) or "checkpointer check failed",
        )


def _resolve_alembic_ini() -> Path:
    """Locate backend/alembic.ini robustly regardless of the process cwd.

    Same pattern as ``modulo.api.main._resolve_alembic_ini`` — the readiness
    migration check must not depend on the cwd (the pre-commit test harness
    runs pytest from the repo root while CI and the container run from
    ``backend/``).
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            return candidate
    return Path("alembic.ini")


async def _check_migrations() -> CheckResult:
    settings = get_settings()
    timeout = _per_check_timeout(settings, "modulo_health_migrations_timeout_seconds")
    start = time.monotonic()

    async def _probe() -> tuple[Literal["ok", "degraded"], str]:
        alembic_ini = _resolve_alembic_ini()
        alembic_cfg = Config(str(alembic_ini))
        alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
        alembic_cfg.set_main_option(
            "script_location",
            str(alembic_ini.parent / "src" / "modulo" / "db" / "migrations"),
        )

        script = ScriptDirectory.from_config(alembic_cfg)
        heads = set(script.get_heads())

        engine = get_or_create_engine(settings)
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            applied = {row[0] for row in result.fetchall()}

        if heads.issubset(applied):
            return "ok", "migrations up to date"
        missing = heads - applied
        return "degraded", f"pending migrations: {', '.join(sorted(missing))}"

    try:
        status, detail = await asyncio.wait_for(_probe(), timeout=timeout)
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(status=status, latency_ms=round(latency_ms, 1), detail=detail)
    except TimeoutError:
        _log.warning("health._check_migrations", exc_info=True)
        return _timeout_result("degraded", "migrations", timeout, start)
    except Exception as exc:
        _log.warning("health._check_migrations", exc_info=True)
        return CheckResult(
            status="degraded",
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            detail=f"migration check failed: {exc}",
        )


async def _configured_queues() -> list[str]:
    """PREFIX-AWARE queue names for this environment (runs + system)."""
    settings = get_settings()
    runs_queue = settings.saq_runs_queue
    system_queue = runs_queue.replace("runs", "system") if "runs" in runs_queue else "system"
    return [runs_queue, system_queue]


async def _live_worker_hostnames(queue_name: str) -> set[str]:
    """Read live worker hostnames for *queue_name* from SAQ worker metadata.

    Live = a ``saq:{queue}:stats`` zset entry whose expiry score is in the
    future (worker_info timer 89s / TTL 90s). The metadata hash holds
    ``{"hostname": FLY_MACHINE_ID}`` written by the worker at startup.

    SAQ stores zset scores in MILLISECONDS (``saq.utils.now()`` is
    ``int(time.time() * 1000)``) — the comparison lower bound must be
    milliseconds too, or ``zrangebyscore(key, now_seconds, "+inf")`` matches
    every entry and stale workers are never filtered.
    """
    settings = get_settings()
    r: aioredis.Redis | None = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        stats_key = f"saq:{queue_name}:stats"
        now_ms = int(time.time() * 1000)
        member_keys = await r.zrangebyscore(stats_key, now_ms, "+inf")
        if not member_keys:
            return set()
        raw = await r.mget(member_keys)
        hostnames: set[str] = set()
        for blob in raw:
            if not blob:
                continue
            try:
                info = json.loads(blob)
            except (ValueError, TypeError):
                continue
            metadata = info.get("metadata") if isinstance(info, dict) else None
            hostname = (metadata or {}).get("hostname") if isinstance(metadata, dict) else None
            if hostname:
                hostnames.add(str(hostname))
        return hostnames
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._live_worker_hostnames queue=%s: %s", queue_name, exc)
        return set()
    finally:
        if r is not None:
            with contextlib.suppress(Exception):
                await r.aclose()


async def _check_saq_workers() -> CheckResult:
    """Machine-scoped SAQ worker staleness check (plan F7).

    Verifies THIS machine's workers (by FLY_MACHINE_ID hostname) are live on
    EACH configured queue independently (runs AND system). The check is
    machine-scoped: it only fails when THIS machine's worker is stale on ANY
    queue — a live system worker does not mask a dead runs worker on the same
    machine. After 4 consecutive stale probes the check reports
    ``unavailable`` (503). The 503 gate is ALWAYS active post-cutover (PR C —
    there is no Celery path), but ``SAQ_HARD_GATE=false`` relaxes it to
    degraded (alert-only) after the hold (plan F7).
    """
    global _consecutive_stale_probes

    settings = get_settings()
    this_host = os.environ.get("FLY_MACHINE_ID") or os.environ.get("HOSTNAME") or "unknown"
    try:
        queues = await _configured_queues()
        live_by_queue: dict[str, set[str]] = {qname: await _live_worker_hostnames(qname) for qname in queues}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("health._check_saq_workers failed: %s", exc)
        return CheckResult(status="ok", detail="saq worker check unavailable (redis read failed)")

    # THIS machine must be live on EVERY configured queue. A host that is live
    # on only one queue is a partially-dead worker and must fail the gate.
    missing_queues = [qname for qname, live in live_by_queue.items() if this_host not in live]
    this_machine_live = not missing_queues

    if this_machine_live:
        _consecutive_stale_probes = 0
        return CheckResult(
            status="ok",
            detail=f"saq workers live on this machine for all queues ({this_host})",
        )

    _consecutive_stale_probes += 1
    if settings.saq_hard_gate and _consecutive_stale_probes >= _STALE_PROBE_LIMIT:
        return CheckResult(
            status="unavailable",
            detail=(
                f"this machine's saq workers stale for {_consecutive_stale_probes} "
                f"consecutive probes (hostname={this_host}, stale_queues={sorted(missing_queues)}, "
                f"live_by_queue={live_by_queue})"
            ),
        )
    if settings.saq_hard_gate:
        return CheckResult(
            status="degraded",
            detail=(
                f"this machine's saq workers stale ({_consecutive_stale_probes}/"
                f"{_STALE_PROBE_LIMIT} probes; hostname={this_host}, stale_queues={sorted(missing_queues)})"
            ),
        )
    # SAQ_HARD_GATE=false (post-hold): alert-only — report ok so the check
    # never 503s a machine; alerting continues permanently (plan F7).
    _log.warning(
        "health.saq_workers_stale_relaxed hostname=%s stale_queues=%s probes=%d",
        this_host,
        sorted(missing_queues),
        _consecutive_stale_probes,
    )
    return CheckResult(
        status="ok",
        detail=f"saq workers stale (SAQ_HARD_GATE=false, alert-only) on this machine ({this_host})",
    )


@handle_db_errors("health.liveness")
@router.get("/healthz")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@handle_db_errors("health.readiness")
@router.get("/healthz/ready")
async def readiness(response: Response) -> ReadinessResponse:
    db_check, redis_check, cp_check, mig_check, saq_check = await asyncio.gather(
        _check_database(),
        _check_redis(),
        _check_checkpointer(),
        _check_migrations(),
        _check_saq_workers(),
    )
    bg_check = _check_break_glass()

    checks: dict[str, CheckResult] = {
        "database": db_check,
        "redis": redis_check,
        "checkpointer": cp_check,
        "migrations": mig_check,
        "saq_workers": saq_check,
        # ADVISORY only — excluded from the aggregate so a break-glass config
        # warning never degrades readiness (plan §3 watchdog reduction).
        "break_glass": bg_check,
    }

    # Aggregate over the NON-advisory checks only.
    statuses = [
        db_check.status,
        redis_check.status,
        cp_check.status,
        mig_check.status,
        saq_check.status,
    ]
    if "unavailable" in statuses:
        overall: Literal["ok", "degraded", "unavailable"] = "unavailable"
        response.status_code = 503
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "ok"

    uptime_seconds = (datetime.now(UTC) - _START_TIME).total_seconds()

    return ReadinessResponse(
        status=overall,
        version=VERSION,
        uptime_seconds=uptime_seconds,
        checks=checks,
    )
