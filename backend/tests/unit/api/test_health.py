"""Tests for readiness endpoint — aggregation logic, degraded/unavailable status, and check structure."""

import asyncio
from collections.abc import Generator
from typing import Self
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.api.routes.health import (
    CheckResult,
    _check_checkpointer,
    _check_database,
    _check_migrations,
    _check_redis,
    _per_check_timeout,
)
from modulo.settings import Settings, get_settings


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="test",
        redis_url="redis://localhost:6379/0",
    )


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ok_check(name: str = "ok") -> CheckResult:
    return CheckResult(status="ok", latency_ms=1.0, detail=f"{name} reachable")


def _degraded_check(detail: str = "degraded") -> CheckResult:
    return CheckResult(status="degraded", detail=detail)


def _unavailable_check(detail: str = "unavailable") -> CheckResult:
    return CheckResult(status="unavailable", latency_ms=5000.0, detail=detail)


class TestReadiness:
    def test_healthz_ready_mounted(self, client: TestClient) -> None:
        resp = client.get("/healthz/ready")
        assert resp.status_code in (200, 503, 504)

    def test_healthz_ready_structure_when_unavailable(self, client: TestClient) -> None:
        resp = client.get("/healthz/ready")
        assert resp.status_code in (200, 503, 504)
        body = resp.json()
        if resp.status_code == 504:
            return
        if resp.status_code == 503:
            assert body["status"] == "unavailable"
        assert body["version"] == "0.1.0"
        assert isinstance(body["uptime_seconds"], float)
        assert isinstance(body["checks"], dict)
        for key in ("database", "redis", "checkpointer", "migrations"):
            assert key in body["checks"], f"missing check key: {key}"

    def test_healthz_ready_degraded_overall(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch(
                "modulo.api.routes.health._check_redis", AsyncMock(return_value=_degraded_check("redis not configured"))
            ),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["redis"]["status"] == "degraded"
        for key in ("database", "checkpointer", "migrations"):
            assert body["checks"][key]["status"] == "ok"

    def test_healthz_ready_unavailable_overall(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_unavailable_check("db down"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        assert body["checks"]["database"]["status"] == "unavailable"

    def test_healthz_ready_all_ok(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        for key in ("database", "redis", "checkpointer", "migrations"):
            assert body["checks"][key]["status"] == "ok"

    def test_healthz_ready_check_keys_present(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.health._check_database", AsyncMock(return_value=_ok_check("database"))),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
        ):
            resp = client.get("/healthz/ready")
        body = resp.json()
        for key in ("database", "redis", "checkpointer", "migrations"):
            c = body["checks"][key]
            assert "status" in c
            assert "latency_ms" in c
            assert "detail" in c


class TestHttpTimeout:
    def test_healthz_ready_with_timeout_check(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.health._check_database",
                AsyncMock(return_value=_unavailable_check("timeout after 5s")),
            ),
            patch("modulo.api.routes.health._check_redis", AsyncMock(return_value=_ok_check("redis"))),
            patch("modulo.api.routes.health._check_checkpointer", AsyncMock(return_value=_ok_check("checkpointer"))),
            patch("modulo.api.routes.health._check_migrations", AsyncMock(return_value=_ok_check("migrations"))),
        ):
            resp = client.get("/healthz/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        assert "timeout" in body["checks"]["database"]["detail"].lower()


class _HangingEngine:
    """Fake SQLAlchemy engine whose connect() never returns."""

    def connect(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        await asyncio.sleep(60)
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class TestPerCheckTimeouts:
    """Configurable per-check timeout limits (feat-infra-health)."""

    def test_settings_defaults_apply(self) -> None:
        settings = _make_settings()
        assert settings.modulo_health_timeout_seconds == 5.0
        for field in (
            "modulo_health_db_timeout_seconds",
            "modulo_health_redis_timeout_seconds",
            "modulo_health_checkpointer_timeout_seconds",
            "modulo_health_migrations_timeout_seconds",
        ):
            assert getattr(settings, field) == 0.0

    def test_per_check_timeout_falls_back_to_global(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_timeout_seconds": 2.5})
        assert _per_check_timeout(settings, "modulo_health_db_timeout_seconds") == 2.5
        assert _per_check_timeout(settings, "modulo_health_redis_timeout_seconds") == 2.5

    def test_per_check_timeout_override_wins(self) -> None:
        settings = _make_settings().model_copy(
            update={
                "modulo_health_timeout_seconds": 2.5,
                "modulo_health_redis_timeout_seconds": 0.5,
            }
        )
        assert _per_check_timeout(settings, "modulo_health_db_timeout_seconds") == 2.5
        assert _per_check_timeout(settings, "modulo_health_redis_timeout_seconds") == 0.5

    async def test_database_check_times_out(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_timeout_seconds": 0.2})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=_HangingEngine()),
        ):
            result = await _check_database()
        assert result.status == "unavailable"
        assert "timed out after 0.2s" in result.detail.lower()
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000

    async def test_redis_check_times_out(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_redis_timeout_seconds": 0.2})

        async def _hang() -> None:
            await asyncio.sleep(60)

        redis_client = AsyncMock()
        redis_client.ping.side_effect = _hang
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.aioredis.Redis.from_url", return_value=redis_client),
        ):
            result = await _check_redis()
        assert result.status == "degraded"
        assert "timed out after 0.2s" in result.detail.lower()
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000

    async def test_checkpointer_check_times_out(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_checkpointer_timeout_seconds": 0.2})

        async def _hang(*args: object, **kwargs: object) -> None:
            await asyncio.sleep(60)

        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.pg_connection_string", return_value="postgresql://test"),
            patch("modulo.api.routes.health.asyncpg.connect", side_effect=_hang),
        ):
            result = await _check_checkpointer()
        assert result.status == "degraded"
        assert "timed out after 0.2s" in result.detail.lower()
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000

    async def test_migrations_check_times_out(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_migrations_timeout_seconds": 0.2})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine", return_value=_HangingEngine()),
        ):
            result = await _check_migrations()
        assert result.status == "degraded"
        assert "timed out after 0.2s" in result.detail.lower()
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000

    async def test_database_check_ok_reports_latency(self) -> None:
        settings = _make_settings().model_copy(update={"modulo_health_timeout_seconds": 1.0})
        with (
            patch("modulo.api.routes.health.get_settings", return_value=settings),
            patch("modulo.api.routes.health.get_or_create_engine") as engine_factory,
        ):
            engine = AsyncMock()
            conn = AsyncMock()
            conn.__aenter__ = AsyncMock(return_value=conn)
            conn.__aexit__ = AsyncMock(return_value=None)
            engine.connect = lambda: conn
            engine_factory.return_value = engine
            result = await _check_database()
        assert result.status == "ok"
        assert result.latency_ms is not None
        assert result.latency_ms < 60_000
