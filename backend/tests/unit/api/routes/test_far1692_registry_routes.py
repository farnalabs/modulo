"""Unit tests for the FAR-1692 registry routes (external agents + MCP servers).

No Docker: the FastAPI app is driven with ``TestClient`` and the tenant/session
dependencies are overridden; the route-module CRUD seams are patched so every
success path AND every error arm (ProgrammingError -> 501, IntegrityError -> 409,
SQLAlchemyError -> 503, HTTPException passthrough, unexpected -> 500) plus each
not-found / validation branch is exercised. This closes the changed-lines
coverage gate on ``api/routes/external_agents.py`` and ``api/routes/mcp_servers.py``.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from tests.unit.api.mock_session import configure_rls_preamble

import modulo.api.routes.external_agents as ext_mod
import modulo.api.routes.mcp_servers as mcp_mod
from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.mcp.tool_pinning import ObservationResult, ToolPinningError
from modulo.db.crud.base import PageResult
from modulo.db.models.external_agent import ExternalAgent
from modulo.db.models.mcp_server import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_UNAPPROVED,
    McpServer,
)
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_AGENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SERVER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)
_FERNET_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()

# Error-arm matrices. ``HTTPException`` proves the passthrough arm (its 403 must
# survive untouched — a mapped status, so the RFC 9457 rendering cannot mask it
# as a generic 500); a bare ``RuntimeError`` proves the 500 backstop.
_ERRORS = [
    pytest.param(ProgrammingError("stmt", {}, Exception("missing table")), 501, id="programming-501"),
    pytest.param(SQLAlchemyError("boom"), 503, id="sqlalchemy-503"),
    pytest.param(HTTPException(status_code=403, detail="denied"), 403, id="http-passthrough"),
    pytest.param(RuntimeError("boom"), 500, id="unexpected-500"),
]
_ERRORS_WITH_INTEGRITY = [
    pytest.param(ProgrammingError("stmt", {}, Exception("missing table")), 501, id="programming-501"),
    pytest.param(IntegrityError("stmt", {}, Exception("dup")), 409, id="integrity-409"),
    pytest.param(SQLAlchemyError("boom"), 503, id="sqlalchemy-503"),
    pytest.param(HTTPException(status_code=403, detail="denied"), 403, id="http-passthrough"),
    pytest.param(RuntimeError("boom"), 500, id="unexpected-500"),
]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _result() -> MagicMock:
    r = MagicMock()
    r.scalar = MagicMock(return_value=0)
    r.scalar_one = MagicMock(return_value=0)
    r.scalar_one_or_none = MagicMock(return_value=None)
    r.all = MagicMock(return_value=[])
    r.first = MagicMock(return_value=None)
    r.rowcount = 0
    sc = MagicMock()
    sc.all = MagicMock(return_value=[])
    sc.__iter__.return_value = iter([])
    r.scalars = MagicMock(return_value=sc)
    return r


def _make_session() -> AsyncMock:
    session = AsyncMock()
    configure_rls_preamble(session)
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=MagicMock(dialect=MagicMock(name="sqlite")))
    session.info = {}
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=_result())
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    session = _make_session()

    async def _override_db() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _principal
    app.dependency_overrides[get_current_tenant_user] = _principal
    # The isolated ``audited(...)`` session is NOT overridden here: the global
    # autouse ``_isolated_audit_session`` fixture installs the contract-correct
    # ``_AuditTestSession`` double. A local override yielding a bare ``AsyncMock``
    # made ``async with session.begin()`` orphan a never-awaited coroutine on
    # every audited request (PytestUnraisableExceptionWarning flood).
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _agent(**over: Any) -> ExternalAgent:
    return ExternalAgent(
        id=over.get("id", _AGENT_ID),
        organisation_id=over.get("organisation_id", _ORG_ID),
        name=over.get("name", "agent"),
        runtime_kind=over.get("runtime_kind", "other"),
        host_descriptor=over.get("host_descriptor"),
        status=over.get("status", "active"),
        discovery_provenance=over.get("discovery_provenance", "manual"),
        first_seen=over.get("first_seen"),
        last_seen=over.get("last_seen"),
        visibility=over.get("visibility", "org"),
        owner_team_id=over.get("owner_team_id"),
        created_at=over.get("created_at", _NOW),
        updated_at=over.get("updated_at", _NOW),
    )


def _server(**over: Any) -> McpServer:
    return McpServer(
        id=over.get("id", _SERVER_ID),
        organisation_id=over.get("organisation_id", _ORG_ID),
        name=over.get("name", "server"),
        transport=over.get("transport", "http"),
        endpoint=over.get("endpoint", "https://mcp.example/sse"),
        command=over.get("command"),
        declared_scopes=over.get("declared_scopes", []),
        owner_account_id=over.get("owner_account_id"),
        credentials_ciphertext=over.get("credentials_ciphertext"),
        tools_json=over.get("tools_json", []),
        tools_hash=over.get("tools_hash"),
        approval_status=over.get("approval_status", APPROVAL_STATUS_UNAPPROVED),
        approved_by=over.get("approved_by"),
        approved_at=over.get("approved_at"),
        approved_hash=over.get("approved_hash"),
        last_observed_at=over.get("last_observed_at"),
        visibility=over.get("visibility", "org"),
        owner_team_id=over.get("owner_team_id"),
        created_at=over.get("created_at", _NOW),
        updated_at=over.get("updated_at", _NOW),
    )


# ---------------------------------------------------------------------------
# external agents
# ---------------------------------------------------------------------------


AGENTS = "/api/v1/external-agents"


def test_list_external_agents_success(client: TestClient) -> None:
    page = PageResult(items=[_agent(), _agent(id=uuid.uuid4(), name="b")], total=2, page=1, page_size=20)
    with patch.object(ext_mod, "list_external_agents", AsyncMock(return_value=page)):
        resp = client.get(AGENTS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


@pytest.mark.parametrize(("exc", "status"), _ERRORS)
def test_list_external_agents_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with patch.object(ext_mod, "list_external_agents", AsyncMock(side_effect=exc)):
        resp = client.get(AGENTS)
    assert resp.status_code == status


def test_create_external_agent_success(client: TestClient) -> None:
    with (
        patch.object(ext_mod, "validate_owner_team_for_create", AsyncMock()),
        patch.object(ext_mod, "create_external_agent", AsyncMock(return_value=_agent(name="claude"))),
    ):
        resp = client.post(AGENTS, json={"name": "claude", "runtime_kind": "claude-code"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "claude"


def test_create_external_agent_team_visibility(client: TestClient) -> None:
    create = AsyncMock(return_value=_agent(name="team-agent", visibility="team", owner_team_id=_TEAM_ID))
    with (
        patch.object(ext_mod, "validate_owner_team_for_create", AsyncMock()) as validate,
        patch.object(ext_mod, "create_external_agent", create),
    ):
        resp = client.post(AGENTS, json={"name": "team-agent", "visibility": "team", "owner_team_id": str(_TEAM_ID)})
    assert resp.status_code == 201
    validate.assert_awaited_once()


@pytest.mark.parametrize(("exc", "status"), _ERRORS_WITH_INTEGRITY)
def test_create_external_agent_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with (
        patch.object(ext_mod, "validate_owner_team_for_create", AsyncMock()),
        patch.object(ext_mod, "create_external_agent", AsyncMock(side_effect=exc)),
    ):
        resp = client.post(AGENTS, json={"name": "x"})
    assert resp.status_code == status


def test_get_external_agent_success(client: TestClient) -> None:
    with patch.object(ext_mod, "get_external_agent", AsyncMock(return_value=_agent())):
        resp = client.get(f"{AGENTS}/{_AGENT_ID}")
    assert resp.status_code == 200


def test_get_external_agent_not_found(client: TestClient) -> None:
    with patch.object(ext_mod, "get_external_agent", AsyncMock(return_value=None)):
        resp = client.get(f"{AGENTS}/{_AGENT_ID}")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "status"), _ERRORS)
def test_get_external_agent_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with patch.object(ext_mod, "get_external_agent", AsyncMock(side_effect=exc)):
        resp = client.get(f"{AGENTS}/{_AGENT_ID}")
    assert resp.status_code == status


def test_update_external_agent_success(client: TestClient) -> None:
    with (
        patch.object(ext_mod, "get_external_agent", AsyncMock(return_value=_agent())),
        patch.object(ext_mod, "update_external_agent", AsyncMock(return_value=_agent(name="renamed"))),
    ):
        resp = client.patch(f"{AGENTS}/{_AGENT_ID}", json={"name": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"


def test_update_external_agent_visibility_transition(client: TestClient) -> None:
    with (
        patch.object(ext_mod, "get_external_agent", AsyncMock(return_value=_agent())),
        patch.object(ext_mod, "validate_team_transition_for_update", AsyncMock()) as validate,
        patch.object(
            ext_mod,
            "update_external_agent",
            AsyncMock(return_value=_agent(visibility="team", owner_team_id=_TEAM_ID)),
        ),
    ):
        resp = client.patch(f"{AGENTS}/{_AGENT_ID}", json={"visibility": "team", "owner_team_id": str(_TEAM_ID)})
    assert resp.status_code == 200
    validate.assert_awaited_once()


def test_update_external_agent_missing_before_update(client: TestClient) -> None:
    with (
        patch.object(ext_mod, "get_external_agent", AsyncMock(return_value=None)),
        patch.object(ext_mod, "update_external_agent", AsyncMock()),
    ):
        resp = client.patch(f"{AGENTS}/{_AGENT_ID}", json={"name": "x"})
    assert resp.status_code == 404


def test_update_external_agent_missing_after_update(client: TestClient) -> None:
    with (
        patch.object(ext_mod, "get_external_agent", AsyncMock(return_value=_agent())),
        patch.object(ext_mod, "update_external_agent", AsyncMock(return_value=None)),
    ):
        resp = client.patch(f"{AGENTS}/{_AGENT_ID}", json={"name": "x"})
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "status"), _ERRORS_WITH_INTEGRITY)
def test_update_external_agent_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with (
        patch.object(ext_mod, "get_external_agent", AsyncMock(return_value=_agent())),
        patch.object(ext_mod, "update_external_agent", AsyncMock(side_effect=exc)),
    ):
        resp = client.patch(f"{AGENTS}/{_AGENT_ID}", json={"name": "x"})
    assert resp.status_code == status


def test_delete_external_agent_success(client: TestClient) -> None:
    with patch.object(ext_mod, "soft_delete_external_agent", AsyncMock(return_value=True)):
        resp = client.delete(f"{AGENTS}/{_AGENT_ID}")
    assert resp.status_code == 204


def test_delete_external_agent_not_found(client: TestClient) -> None:
    with patch.object(ext_mod, "soft_delete_external_agent", AsyncMock(return_value=False)):
        resp = client.delete(f"{AGENTS}/{_AGENT_ID}")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "status"), _ERRORS)
def test_delete_external_agent_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with patch.object(ext_mod, "soft_delete_external_agent", AsyncMock(side_effect=exc)):
        resp = client.delete(f"{AGENTS}/{_AGENT_ID}")
    assert resp.status_code == status


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


SERVERS = "/api/v1/mcp-servers"


def test_list_mcp_servers_success(client: TestClient) -> None:
    page = PageResult(items=[_server()], total=1, page=1, page_size=20)
    with patch.object(mcp_mod, "list_mcp_servers", AsyncMock(return_value=page)):
        resp = client.get(SERVERS)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.parametrize(("exc", "status"), _ERRORS)
def test_list_mcp_servers_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with patch.object(mcp_mod, "list_mcp_servers", AsyncMock(side_effect=exc)):
        resp = client.get(SERVERS)
    assert resp.status_code == status


def test_create_mcp_server_success_without_credentials(client: TestClient) -> None:
    with (
        patch.object(mcp_mod, "validate_owner_team_for_create", AsyncMock()),
        patch.object(mcp_mod, "create_mcp_server", AsyncMock(return_value=_server(name="plain"))),
    ):
        resp = client.post(SERVERS, json={"name": "plain", "transport": "http"})
    assert resp.status_code == 201
    assert resp.json()["has_credentials"] is False


def test_create_mcp_server_success_with_credentials(client: TestClient) -> None:
    create = AsyncMock(return_value=_server(name="secret", credentials_ciphertext=b"cipher"))
    with (
        patch.object(mcp_mod, "validate_owner_team_for_create", AsyncMock()),
        patch.object(mcp_mod, "create_mcp_server", create),
    ):
        resp = client.post(SERVERS, json={"name": "secret", "transport": "http", "credentials": "s3cr3t"})
    assert resp.status_code == 201
    assert resp.json()["has_credentials"] is True
    # The credential is encrypted before it reaches the CRUD layer and never echoed.
    assert create.call_args.kwargs["credentials_ciphertext"] is not None
    assert "s3cr3t" not in resp.text


@pytest.mark.parametrize(("exc", "status"), _ERRORS_WITH_INTEGRITY)
def test_create_mcp_server_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with (
        patch.object(mcp_mod, "validate_owner_team_for_create", AsyncMock()),
        patch.object(mcp_mod, "create_mcp_server", AsyncMock(side_effect=exc)),
    ):
        resp = client.post(SERVERS, json={"name": "x", "transport": "http"})
    assert resp.status_code == status


def test_get_mcp_server_success(client: TestClient) -> None:
    with patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server())):
        resp = client.get(f"{SERVERS}/{_SERVER_ID}")
    assert resp.status_code == 200


def test_get_mcp_server_not_found(client: TestClient) -> None:
    with patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=None)):
        resp = client.get(f"{SERVERS}/{_SERVER_ID}")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "status"), _ERRORS)
def test_get_mcp_server_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with patch.object(mcp_mod, "get_mcp_server", AsyncMock(side_effect=exc)):
        resp = client.get(f"{SERVERS}/{_SERVER_ID}")
    assert resp.status_code == status


def test_update_mcp_server_success(client: TestClient) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server())),
        patch.object(mcp_mod, "update_mcp_server", AsyncMock(return_value=_server(name="renamed"))),
    ):
        resp = client.patch(f"{SERVERS}/{_SERVER_ID}", json={"name": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"


def test_update_mcp_server_with_credentials(client: TestClient) -> None:
    update = AsyncMock(return_value=_server(credentials_ciphertext=b"cipher"))
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server())),
        patch.object(mcp_mod, "update_mcp_server", update),
    ):
        resp = client.patch(f"{SERVERS}/{_SERVER_ID}", json={"credentials": "new-secret"})
    assert resp.status_code == 200
    assert update.call_args.args[2]["credentials_ciphertext"] is not None
    assert "new-secret" not in resp.text


def test_update_mcp_server_visibility_transition(client: TestClient) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server())),
        patch.object(mcp_mod, "validate_team_transition_for_update", AsyncMock()) as validate,
        patch.object(
            mcp_mod,
            "update_mcp_server",
            AsyncMock(return_value=_server(visibility="team", owner_team_id=_TEAM_ID)),
        ),
    ):
        resp = client.patch(f"{SERVERS}/{_SERVER_ID}", json={"visibility": "team", "owner_team_id": str(_TEAM_ID)})
    assert resp.status_code == 200
    validate.assert_awaited_once()


def test_update_mcp_server_missing_before_update(client: TestClient) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=None)),
        patch.object(mcp_mod, "update_mcp_server", AsyncMock()),
    ):
        resp = client.patch(f"{SERVERS}/{_SERVER_ID}", json={"name": "x"})
    assert resp.status_code == 404


def test_update_mcp_server_missing_after_update(client: TestClient) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server())),
        patch.object(mcp_mod, "update_mcp_server", AsyncMock(return_value=None)),
    ):
        resp = client.patch(f"{SERVERS}/{_SERVER_ID}", json={"name": "x"})
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "status"), _ERRORS_WITH_INTEGRITY)
def test_update_mcp_server_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server())),
        patch.object(mcp_mod, "update_mcp_server", AsyncMock(side_effect=exc)),
    ):
        resp = client.patch(f"{SERVERS}/{_SERVER_ID}", json={"name": "x"})
    assert resp.status_code == status


def test_delete_mcp_server_success(client: TestClient) -> None:
    with patch.object(mcp_mod, "soft_delete_mcp_server", AsyncMock(return_value=True)):
        resp = client.delete(f"{SERVERS}/{_SERVER_ID}")
    assert resp.status_code == 204


def test_delete_mcp_server_not_found(client: TestClient) -> None:
    with patch.object(mcp_mod, "soft_delete_mcp_server", AsyncMock(return_value=False)):
        resp = client.delete(f"{SERVERS}/{_SERVER_ID}")
    assert resp.status_code == 404


@pytest.mark.parametrize(("exc", "status"), _ERRORS)
def test_delete_mcp_server_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with patch.object(mcp_mod, "soft_delete_mcp_server", AsyncMock(side_effect=exc)):
        resp = client.delete(f"{SERVERS}/{_SERVER_ID}")
    assert resp.status_code == status


def _observation(**over: Any) -> ObservationResult:
    return ObservationResult(
        tools_hash=over.get("tools_hash", "a" * 64),
        approval_status=over.get("approval_status", APPROVAL_STATUS_APPROVED),
        drifted=over.get("drifted", False),
        newly_drifted=over.get("newly_drifted", False),
        previous_tools_hash=over.get("previous_tools_hash"),
    )


def test_observe_tools_success(client: TestClient) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server())),
        patch.object(mcp_mod, "apply_tool_observation", AsyncMock(return_value=_observation(drifted=True))),
    ):
        resp = client.post(f"{SERVERS}/{_SERVER_ID}/observe-tools", json={"tools": [{"name": "read"}]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["drift_detected"] is True
    assert body["server_id"] == str(_SERVER_ID)


def test_observe_tools_server_missing(client: TestClient) -> None:
    with patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=None)):
        resp = client.post(f"{SERVERS}/{_SERVER_ID}/observe-tools", json={"tools": []})
    assert resp.status_code == 404


def test_observe_tools_malformed_tools(client: TestClient) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server())),
        patch.object(mcp_mod, "apply_tool_observation", AsyncMock(side_effect=ToolPinningError("bad tools"))),
    ):
        resp = client.post(f"{SERVERS}/{_SERVER_ID}/observe-tools", json={"tools": [{"x": 1}]})
    assert resp.status_code == 422


def test_observe_tools_rejects_oversized_tool_list(client: TestClient) -> None:
    # The observation set is stored verbatim, so it is capped: one request cannot
    # persist an unbounded document. Validation rejects it before the route runs.
    tools = [{"name": f"tool-{i}"} for i in range(mcp_mod._MAX_OBSERVED_TOOLS + 1)]
    resp = client.post(f"{SERVERS}/{_SERVER_ID}/observe-tools", json={"tools": tools})
    assert resp.status_code == 422


@pytest.mark.parametrize(("exc", "status"), _ERRORS)
def test_observe_tools_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server())),
        patch.object(mcp_mod, "apply_tool_observation", AsyncMock(side_effect=exc)),
    ):
        resp = client.post(f"{SERVERS}/{_SERVER_ID}/observe-tools", json={"tools": []})
    assert resp.status_code == status


def test_approve_tools_success(client: TestClient) -> None:
    observed = _server(tools_json=[{"name": "read"}], tools_hash="a" * 64)
    approved = _server(
        tools_json=[{"name": "read"}],
        tools_hash="a" * 64,
        approval_status=APPROVAL_STATUS_APPROVED,
        approved_hash="a" * 64,
    )
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=observed)),
        patch.object(mcp_mod, "compute_tools_hash", MagicMock(return_value="a" * 64)),
        patch.object(mcp_mod, "update_mcp_server", AsyncMock(return_value=approved)),
    ):
        resp = client.post(f"{SERVERS}/{_SERVER_ID}/approve-tools")
    assert resp.status_code == 200
    assert resp.json()["approval_status"] == APPROVAL_STATUS_APPROVED


def test_approve_tools_server_missing(client: TestClient) -> None:
    with patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=None)):
        resp = client.post(f"{SERVERS}/{_SERVER_ID}/approve-tools")
    assert resp.status_code == 404


def test_approve_tools_no_observed_tools(client: TestClient) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server(tools_json=[]))),
        patch.object(mcp_mod, "update_mcp_server", AsyncMock()),
    ):
        resp = client.post(f"{SERVERS}/{_SERVER_ID}/approve-tools")
    assert resp.status_code == 422


def test_approve_tools_missing_after_update(client: TestClient) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server(tools_json=[{"name": "read"}]))),
        patch.object(mcp_mod, "compute_tools_hash", MagicMock(return_value="a" * 64)),
        patch.object(mcp_mod, "update_mcp_server", AsyncMock(return_value=None)),
    ):
        resp = client.post(f"{SERVERS}/{_SERVER_ID}/approve-tools")
    assert resp.status_code == 404


def test_approve_tools_malformed_tools(client: TestClient) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server(tools_json=[{"name": "read"}]))),
        patch.object(mcp_mod, "compute_tools_hash", MagicMock(side_effect=ToolPinningError("bad"))),
        patch.object(mcp_mod, "update_mcp_server", AsyncMock()),
    ):
        resp = client.post(f"{SERVERS}/{_SERVER_ID}/approve-tools")
    assert resp.status_code == 422


@pytest.mark.parametrize(("exc", "status"), _ERRORS)
def test_approve_tools_error_arms(client: TestClient, exc: Exception, status: int) -> None:
    with (
        patch.object(mcp_mod, "get_mcp_server", AsyncMock(return_value=_server(tools_json=[{"name": "read"}]))),
        patch.object(mcp_mod, "compute_tools_hash", MagicMock(return_value="a" * 64)),
        patch.object(mcp_mod, "update_mcp_server", AsyncMock(side_effect=exc)),
    ):
        resp = client.post(f"{SERVERS}/{_SERVER_ID}/approve-tools")
    assert resp.status_code == status


def test_patched_principal_is_authenticated() -> None:
    """Guard: the fixture's principal is a real admin tenant (sanity check)."""
    principal = _principal()
    assert isinstance(principal, AuthenticatedPrincipal)
    assert principal.org_role == "admin"
