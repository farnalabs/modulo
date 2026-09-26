"""Unit tests for /api/v1/agents endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_AGENT_ID = uuid.uuid4()
_SCHEMA_ID = uuid.uuid4()
_BACKEND_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture(autouse=True)
def _stub_get_agent() -> Generator[None, None, None]:
    """The IDOR ownership check reads the agent via ``get_agent`` before the
    write CRUD, but the write-path cases only mock ``update_agent`` /
    ``delete_agent``. Supply a same-org agent so the ownership check passes for
    the legitimate (same-org) principal these tests use."""
    with patch("modulo.api.routes.agents.get_agent", return_value=_make_agent()):
        yield


def _make_agent() -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID
    a.organisation_id = _ORG_ID
    a.name = "Test Agent"
    a.description = "A test agent for unit tests"
    a.input_schema_id = _SCHEMA_ID
    a.input_schema_version = "1.0"
    a.output_schema_id = _SCHEMA_ID
    a.output_schema_version = "1.0"
    a.prompt_template = "Hello"
    a.model_backend_id = _BACKEND_ID
    a.connector_type_refs = []
    a.evals = []
    a.retry_policy = {}
    a.token_budget = None
    a.library_id = None
    a.template_id = None
    a.account_id = uuid.uuid4()
    a.required_environment_capabilities = []
    a.template_id = None
    a.schema_profile = None
    a.created_at = _NOW
    a.updated_at = _NOW
    return a


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


_PATCH_REQUIRED = {"required_environment_capabilities": [], "template_id": None}

_AGENT_BODY = {
    "name": "Test Agent",
    "description": "A test agent for unit tests",
    "input_schema_id": str(_SCHEMA_ID),
    "input_schema_version": "1.0",
    "output_schema_id": str(_SCHEMA_ID),
    "output_schema_version": "1.0",
    "prompt_template": "Hello",
    "model_backend_id": str(_BACKEND_ID),
    "required_environment_capabilities": [],
    "template_id": None,
}

_UPDATE_BODY: dict[str, Any] = {
    "required_environment_capabilities": [],
    "template_id": None,
}

_AGENT_PATCH_PREFIX = "modulo.api.routes.agents."


def _crud_cases() -> list[dict[str, object]]:
    page_result = MagicMock(items=[_make_agent()], total=1, page=1, page_size=20)
    agent = _make_agent()
    updated_agent = _make_agent()
    updated_agent.name = "Updated"
    return [
        {
            "id": "list",
            "method": "GET",
            "url": "/api/v1/agents",
            "body": None,
            "patches": [("list_agents", page_result)],
            "expected_status": 200,
            "check": lambda resp: resp.json()["total"] == 1,
        },
        {
            "id": "create",
            "method": "POST",
            "url": "/api/v1/agents",
            "body": _AGENT_BODY,
            "patches": [("create_agent", _make_agent())],
            "expected_status": 201,
            "check": lambda resp: resp.json()["name"] == "Test Agent",
        },
        {
            "id": "get",
            "method": "GET",
            "url": f"/api/v1/agents/{_AGENT_ID}",
            "body": None,
            "patches": [("get_agent", _make_agent())],
            "expected_status": 200,
        },
        {
            "id": "get_not_found",
            "method": "GET",
            "url": f"/api/v1/agents/{uuid.uuid4()}",
            "body": None,
            "patches": [("get_agent", None)],
            "expected_status": 404,
        },
        {
            "id": "update",
            "method": "PATCH",
            "url": f"/api/v1/agents/{_AGENT_ID}",
            "body": {**_UPDATE_BODY, "name": "Updated"},
            "patches": [("get_agent", agent), ("update_agent", updated_agent)],
            "expected_status": 200,
            "check": lambda resp: resp.json()["name"] == "Updated",
        },
        {
            "id": "update_not_found",
            "method": "PATCH",
            "url": f"/api/v1/agents/{uuid.uuid4()}",
            "body": {**_UPDATE_BODY, "name": "x"},
            "patches": [("get_agent", None), ("update_agent", None)],
            "expected_status": 404,
        },
        {
            "id": "delete",
            "method": "DELETE",
            "url": f"/api/v1/agents/{_AGENT_ID}",
            "body": None,
            "patches": [("delete_agent", True)],
            "expected_status": 204,
        },
        {
            "id": "delete_not_found",
            "method": "DELETE",
            "url": f"/api/v1/agents/{uuid.uuid4()}",
            "body": None,
            "patches": [("delete_agent", False)],
            "expected_status": 404,
        },
    ]


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("case", _crud_cases(), ids=lambda c: c["id"])
def test_crud(client: TestClient, case: dict[str, object]) -> None:
    method = case["method"]
    url = case["url"]
    body = case.get("body")
    expected_status = case["expected_status"]
    check = case.get("check")

    patchers = []
    for func_name, ret in case["patches"]:
        patchers.append(patch(f"{_AGENT_PATCH_PREFIX}{func_name}", return_value=ret))
    patchers.append(patch(f"{_AGENT_PATCH_PREFIX}set_rls_org"))

    for p in patchers:
        p.start()

    try:
        if method == "GET":
            resp = client.get(url)
        elif method == "POST":
            resp = client.post(url, json=body or {})
        elif method == "PATCH":
            resp = client.patch(url, json=body or {})
        elif method == "DELETE":
            resp = client.delete(url)
        elif method == "PUT":
            resp = client.put(url, json=body or {})
        else:
            raise ValueError(f"Unsupported method: {method}")

        assert resp.status_code == expected_status, f"Expected {expected_status}, got {resp.status_code}: {resp.text}"
        if check:
            assert check(resp)
    finally:
        for p in patchers:
            p.stop()


def test_list_agents_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/agents")
    assert resp.status_code in (401, 403)


def test_create_agent_with_max_input_length(client: TestClient) -> None:
    body = {**_AGENT_BODY, "max_input_length": 5000}
    agent = _make_agent()
    agent.max_input_length = 5000
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 201
    assert resp.json()["max_input_length"] == 5000


def test_create_agent_without_max_input_length_defaults_to_null(client: TestClient) -> None:
    agent = _make_agent()
    agent.max_input_length = None
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=_AGENT_BODY)
    assert resp.status_code == 201
    assert resp.json()["max_input_length"] is None


def test_update_agent_max_input_length(client: TestClient) -> None:
    agent = _make_agent()
    agent.max_input_length = 10000
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.update_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={**_UPDATE_BODY, "max_input_length": 10000})
    assert resp.status_code == 200
    assert resp.json()["max_input_length"] == 10000


def test_update_agent_reassigns_input_output_schemas(client: TestClient) -> None:
    """PATCH reassigns input/output schema references after create.

    ``AgentUpdate`` exposes ``input_schema_id``/``output_schema_id`` (plus the
    matching version fields), so a PATCH can (re)bind an agent to a different
    schema — closing the product-map "schemas are fixed after create (no PATCH
    support)" gap. An omitted version resolves to the org's ``latest``
    placeholder version exactly like create_agent_endpoint does.
    """
    agent = _make_agent()
    new_input = uuid.uuid4()
    new_output = uuid.uuid4()
    with (
        patch(f"{_AGENT_PATCH_PREFIX}get_agent", return_value=agent),
        patch(f"{_AGENT_PATCH_PREFIX}update_agent", return_value=agent) as mock_update,
        patch(f"{_AGENT_PATCH_PREFIX}set_rls_org"),
    ):
        resp = client.patch(
            f"/api/v1/agents/{_AGENT_ID}",
            json={
                **_UPDATE_BODY,
                "input_schema_id": str(new_input),
                "output_schema_id": str(new_output),
            },
        )
    assert resp.status_code == 200, resp.text
    updates = mock_update.call_args[0][2]
    assert updates["input_schema_id"] == new_input
    assert updates["input_schema_version"] == "latest"
    assert updates["output_schema_id"] == new_output
    assert updates["output_schema_version"] == "latest"


def test_update_agent_detaches_output_schema(client: TestClient) -> None:
    """PATCH with an explicit null detaches a schema binding (id + version).

    Closing the same product-map gap: ``None`` clears both the id and its
    version so the (id, version, org) FK stays consistent, and a stray
    version-only entry is dropped instead of being silently applied.
    """
    agent = _make_agent()
    with (
        patch(f"{_AGENT_PATCH_PREFIX}get_agent", return_value=agent),
        patch(f"{_AGENT_PATCH_PREFIX}update_agent", return_value=agent) as mock_update,
        patch(f"{_AGENT_PATCH_PREFIX}set_rls_org"),
    ):
        resp = client.patch(
            f"/api/v1/agents/{_AGENT_ID}",
            json={
                **_UPDATE_BODY,
                "input_schema_id": None,
                "input_schema_version": "should-be-dropped",
            },
        )
    assert resp.status_code == 200, resp.text
    updates = mock_update.call_args[0][2]
    assert updates["input_schema_id"] is None
    assert updates["input_schema_version"] is None


def test_update_agent_rejects_unresolvable_schema_reference(client: TestClient) -> None:
    """A schema id that resolves to no org-owned version is refused with 422.

    Exercises the ``_resolve_schema_binding`` refusal path: the (id, version,
    org) lookup returns nothing, so the PATCH is rejected before the write
    rather than surfacing as a 409 FK collision.
    """
    agent = _make_agent()
    mock_session = _make_mock_session()
    empty_result = MagicMock()
    empty_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=empty_result)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_session
    with (
        patch(f"{_AGENT_PATCH_PREFIX}get_agent", return_value=agent),
        patch(f"{_AGENT_PATCH_PREFIX}update_agent", return_value=agent) as mock_update,
        patch(f"{_AGENT_PATCH_PREFIX}set_rls_org"),
    ):
        resp = client.patch(
            f"/api/v1/agents/{_AGENT_ID}",
            json={**_UPDATE_BODY, "input_schema_id": str(uuid.uuid4())},
        )
    assert resp.status_code == 422, resp.text
    assert "Referenced schema version not found" in resp.json()["detail"]
    mock_update.assert_not_called()


def test_update_agent_patch_returns_200_and_reflects_update(client: TestClient) -> None:
    """Regression: PATCH with a valid body must return 200, not 500.

    The CRUD ``update_agent`` flush expires the server-generated ``updated_at``
    column (``onupdate=func.current_timestamp()``). Building the response
    AFTER the ``session.begin()`` block commits — on the ``autobegin=False``
    DI session — used to raise ``InvalidRequestError: Autobegin is disabled``
    when ``AgentResponse.model_validate`` re-loaded the expired attribute,
    turning a successful committed update into a 500.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from modulo.db.models.agent import Agent

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    agent_id = uuid.uuid4()
    agent = Agent(
        id=agent_id,
        organisation_id=_ORG_ID,
        name="Test Agent",
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        is_executable=True,
        input_schema_id=_SCHEMA_ID,
        input_schema_version="1.0",
        output_schema_id=_SCHEMA_ID,
        output_schema_version="1.0",
        prompt_template="Hello",
        model_backend_id=_BACKEND_ID,
        description="A test agent for unit tests",
        connector_type_refs=[],
        evals=[],
        retry_policy={},
        token_budget=None,
        max_input_length=None,
        library_id=None,
        prompt_always_visible=False,
        required_environment_capabilities=[],
        template_id=None,
        agent_commands=None,
        created_at=_NOW,
        updated_at=_NOW,
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with engine.begin() as conn:
            await conn.run_sync(Agent.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with factory() as session:
            async with session.begin():
                session.add(agent)
                await session.flush()
            yield session
        await engine.dispose()

    client.app.dependency_overrides[get_db_session] = override_session

    with patch("modulo.api.routes.agents.set_rls_org"):
        resp = client.patch(f"/api/v1/agents/{agent_id}", json={**_UPDATE_BODY, "name": "Updated"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Updated"


# ── Generic agent criteria validation tests ──────────────────────────────


def test_create_generic_agent_missing_description_returns_422(client: TestClient) -> None:
    body = {**_AGENT_BODY, "description": None}
    resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 422
    assert "description" in resp.json()["detail"].lower()


def test_create_generic_agent_with_library_id_skips_description_check(client: TestClient) -> None:
    body = {**_AGENT_BODY, "description": None, "library_id": str(uuid.uuid4())}
    agent = _make_agent()
    agent.library_id = uuid.UUID(body["library_id"])
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 201


def test_create_non_executable_agent_missing_description_returns_422(client: TestClient) -> None:
    body = {**_AGENT_BODY, "description": None, "is_executable": False}
    resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 422
    assert "description" in resp.json()["detail"].lower()


def test_create_generic_agent_with_description_succeeds(client: TestClient) -> None:
    body = {**_AGENT_BODY, "description": "Valid generic agent"}
    agent = _make_agent()
    agent.description = "Valid generic agent"
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 201
    assert resp.json()["description"] == "Valid generic agent"


def test_update_generic_agent_clearing_description_returns_422(client: TestClient) -> None:
    agent = _make_agent()
    agent.description = "Current description"
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={**_UPDATE_BODY, "description": ""})
    assert resp.status_code == 422
    assert "description" in resp.json()["detail"].lower()


def test_update_library_agent_clearing_description_succeeds(client: TestClient) -> None:
    agent = _make_agent()
    agent.library_id = uuid.uuid4()
    agent.description = "library-sourced"
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.update_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={**_UPDATE_BODY, "description": ""})
    assert resp.status_code == 200


def test_update_agent_making_non_executable_without_description_returns_422(client: TestClient) -> None:
    agent = _make_agent()
    agent.description = ""
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(
            f"/api/v1/agents/{_AGENT_ID}",
            json={**_UPDATE_BODY, "is_executable": False},
        )
    assert resp.status_code == 422
    assert "description" in resp.json()["detail"].lower()


def _foreign_org_agent() -> MagicMock:
    """An agent owned by a different organisation than the test principal."""
    foreign = MagicMock()
    foreign.id = _AGENT_ID
    foreign.organisation_id = uuid.uuid4()
    return foreign


def test_update_agent_foreign_org_returns_404(client: TestClient) -> None:
    """IDOR regression: a foreign-org principal must not update an agent it
    does not own. The ownership check must raise 404 before any write."""
    with patch("modulo.api.routes.agents.get_agent", return_value=_foreign_org_agent()):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json=_UPDATE_BODY)
    assert resp.status_code == 404


def test_delete_agent_foreign_org_returns_404(client: TestClient) -> None:
    """IDOR regression: a foreign-org principal must not delete an agent it
    does not own."""
    with patch("modulo.api.routes.agents.get_agent", return_value=_foreign_org_agent()):
        resp = client.delete(f"/api/v1/agents/{_AGENT_ID}")
    assert resp.status_code == 404


def test_apply_prompt_foreign_org_returns_404(client: TestClient) -> None:
    """IDOR regression: applying an optimized prompt to a foreign-org agent
    must be denied with 404."""
    with patch("modulo.api.routes.agents.get_agent", return_value=_foreign_org_agent()):
        resp = client.post(
            f"/api/v1/agents/{_AGENT_ID}/prompts/v1/apply",
            json={"suggested_prompt": "x"},
        )
    assert resp.status_code == 404


def test_rollback_prompt_foreign_org_returns_404(client: TestClient) -> None:
    """IDOR regression: rolling back a foreign-org agent's prompt must be
    denied with 404."""
    with patch("modulo.api.routes.agents.get_agent", return_value=_foreign_org_agent()):
        resp = client.put(
            f"/api/v1/agents/{_AGENT_ID}/prompts/rollback/v1",
            json={},
        )
    assert resp.status_code == 404


# ── FAR-900: schema_profile persists through agent creation ────────────────


def test_create_agent_schema_profile_persists(client: TestClient) -> None:
    """FIX 1 regression: POST with schema_profile='provider-strict' must
    pass it through to the CRUD layer and persist it.  The old code accepted
    schema_profile on AgentCreate but never forwarded it to create_agent(),
    so the DB row always had schema_profile=None."""
    agent = _make_agent()
    agent.schema_profile = "provider-strict"
    body = {**_AGENT_BODY, "schema_profile": "provider-strict"}
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=agent) as mock_create,
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 201
    assert resp.json()["schema_profile"] == "provider-strict"
    # Verify the CRUD was called with schema_profile
    _, kwargs = mock_create.call_args
    assert kwargs.get("schema_profile") == "provider-strict"


