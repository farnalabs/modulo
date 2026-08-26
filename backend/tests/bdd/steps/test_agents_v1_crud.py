"""BDD step definitions: Agent CRUD v1 — /api/v1/agents endpoints."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import ORG_ID

scenarios("../features/agents/crud.feature")

_AGENT_ID: uuid.UUID | None = None
_AGENT_BODY: dict = {
    "name": "code-review",
    "description": "Reviews pull requests",
    "input_schema_id": str(uuid.uuid4()),
    "input_schema_version": "1.0",
    "output_schema_id": str(uuid.uuid4()),
    "output_schema_version": "1.0",
    "prompt_template": "You are a code reviewer.",
    "model_backend_id": str(uuid.uuid4()),
    "required_environment_capabilities": [],
    "template_id": None,
    "agent_command": None,
    "agent_commands": None,
}


def _make_mock_agent(name: str = "test") -> MagicMock:
    a = MagicMock()
    a.id = uuid.uuid4()
    a.organisation_id = ORG_ID
    a.name = name
    a.description = "Test agent description"
    a.is_executable = True
    a.input_schema_id = uuid.uuid4()
    a.input_schema_version = "1.0"
    a.output_schema_id = uuid.uuid4()
    a.output_schema_version = "1.0"
    a.prompt_template = "Hello"
    a.model_backend_id = uuid.uuid4()
    a.connector_type_refs = []
    a.evals = []
    a.retry_policy = {}
    a.token_budget = None
    a.max_input_length = None
    a.library_id = None
    a.required_environment_capabilities = []
    a.template_id = None
    a.agent_command = None
    a.agent_commands = None
    a.prompt_always_visible = False
    a.account_id = uuid.uuid4()
    a.prompt_version_history = []
    a.created_at = datetime.now(UTC)
    a.updated_at = datetime.now(UTC)
    return a


@given(parsers.parse('an agent exists with name "{name}"'))
def _agent_exists(client, request, name: str) -> None:
    global _AGENT_ID
    _AGENT_ID = uuid.uuid4()


@given("a non-existent agent ID")
def _non_existent_agent(request) -> None:
    global _AGENT_ID
    _AGENT_ID = uuid.uuid4()
    request.node._nonexistent = True


@when("I GET /api/v1/agents")
def _get_agents(client, request) -> None:
    page_result = MagicMock(items=[_make_mock_agent()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.agents.list_agents", return_value=page_result),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get("/api/v1/agents")
    request.node._resp = resp


@when(parsers.parse('I create agent "{name}" with description "{desc}"'))
def _create_agent(client, request, name: str, desc: str) -> None:
    body = {**_AGENT_BODY, "name": name, "description": desc}
    mock_agent = _make_mock_agent(name=name)
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=mock_agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    request.node._resp = resp


@when(parsers.parse('I create generic agent "{name}" without a description'))
def _create_agent_no_desc(client, request, name: str) -> None:
    body = {**_AGENT_BODY, "name": name, "description": None}
    resp = client.post("/api/v1/agents", json=body)
    request.node._resp = resp


@when("I create a library agent without a description")
def _create_library_agent(client, request) -> None:
    body = {**_AGENT_BODY, "description": None, "library_id": str(uuid.uuid4())}
    mock_agent = _make_mock_agent()
    mock_agent.library_id = uuid.UUID(body["library_id"])
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=mock_agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    request.node._resp = resp


@when("I GET the agent by ID")
def _get_agent(client, request) -> None:
    agent_id = _AGENT_ID or uuid.uuid4()
    nonexistent = getattr(request.node, "_nonexistent", False)
    return_val = None if nonexistent else _make_mock_agent(name="test-agent")
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=return_val),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/agents/{agent_id}")
    request.node._resp = resp


@when(parsers.parse('I update the agent name to "{name}"'))
def _update_agent(client, request, name: str) -> None:
    agent_id = _AGENT_ID or uuid.uuid4()
    mock_agent = _make_mock_agent(name=name)
    mock_agent.id = agent_id
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=mock_agent),
        patch("modulo.api.routes.agents.update_agent", return_value=mock_agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"name": name, "required_environment_capabilities": [], "template_id": None},
        )
    request.node._resp = resp


@when("I delete the agent")
def _delete_agent(client, request) -> None:
    agent_id = _AGENT_ID or uuid.uuid4()
    nonexistent = getattr(request.node, "_nonexistent", False)
    with (
        patch("modulo.api.routes.agents.delete_agent", return_value=not nonexistent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/agents/{agent_id}")
    request.node._resp = resp


@then("the response contains a list of agents")
def _check_list(request) -> None:
    data = request.node._resp.json()
    assert "items" in data
    assert "total" in data


@then(parsers.parse('the agent name is "{name}"'))
def _check_name(name: str, request) -> None:
    data = request.node._resp.json()
    assert data.get("name") == name


@then('the error mentions "description"')
def _check_description_error(request) -> None:
    detail = request.node._resp.json().get("detail", "")
    assert "description" in detail.lower(), f"Expected description in error, got: {detail}"
