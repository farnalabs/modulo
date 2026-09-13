"""Unit tests for /api/v1/admin/costs/components endpoints.

Covers CRUD happy paths, validation errors, reserved-name checks, formula
validation, cross-field validation, and DB-error conventions.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.cost_component import CostComponentKind
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_COMPONENT_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_PREFIX = "modulo.api.routes.cost_components."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=execute_result)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    plan = MagicMock()
    plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: plan
    # Patch get_settings at module level (model validators call it directly)
    # and _verify_identity to skip real DB auth verification in tests.
    p1 = patch(f"{_PREFIX}get_settings", return_value=_make_settings())
    p2 = patch("modulo.auth.dependencies._verify_identity", new_callable=AsyncMock, return_value="admin")
    p1.start()
    p2.start()
    yield TestClient(app)
    p2.stop()
    p1.stop()
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    plan = MagicMock()
    plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: plan
    p1 = patch(f"{_PREFIX}get_settings", return_value=_make_settings())
    p1.start()
    yield TestClient(app)
    p1.stop()
    app.dependency_overrides.clear()


def _make_component(**overrides: object) -> MagicMock:
    c = MagicMock()
    c.id = overrides.get("id", _COMPONENT_ID)
    c.name = overrides.get("name", "llm_tokens")
    c.display_name = overrides.get("display_name", "LLM Tokens")
    c.kind = overrides.get("kind", CostComponentKind.CALCULATED.value)
    c.rate_usd = overrides.get("rate_usd", Decimal("0.001"))
    c.rate_fallback = overrides.get("rate_fallback")
    c.formula = overrides.get("formula", "tokens_input * rate")
    c.report_key = overrides.get("report_key")
    c.enabled = overrides.get("enabled", True)
    c.sort_order = overrides.get("sort_order", 0)
    c.deleted_at = overrides.get("deleted_at")
    return c


def _rls_cm() -> ExitStack:
    """Patch the two RLS helpers used by cost_components routes."""
    stack = ExitStack()
    stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
    return stack


# ---------------------------------------------------------------------------
# GET /api/v1/admin/costs/components
# ---------------------------------------------------------------------------


class TestGetComponents:
    def test_returns_components(self, client: TestClient) -> None:
        comp = _make_component()
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}list_cost_components", new_callable=AsyncMock, return_value=[comp]))
            stack.enter_context(_rls_cm())
            resp = client.get("/api/v1/admin/costs/components")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["name"] == "llm_tokens"

    def test_returns_empty_list(self, client: TestClient) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}list_cost_components", new_callable=AsyncMock, return_value=[]))
            stack.enter_context(_rls_cm())
            resp = client.get("/api/v1/admin/costs/components")
        assert resp.status_code == 200
        assert not resp.json()


# ---------------------------------------------------------------------------
# POST /api/v1/admin/costs/components
# ---------------------------------------------------------------------------


class TestCreateComponent:
    def test_create_self_reported(self, client: TestClient) -> None:
        comp = _make_component(
            name="reported_cost",
            kind=CostComponentKind.SELF_REPORTED.value,
            formula=None,
            report_key="model_cost_usd",
        )
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}create_cost_component", new_callable=AsyncMock, return_value=comp))
            stack.enter_context(_rls_cm())
            stack.enter_context(patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock))
            resp = client.post(
                "/api/v1/admin/costs/components",
                json={
                    "name": "reported_cost",
                    "display_name": "Reported Cost",
                    "kind": "self_reported",
                    "report_key": "model_cost_usd",
                },
            )
        assert resp.status_code == 201
        assert resp.json()["name"] == "reported_cost"

    def test_create_calculated(self, client: TestClient) -> None:
        comp = _make_component(
            name="total_cost",
            kind=CostComponentKind.CALCULATED.value,
            formula="tokens_input * rate",
            report_key=None,
        )
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}create_cost_component", new_callable=AsyncMock, return_value=comp))
            stack.enter_context(_rls_cm())
            stack.enter_context(patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock))
            resp = client.post(
                "/api/v1/admin/costs/components",
                json={
                    "name": "total_cost",
                    "display_name": "Total Cost",
                    "kind": "calculated",
                    "formula": "tokens_input * rate",
                    "rate_usd": 0.001,
                },
            )
        assert resp.status_code == 201
        assert resp.json()["formula"] == "tokens_input * rate"

    def test_create_missing_name_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={"display_name": "X", "kind": "calculated", "formula": "rate"},
        )
        assert resp.status_code == 422

    def test_create_missing_kind_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={"name": "x", "display_name": "X", "formula": "rate"},
        )
        assert resp.status_code == 422

    def test_create_invalid_name_pattern_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={"name": "Invalid Name!", "display_name": "X", "kind": "calculated", "formula": "rate"},
        )
        assert resp.status_code == 422

    def test_create_reserved_name_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={"name": "reported", "display_name": "X", "kind": "self_reported", "report_key": "model_cost_usd"},
        )
        assert resp.status_code == 422

    def test_create_reserved_report_key_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={
                "name": "rk_test",
                "display_name": "X",
                "kind": "self_reported",
                "report_key": "reported",
            },
        )
        assert resp.status_code == 422

    def test_create_self_reported_with_formula_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={
                "name": "bad_sr",
                "display_name": "X",
                "kind": "self_reported",
                "formula": "rate * 2",
                "report_key": "model_cost_usd",
            },
        )
        assert resp.status_code == 422

    def test_create_calculated_missing_formula_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={"name": "bad_calc", "display_name": "X", "kind": "calculated"},
        )
        assert resp.status_code == 422

    def test_create_self_reported_missing_report_key_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={"name": "bad_sr2", "display_name": "X", "kind": "self_reported"},
        )
        assert resp.status_code == 422

    def test_create_calculated_with_report_key_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={
                "name": "bad_calc2",
                "display_name": "X",
                "kind": "calculated",
                "formula": "rate",
                "report_key": "model_cost_usd",
            },
        )
        assert resp.status_code == 422

    def test_create_unknown_rate_fallback_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={
                "name": "bad_fallback",
                "display_name": "X",
                "kind": "calculated",
                "formula": "rate",
                "rate_fallback": "nonexistent_fallback",
            },
        )
        assert resp.status_code == 422

    def test_create_invalid_formula_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/costs/components",
            json={
                "name": "bad_formula",
                "display_name": "X",
                "kind": "calculated",
                "formula": "eval('import os')",
                "rate_usd": 0.01,
            },
        )
        assert resp.status_code == 422

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(
            "/api/v1/admin/costs/components",
            json={"name": "x", "display_name": "X", "kind": "self_reported", "report_key": "model_cost_usd"},
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# PUT /api/v1/admin/costs/components/{component_id}
# ---------------------------------------------------------------------------


class TestUpdateComponent:
    def test_update_happy_path(self, client: TestClient) -> None:
        comp = _make_component(name="llm_tokens", display_name="Updated")
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}update_cost_component", new_callable=AsyncMock, return_value=comp))
            stack.enter_context(_rls_cm())
            stack.enter_context(patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock))
            resp = client.put(
                f"/api/v1/admin/costs/components/{_COMPONENT_ID}",
                json={"display_name": "Updated"},
            )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Updated"

    def test_update_not_found_returns_404(self, client: TestClient) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}update_cost_component", new_callable=AsyncMock, return_value=None))
            stack.enter_context(_rls_cm())
            resp = client.put(
                f"/api/v1/admin/costs/components/{_COMPONENT_ID}",
                json={"display_name": "X"},
            )
        assert resp.status_code == 404

    def test_update_empty_body(self, client: TestClient) -> None:
        comp = _make_component()
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}update_cost_component", new_callable=AsyncMock, return_value=comp))
            stack.enter_context(_rls_cm())
            stack.enter_context(patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock))
            resp = client.put(f"/api/v1/admin/costs/components/{_COMPONENT_ID}", json={})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# DELETE /api/v1/admin/costs/components/{component_id}
# ---------------------------------------------------------------------------


class TestDeleteComponent:
    def test_delete_happy_path(self, client: TestClient) -> None:
        comp = _make_component()
        with ExitStack() as stack:
            stack.enter_context(
                patch(f"{_PREFIX}soft_delete_cost_component", new_callable=AsyncMock, return_value=comp)
            )
            stack.enter_context(_rls_cm())
            stack.enter_context(patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock))
            resp = client.delete(f"/api/v1/admin/costs/components/{_COMPONENT_ID}")
        assert resp.status_code == 204

    def test_delete_not_found_returns_404(self, client: TestClient) -> None:
        with ExitStack() as stack:
            stack.enter_context(
                patch(f"{_PREFIX}soft_delete_cost_component", new_callable=AsyncMock, return_value=None)
            )
            stack.enter_context(_rls_cm())
            resp = client.delete(f"/api/v1/admin/costs/components/{_COMPONENT_ID}")
        assert resp.status_code == 404
