"""Unit tests for health endpoints — liveness, readiness, and dependency checks."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.api.routes.health import CheckResult
from modulo.settings import Settings, get_settings


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="test",
        redis_url="",
    )


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ok_check(name: str = "ok") -> CheckResult:
    return CheckResult(status="ok", latency_ms=1.0, detail=f"{name} reachable")


def _degraded_check(detail: str = "degraded") -> CheckResult:
    return CheckResult(status="degraded", detail=detail)


def _unavailable_check(detail: str = "unavailable") -> CheckResult:
    return CheckResult(status="unavailable", latency_ms=5000.0, detail=detail)


class TestLiveness:
    def test_healthz_returns_200(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestReadiness:
    def test_healthz_ready_mounted(self, client: TestClient) -> None:
        resp = client.get("/healthz/ready")
        assert resp.status_code in (200, 503)

    def test_healthz_ready_structure_when_unavailable(self, client: TestClient) -> None:
        resp = client.get("/healthz/ready")
        assert resp.status_code == 503
        body = resp.json()
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