def test_create_agent_schema_profile_none_by_default(client: TestClient) -> None:
    """Absent schema_profile on POST defaults to None (verbatim)."""
    agent = _make_agent()
    agent.schema_profile = None
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=agent) as mock_create,
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=_AGENT_BODY)
    assert resp.status_code == 201
    assert resp.json()["schema_profile"] is None
    _, kwargs = mock_create.call_args
    assert kwargs.get("schema_profile") is None


# ── FAR-220: save-time git-content pin gate for Agent rows ─────────────────


def test_create_agent_with_unpinned_git_prompt_is_rejected(client: TestClient) -> None:
    """Prove-the-fix: a movable ``git+`` ref in prompt_template used to persist
    and only fail at render time; the save-time gate must 422 it instead."""
    body = {
        **_AGENT_BODY,
        "prompt_template": "git+https://github.com/example/repo@main#prompts/x.md",
    }
    with patch("modulo.api.routes.agents.create_agent") as mock_create:
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 422
    assert "not pinned" in resp.json()["detail"]
    assert not mock_create.called


def test_create_agent_with_malformed_git_prompt_is_rejected(client: TestClient) -> None:
    body = {**_AGENT_BODY, "prompt_template": "git+https://github.com/example/repo"}
    with patch("modulo.api.routes.agents.create_agent") as mock_create:
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 422
    assert "invalid git content ref" in resp.json()["detail"]
    assert not mock_create.called


