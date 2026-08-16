"""Unit tests for the /api/v1/admin/rate-limits API wire shape.

Guards the FAR-253 refactor: the admin rate-limits endpoints must construct
``RateLimitRule`` dataclass instances from the request fields and serve the
same JSON wire shape (``mode`` + ``rules[{path_prefix, max_requests,
window_s}]``) that they did before the dead-code cleanup removed the bare
``tuple[str, int, int]`` rule lists.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.middleware import rate_limiter as rl_mod
from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.api.routes.admin_rate_limits import router
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.rate_limiter import RateLimitRule
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_principal(role: str = "admin") -> TenantPrincipal:
    return TenantPrincipal(
        username="admin" if role == "admin" else "viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )


def _authz_result() -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=True)
    return result


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=_authz_result())
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Minimal app with only the admin rate-limits router wired in.

    Uses a bare FastAPI app (not ``modulo.api.main``) so the suite stays
    fast and never triggers MCP server startup or DB connection pooling.
    """
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    def override_settings() -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_admin_password="testpass",
            redis_url="",
        )

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = override_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_tenant_user] = lambda: _make_principal("admin")
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    original_redis_available = rl_mod.redis_available
    rl_mod.redis_available = False
    yield TestClient(app)
    rl_mod.redis_available = original_redis_available
    app.dependency_overrides.clear()


def _expected_rule_dict(rule: RateLimitRule) -> dict[str, object]:
    return {
        "path_prefix": rule.path_prefix,
        "max_requests": rule.max_requests,
        "window_s": rule.window_s,
    }


class TestGetRateLimits:
    def test_get_returns_current_rules_wire_shape(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/rate-limits")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data) == {"mode", "rules"}
        assert data["mode"] == "in_memory"
        assert data["rules"] == [_expected_rule_dict(r) for r in RateLimitMiddleware.RULES]
        assert data["rules"], "no rate limit rules returned"

    def test_get_lists_default_rule_prefixes(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/rate-limits")
        assert resp.status_code == 200
        prefixes = [r["path_prefix"] for r in resp.json()["rules"]]
        assert prefixes == [r.path_prefix for r in RateLimitMiddleware.RULES]
        assert "/api/v1/runs" in prefixes


class TestUpdateRateLimits:
    def test_put_constructs_rate_limit_rules_and_returns_same_shape(self, client: TestClient) -> None:
        original = list(RateLimitMiddleware.RULES)
        try:
            payload = {
                "rules": [
                    {"path_prefix": "/api/v1/limited", "max_requests": 3, "window_s": 5},
                    {"path_prefix": "/api/v1/other", "max_requests": 7, "window_s": 60},
                ]
            }
            resp = client.put("/api/v1/admin/rate-limits", json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert set(data) == {"mode", "rules"}
            assert data["mode"] == "in_memory"
            assert data["rules"] == [
                {"path_prefix": "/api/v1/limited", "max_requests": 3, "window_s": 5},
                {"path_prefix": "/api/v1/other", "max_requests": 7, "window_s": 60},
            ]
            # The PUT must have installed RateLimitRule dataclass instances
            # (never bare tuples), which the middleware consumes.
            assert [
                RateLimitRule(path_prefix="/api/v1/limited", max_requests=3, window_s=5),
                RateLimitRule(path_prefix="/api/v1/other", max_requests=7, window_s=60),
            ] == RateLimitMiddleware.RULES
        finally:
            RateLimitMiddleware.set_rules(original)

    def test_put_empty_rules_rejected(self, client: TestClient) -> None:
        resp = client.put("/api/v1/admin/rate-limits", json={"rules": []})
        assert resp.status_code == 400
        assert "At least one rate limit rule is required" in resp.json()["detail"]
