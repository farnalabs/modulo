"""Route-level / integration tests for the product analytics endpoints (FAR-354).

These exercises the real FastAPI handlers (POST /consent, GET, PUT) end-to-end
through ``TestClient`` with a mocked DB session, so the permission gate, the
prompt-eligibility 409, and the FOR UPDATE org lock path are all round-tripped.

The DB session is mocked, so the org row is a MagicMock whose ``settings_json``
persists across requests within a single test — that lets us assert state
transitions (accept -> GET reflects level=all, re-accept -> 409).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.routes import product_analytics as pa_module
from modulo.api.routes.product_analytics import router as product_analytics_router
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal

# Mount only the product-analytics router on a minimal app so we exercise the
# real handlers through TestClient without pulling in the full API surface
# (and its optional extras such as the MCP server).
app = FastAPI()
app.include_router(product_analytics_router)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_CONSENT_URL = "/api/v1/org/product-analytics/consent"
_PRODUCT_ANALYTICS_URL = "/api/v1/org/product-analytics"


def _make_session() -> tuple[AsyncMock, MagicMock]:
    """Return (session, org) where org.settings_json persists across requests."""
    org = MagicMock()
    org.settings_json = {}
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=org)
    session.execute = AsyncMock(return_value=execute_result)
    return session, org


def _principal(org_role: str) -> TenantPrincipal:
    return TenantPrincipal(
        username="tester",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )


@pytest.fixture
def env():
    """Wire a mocked session + no-op side-effect deps into the app."""
    session, org = _make_session()
    with (
        patch.object(pa_module, "set_rls_org", new=AsyncMock()),
        patch.object(pa_module, "append_audit_event", new=AsyncMock()),
        patch.object(pa_module, "is_instance_analytics_enabled", new=AsyncMock(return_value=False)),
        patch.object(pa_module, "get_organisation", new=AsyncMock(return_value=org)),
    ):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_current_tenant_user] = lambda: _principal("admin")
        yield session, org
        app.dependency_overrides.clear()


def _client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /consent — accept path + prompt-eligibility gate (409)
# ---------------------------------------------------------------------------


class TestConsentEndpoint:
    def test_accept_returns_all_and_prompted(self, env) -> None:
        client = _client()
        resp = client.post(_CONSENT_URL, json={"action": "accept"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["level"] == "all"
        assert body["prompted"] == "yes"
        assert body["instance_enabled"] is False
        assert body["egress_allowed"] is False

    def test_reaccept_after_accept_is_409(self, env) -> None:
        client = _client()
        first = client.post(_CONSENT_URL, json={"action": "accept"})
        assert first.status_code == 200
        second = client.post(_CONSENT_URL, json={"action": "accept"})
        assert second.status_code == 409, second.text

    def test_decline_then_consent_ineligible_409(self, env) -> None:
        client = _client()
        decline = client.post(_CONSENT_URL, json={"action": "decline"})
        assert decline.status_code == 200
        # declined -> prompted=no (sticky) -> not eligible -> 409 on a later accept
        retry = client.post(_CONSENT_URL, json={"action": "accept"})
        assert retry.status_code == 409, retry.text

    def test_dismiss_then_accept_still_409_within_cooldown(self, env) -> None:
        client = _client()
        dismiss = client.post(_CONSENT_URL, json={"action": "dismiss"})
        assert dismiss.status_code == 200
        retry = client.post(_CONSENT_URL, json={"action": "accept"})
        assert retry.status_code == 409, retry.text


# ---------------------------------------------------------------------------
# GET / — reflects committed consent state
# ---------------------------------------------------------------------------


class TestGetEndpoint:
    def test_get_after_accept_reflects_level_all(self, env) -> None:
        client = _client()
        accept = client.post(_CONSENT_URL, json={"action": "accept"})
        assert accept.status_code == 200
        resp = client.get(_PRODUCT_ANALYTICS_URL)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["level"] == "all"
        assert body["prompted"] == "yes"

    def test_get_default_state(self, env) -> None:
        client = _client()
        resp = client.get(_PRODUCT_ANALYTICS_URL)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["level"] == "off"
        assert body["prompted"] is None


# ---------------------------------------------------------------------------
# PUT / — admin-only level toggle (permission gate)
# ---------------------------------------------------------------------------


class TestUpdateLevelEndpoint:
    def test_put_by_admin_succeeds(self, env) -> None:
        client = _client()
        resp = client.put(_PRODUCT_ANALYTICS_URL, json={"level": "all"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["level"] == "all"

    def test_put_by_non_admin_is_403(self, env) -> None:
        app.dependency_overrides[get_current_tenant_user] = lambda: _principal("member")
        client = _client()
        resp = client.put(_PRODUCT_ANALYTICS_URL, json={"level": "all"})
        assert resp.status_code == 403, resp.text

    def test_put_invalid_level_422(self, env) -> None:
        client = _client()
        resp = client.put(_PRODUCT_ANALYTICS_URL, json={"level": "bogus"})
        # The pydantic Literal validation rejects an invalid level before the
        # handler runs, so FastAPI returns 422 Unprocessable Entity.
        assert resp.status_code == 422, resp.text
