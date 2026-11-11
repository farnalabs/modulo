"""Real-Postgres MCP-path coverage for System Maps discovery."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from farna.api.mcp_server import (
    _ctx_auth_token,
    _ctx_auth_type,
    _ctx_key_grants,
    _ctx_key_scope,
    _ctx_org_id,
    _ctx_role,
    _ctx_team_id,
    _ctx_user_id,
    _system_maps_http_error_payload,
    discover_system_map,
)
from farna.settings import Settings
from tests.integration.test_system_maps_ai_discovery import _insert_backend, _insert_eval

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def mcp_discovery_context(
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> AsyncGenerator[None, None]:
    factory = async_sessionmaker(app_engine, expire_on_commit=False, autobegin=False)
    _ctx_org_id.set(test_org)
    _ctx_user_id.set(test_user)
    _ctx_role.set("admin")
    _ctx_auth_type.set("api_key")
    _ctx_auth_token.set("mk_integration")
    _ctx_key_scope.set("org")
    _ctx_key_grants.set(frozenset({"system_maps_access"}))
    _ctx_team_id.set(None)
    settings = Settings(
        database_url="postgresql+asyncpg://unused/test",
        secret_key="a" * 32,
        fernet_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        modulo_admin_password="",
        modulo_dev_mode=True,
    )
    with (
        patch("farna.api.mcp_server.get_settings", return_value=settings),
        patch("farna.api.routes.system_maps.get_settings", return_value=settings),
        patch("farna.api.mcp_server._get_session_factory", return_value=factory),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
    ):
        yield
    for context_var in (
        _ctx_org_id,
        _ctx_user_id,
        _ctx_role,
        _ctx_auth_type,
        _ctx_auth_token,
        _ctx_key_scope,
        _ctx_key_grants,
        _ctx_team_id,
    ):
        context_var.set(None)


@pytest.mark.asyncio
async def test_mcp_wrapper_runs_against_real_postgres_and_never_publishes(
    mcp_discovery_context: None,
) -> None:
    result = await discover_system_map(name=f"MCP {uuid.uuid4().hex}")
    assert "error" not in result
    assert result["ai_assisted"] is False
    assert result["provenance"]["mode"] == "deterministic"
    assert result["published"] is False


@pytest.mark.asyncio
async def test_mcp_rejects_foreign_and_inactive_backend_and_unauthorised_eval(
    mcp_discovery_context: None,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    test_pipeline: uuid.UUID,
) -> None:
    inactive = await _insert_backend(db_engine, org_id=test_org, account_id=test_user, fixtures={}, status="disabled")
    inactive_result = await discover_system_map(model_backend_id=inactive)
    assert inactive_result["error"] == "model_backend_unauthorised"

    foreign_org = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(foreign_org), "name": "MCP Foreign", "slug": f"mcp-{foreign_org.hex[:8]}"},
        )
    foreign = await _insert_backend(db_engine, org_id=foreign_org, account_id=test_user, fixtures={})
    assert (await discover_system_map(model_backend_id=foreign))["error"] == "model_backend_unauthorised"

    eval_id = await _insert_eval(db_engine, org_id=test_org, account_id=test_user, pipeline_id=test_pipeline)
    _ctx_role.set("viewer")
    eval_result = await discover_system_map(model_backend_id=inactive, eval_definition_id=eval_id)
    assert eval_result["error"] == "insufficient_scope"


@pytest.mark.parametrize("verdict", ["fail", "error"])
def test_mcp_eval_gate_error_is_safe(verdict: str) -> None:
    error = StarletteHTTPException(
        status_code=422,
        detail={
            "code": "eval_gate_failed",
            "message": "AI-assisted discovery did not pass its evaluation gate",
            "verdict": verdict,
            "prompt": "secret prompt",
            "credentials": "secret credentials",
        },
    )
    payload = _system_maps_http_error_payload(error)
    assert payload == {
        "error": "eval_gate_failed",
        "detail": "AI-assisted discovery did not pass its evaluation gate",
        "verdict": verdict,
    }
    assert "secret" not in json.dumps(payload)
