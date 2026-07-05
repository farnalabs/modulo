"""Health check endpoints — liveness, readiness, and dependency health."""

import asyncio
import time
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import text

from modulo.api.dependencies import get_or_create_engine, pg_connection_string
from modulo.settings import get_settings

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
    try:
        import redis.asyncio as aioredis

        r = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        await r.ping()
        await r.aclose()
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="ok",
            latency_ms=round(latency_ms, 1),
            detail="redis reachable",
        )
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            status="unavailable",
            latency_ms=round(latency_ms, 1),
            detail=str(exc),
        )


async def _check_checkpointer() -> CheckResult:
    settings = get_settings()
    try:
        import asyncpg  # type: ignore[import-untyped]

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
            try:
                await conn.close()
            except Exception:
                pass
        return CheckResult(status="ok", detail="checkpointer schema accessible")
    except Exception as exc:
        return CheckResult(status="degraded", detail=str(exc) or "checkpointer check failed")


async def _check_migrations() -> CheckResult:
    settings = get_settings()
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

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


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/healthz/ready")
async def readiness(response: Response) -> ReadinessResponse:
    db_check, redis_check, cp_check, mig_check = await asyncio.gather(
        _check_database(),
        _check_redis(),
        _check_checkpointer(),
        _check_migrations(),
    )

    checks: dict[str, CheckResult] = {
        "database": db_check,
        "redis": redis_check,
        "checkpointer": cp_check,
        "migrations": mig_check,
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