def test_create_agent_with_pinned_git_prompt_is_accepted(client: TestClient) -> None:
    body = {
        **_AGENT_BODY,
        "prompt_template": "git+https://github.com/example/repo@" + "a" * 40 + "#prompts/x.md",
        "agent_commands": ["git+https://github.com/example/other@" + "b" * 40 + "#cmd.sh"],
    }
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=_make_agent()),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 201


def test_create_agent_with_git_ref_among_several_commands_is_rejected(client: TestClient) -> None:
    body = {
        **_AGENT_BODY,
        "agent_commands": ["echo hi", "git+https://github.com/example/repo@" + "a" * 40 + "#cmd.sh"],
    }
    with patch("modulo.api.routes.agents.create_agent") as mock_create:
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 422
    assert "whole field" in resp.json()["detail"]
    assert not mock_create.called


def test_update_agent_with_unpinned_git_prompt_is_rejected(client: TestClient) -> None:
    with patch("modulo.api.routes.agents.update_agent") as mock_update:
        resp = client.patch(
            f"/api/v1/agents/{_AGENT_ID}",
            json={**_UPDATE_BODY, "prompt_template": "git+https://github.com/example/repo@main#prompts/x.md"},
        )
    assert resp.status_code == 422
    assert "not pinned" in resp.json()["detail"]
    assert not mock_update.called


def test_update_agent_unrelated_field_with_stale_stored_prompt_is_not_retroactively_blocked(client: TestClient) -> None:
    """A PATCH of a field OTHER than prompt/commands must not validate the OLD
    stored prompt (exclude_unset semantics) — retroactive breakage of unrelated
    edits is out of scope; run-time rendering stays fail-closed regardless."""
    with (
        patch("modulo.api.routes.agents.update_agent", return_value=_make_agent()),
        patch("modulo.api.routes.agents.set_rls_org"),
        patch("modulo.api.routes.agents._validate_agent_git_content") as mock_gate,
    ):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={**_UPDATE_BODY, "name": "Renamed"})
    assert resp.status_code == 200
    assert not mock_gate.called


def test_apply_optimized_prompt_with_unpinned_git_prompt_is_rejected(client: TestClient) -> None:
    resp = client.post(
        f"/api/v1/agents/{_AGENT_ID}/prompts/v1/apply",
        json={
            "suggested_prompt": "git+https://github.com/example/repo@main#prompts/x.md",
            "eval_result_ids": [str(uuid.uuid4())],
        },
    )
    assert resp.status_code == 422
    assert "not pinned" in resp.json()["detail"]
