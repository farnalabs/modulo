"""Tests for the eval-definition creation/editing freeze (FAR-1100 chunk 3 → 3b).

The freeze guard is a single module (eval_definition_freeze.py) that blocks
POST /evals, PUT /evals/{id}, MCP create_eval_definition, and MCP
update_eval_definition with a typed 409 / error dict naming chunk 3b.

This file verifies:
- The guard module itself (raise_if_frozen, definition_frozen_response).
- REST routes return 409 with the chunk-3b message.
- MCP _impl functions return the typed error dict with the chunk-3b message.
- Deletion, list, and get are NOT frozen.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.eval_engine.eval_definition_freeze import (
    _DETAIL,
    _MCP_ERROR,
    CODE_EVALS_DEFINITION_FROZEN,
    definition_frozen_response,
    raise_if_frozen,
)
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock()
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


# ---------------------------------------------------------------------------
# Unit tests for the guard module itself
# ---------------------------------------------------------------------------


class TestFreezeGuardModule:
    def test_raise_if_frozen_always_raises(self) -> None:
        with pytest.raises(HTTPException, match="chunk 3b"):
            raise_if_frozen()

    def test_definition_frozen_response_returns_error_dict(self) -> None:
        result = definition_frozen_response()
        assert result is not None
        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]

    def test_error_code_constant(self) -> None:
        assert CODE_EVALS_DEFINITION_FROZEN == "evals.definition_frozen"

    def test_detail_mentions_farnalabs_ticket(self) -> None:
        assert "FAR-1100" in _DETAIL

    def test_mcp_error_shape_matches_response(self) -> None:
        result = definition_frozen_response()
        assert result == _MCP_ERROR


# ---------------------------------------------------------------------------
# REST route tests
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_session
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestFreezeRestPost:
    """POST /api/v1/evals returns 409 when frozen."""

    def test_post_returns_409(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/api/v1/evals",
            json={
                "pipeline_id": str(uuid.uuid4()),
                "name": "Test",
                "eval_type": "regex",
                "config_json": {"pattern": "x"},
            },
        )
        assert resp.status_code == 409
        body = resp.json()
        assert "chunk 3b" in body["detail"]

    def test_post_detail_contains_frozen_keyword(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/api/v1/evals",
            json={
                "pipeline_id": str(uuid.uuid4()),
                "name": "Test",
                "eval_type": "regex",
                "config_json": {},
            },
        )
        assert resp.status_code == 409
        assert "frozen" in resp.json()["detail"].lower()


class TestFreezeRestPut:
    """PUT /api/v1/evals/{id} returns 409 when frozen."""

    def test_put_returns_409(self, admin_client: TestClient) -> None:
        eval_id = uuid.uuid4()
        resp = admin_client.put(
            f"/api/v1/evals/{eval_id}",
            json={"name": "Updated"},
        )
        assert resp.status_code == 409
        body = resp.json()
        assert "chunk 3b" in body["detail"]

    def test_put_detail_contains_frozen_keyword(self, admin_client: TestClient) -> None:
        eval_id = uuid.uuid4()
        resp = admin_client.put(
            f"/api/v1/evals/{eval_id}",
            json={"name": "Updated"},
        )
        assert resp.status_code == 409
        assert "frozen" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# MCP _impl tests
# ---------------------------------------------------------------------------


class TestFreezeMcpCreate:
    """MCP create_eval_definition returns the freeze error dict."""

    def test_create_returns_frozen_error(self) -> None:
        from modulo.api.mcp_server import _create_eval_definition_impl

        result = _run_sync(
            _create_eval_definition_impl(
                pipeline_id=str(uuid.uuid4()),
                node_id=None,
                name="Test",
                eval_type="regex",
                config_json=None,
                failure_behaviour="warn",
                pass_threshold=None,
                suite_id=None,
            )
        )
        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]


class TestFreezeMcpUpdate:
    """MCP update_eval_definition returns the freeze error dict."""

    def test_update_returns_frozen_error(self) -> None:
        from modulo.api.mcp_server import _update_eval_definition_impl

        result = _run_sync(
            _update_eval_definition_impl(
                eval_id=str(uuid.uuid4()),
                node_id=None,
                name=None,
                eval_type=None,
                config_json=None,
                failure_behaviour=None,
                pass_threshold=None,
                suite_id=None,
            )
        )
        assert result["error"] == "definition_frozen"
        assert "chunk 3b" in result["detail"]


# ---------------------------------------------------------------------------
# Not-frozen routes (sanity checks)
# ---------------------------------------------------------------------------


class TestNotFrozen:
    """Verify that read/delete routes are NOT affected by the freeze."""

    def test_list_not_frozen(self, admin_client: TestClient) -> None:
        """GET /api/v1/evals should NOT return 409."""
        resp = admin_client.get("/api/v1/evals")
        # Should not be 409 — may be 200 or other, but not the freeze response
        assert resp.status_code != 409

    def test_get_not_frozen(self, admin_client: TestClient) -> None:
        """GET /api/v1/evals/{id} should NOT return 409."""
        eval_id = uuid.uuid4()
        resp = admin_client.get(f"/api/v1/evals/{eval_id}")
        # Should not be 409 — may be 404, but not the freeze response
        assert resp.status_code != 409

    def test_delete_not_frozen(self, admin_client: TestClient) -> None:
        """DELETE /api/v1/evals/{id} should NOT return 409."""
        eval_id = uuid.uuid4()
        resp = admin_client.delete(f"/api/v1/evals/{eval_id}")
        # Should not be 409 — may be 404 or 200, but not the freeze response
        assert resp.status_code != 409


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_sync(coro):
    """Run an async function synchronously for MCP _impl tests."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
