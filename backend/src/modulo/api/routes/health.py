"""Health check endpoints — liveness, readiness, and dependency health."""

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
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
from modulo.core.background_pipeline_worker import BackgroundPipelineWorker
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

_bg_worker_ref: BackgroundPipelineWorker | None = None


def set_worker_ref(worker: BackgroundPipelineWorker | None) -> None:
    global _bg_worker_ref
    _bg_worker_ref = worker


router = APIRouter(tags=["health"])

VERSION = "0.1.0"
_START_TIME: datetime = datetime.now(UTC)


class CheckResult(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    latency_ms: float | None = None
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    version: str
    uptime_seconds: float
    checks: dict[str, CheckResult]


async def _check_database() -> CheckResult:
    settings = get_settings()
    engine = get_or_create_engine(settings)
    start = time.monotonic()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="ok",
            latency_ms=round(latency_ms, 1),
            detail="database reachable",
        )
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="unavailable",
            latency_ms=round(latency_ms, 1),
            detail=str(exc),
        )


async def _check_redis() -> CheckResult:
    settings = get_settings()
    if not settings.redis_url:
        return CheckResult(status="degraded", detail="redis not configured")
    start = time.monotonic()
    r = None
    try:
        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        await r.ping()
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="ok",
            latency_ms=round(latency_ms, 1),
            detail="redis reachable",
        )
    except Exception as exc:
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
    try:
        conn_string = pg_connection_string(settings.database_url)
        conn = await asyncpg.connect(conn_string, timeout=5)
        try:
            await conn.fetchrow("SELECT 1 FROM checkpoint_migrations LIMIT 1")
        except Exception as exc:
            return CheckResult(
                status="degraded",
                detail=f"checkpoint_migrations table not accessible: {exc}",
            )
        finally:
            with contextlib.suppress(Exception):
                await conn.close()
        return CheckResult(status="ok", detail="checkpointer schema accessible")
    except Exception as exc:
        return CheckResult(status="degraded", detail=str(exc) or "checkpointer check failed")


async def _check_migrations() -> CheckResult:
    settings = get_settings()
    try:
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)

        script = ScriptDirectory.from_config(alembic_cfg)
        heads = set(script.get_heads())

        engine = get_or_create_engine(settings)
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            applied = {row[0] for row in result.fetchall()}

        if heads.issubset(applied):
            return CheckResult(status="ok", detail="migrations up to date")
        missing = heads - applied
        return CheckResult(
            status="degraded",
            detail=f"pending migrations: {', '.join(sorted(missing))}",
        )
    except Exception as exc:
        return CheckResult(status="degraded", detail=f"migration check failed: {exc}")


@handle_db_errors("health.liveness")
@router.get("/healthz")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


async def _check_background_worker() -> CheckResult:
    if _bg_worker_ref is None:
        return CheckResult(status="degraded", detail="background worker not initialized")
    info = _bg_worker_ref.info()
    if info["started"]:
        return CheckResult(
            status="ok",
            detail=f"queue_depth={info['queue_depth']}, in_flight={info['in_flight']}",
        )
    return CheckResult(status="degraded", detail="background worker not started")


@handle_db_errors("health.readiness")
@router.get("/healthz/ready")
async def readiness(response: Response) -> ReadinessResponse:
    db_check, redis_check, cp_check, mig_check, bg_check = await asyncio.gather(
        _check_database(),
        _check_redis(),
        _check_checkpointer(),
        _check_migrations(),
        _check_background_worker(),
    )

    checks: dict[str, CheckResult] = {
        "database": db_check,
        "redis": redis_check,
        "checkpointer": cp_check,
        "migrations": mig_check,
        "background_worker": bg_check,
    }

    statuses = [c.status for c in checks.values()]
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
