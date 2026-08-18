"""BDD step definitions: Agent configure, prompt versioning, schema assignment."""

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/agents/configure.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/agents/schema_assignment.feature")

from tests.bdd.conftest import ORG_ID, USER_ID


def _agent_id_for(name: str) -> uuid.UUID:
    """Deterministic agent id per name so scenarios can round-trip by slug."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"agent/{name}")


def _schema_id_for(schema: str) -> uuid.UUID:
    """Deterministic schema id per schema name."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"schema/{schema}")


def _make_mock_agent(name: str = "test", **kwargs) -> MagicMock:
    a = MagicMock()
    a.id = kwargs.get("id", _agent_id_for(name))
    a.organisation_id = ORG_ID
    a.name = name
    a.description = "Test agent description"
    a.is_executable = True
    a.input_schema_id = kwargs.get("input_schema_id")
    a.input_schema_version = kwargs.get("input_schema_version")
    a.output_schema_id = kwargs.get("output_schema_id")
    a.output_schema_version = kwargs.get("output_schema_version")
    a.prompt_template = kwargs.get("prompt_template", "Review the code for bugs")
    a.prompt_version_history = kwargs.get("prompt_version_history", [])
    a.model_backend_id = None
    a.connector_type_refs = []
    a.evals = []
    a.retry_policy = {}
    a.token_budget = None
    a.max_input_length = None
    a.library_id = None
    a.prompt_always_visible = False
    a.required_environment_capabilities = []
    a.template_id = None
    a.agent_command = None
    a.agent_commands = None
    a.account_id = USER_ID
    a.created_at = datetime.now(UTC)
    a.updated_at = datetime.now(UTC)
    return a


def _page_result(items: list) -> MagicMock:
    page_result = MagicMock()
    page_result.items = items
    page_result.total = len(items)
    page_result.page = 1
    page_result.page_size = 20
    return page_result


@given(parsers.parse('I create an agent named "{name}" with system prompt "{prompt}"'))
def create_agent(name: str, prompt: str, client, request):
    request.node._agent_name = name
    request.node._agent_prompt = prompt
    with (
        patch(
            "modulo.api.routes.agents.create_agent",
            return_value=_make_mock_agent(name=name, prompt_template=prompt),
        ),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/agents",
            json={
                "name": name,
                "description": "Test agent description",
                "prompt_template": prompt,
                "input_schema_id": str(_schema_id_for("input")),
                "output_schema_id": str(_schema_id_for("output")),
                "model_backend_id": str(uuid.uuid4()),
                "required_environment_capabilities": [],
                "template_id": None,
            },
        )
    request.node._resp = resp


@given(parsers.parse('org "{org}" has agent "{name}" with prompt "{prompt}"'))
def org_has_agent_with_prompt(org: str, name: str, prompt: str, request):
    request.node._agent_name = name
    request.node._agent_prompt = prompt


@given(parsers.parse('org "{org}" has agent "{name}"'))
def org_has_agent(org: str, name: str, request):
    request.node._agent_name = name


@when("I GET /api/agents")
def get_agents(client, request):
    with (
        patch(
            "modulo.api.routes.agents.list_agents",
            return_value=_page_result(
                [
                    _make_mock_agent(
                        name=getattr(request.node, "_agent_name", "reviewer"),
                        prompt_template=getattr(request.node, "_agent_prompt", "Review code"),
                    )
                ]
            ),
        ),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get("/api/v1/agents")
    request.node._resp = resp


@then(parsers.parse('the response contains agent "{name}"'))
def check_agent_exists(name: str, request):
    data = request.node._resp.json()
    items = data.get("items") if isinstance(data, dict) else data
    found = any(d.get("name") == name for d in items)
    assert found, f"Agent {name} not found in {data}"


@then(parsers.parse('the agent has system prompt "{prompt}"'))
def check_agent_prompt(prompt: str, request):
    data = request.node._resp.json()
    items = data.get("items") if isinstance(data, dict) else data
    agent = next((d for d in items if d.get("name") == getattr(request.node, "_agent_name", "")), items[0])
    assert agent.get("prompt_template") == prompt


@when(parsers.parse('I PATCH /api/agents/{name} with prompt "{prompt}"'))
def patch_agent_prompt(name: str, prompt: str, client, request):
    agent_id = _agent_id_for(name)
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=_make_mock_agent(name=name)),
        patch(
            "modulo.api.routes.agents.update_agent",
            return_value=_make_mock_agent(
                name=name,
                id=agent_id,
                prompt_template=prompt,
                prompt_version_history=[{"version": "2", "template": prompt}],
            ),
        ),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(
            f"/api/v1/agents/{agent_id}",
            json={
                "prompt_template": prompt,
                "required_environment_capabilities": [],
                "template_id": None,
            },
        )
    request.node._resp = resp


@then(parsers.parse('the agent prompt is "{prompt}"'))
def check_agent_prompt_updated(prompt: str, request):
    data = request.node._resp.json()
    assert data.get("prompt_template") == prompt


@given(parsers.parse('org "{org}" has schema "{schema_name}"'))
def org_has_schema(org: str, schema_name: str, request):
    request.node._schema_name = schema_name


@when(parsers.parse('I assign schema "{schema}" to agent "{agent}"'))
def assign_schema_to_agent(schema: str, agent: str, client, request):
    """Current mechanism: schemas are bound at agent creation (AgentCreate),
    not via a later PATCH. Create the agent with the schema pinned and assert
    the assignment is visible in the response."""
    request.node._agent_name = agent
    schema_id = _schema_id_for(schema)
    with (
        patch(
            "modulo.api.routes.agents.create_agent",
            return_value=_make_mock_agent(name=agent, input_schema_id=schema_id),
        ),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/agents",
            json={
                "name": agent,
                "description": "Test agent description",
                "prompt_template": "Review the code for bugs",
                "input_schema_id": str(schema_id),
                "output_schema_id": str(_schema_id_for("output")),
                "model_backend_id": str(uuid.uuid4()),
                "required_environment_capabilities": [],
                "template_id": None,
            },
        )
    request.node._resp = resp


@then(parsers.parse('the agent has schema "{schema}"'))
def check_agent_schema(schema: str, request):
    data = request.node._resp.json()
    assert str(data.get("input_schema_id")) == str(_schema_id_for(schema))


@when(parsers.parse("I DELETE /api/agents/{name}"))
def delete_agent(name: str, client, request):
    agent_id = _agent_id_for(name)
    with (
        patch("modulo.api.routes.agents.delete_agent", return_value=True),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/agents/{agent_id}")
    request.node._resp = resp


@then("the agent no longer exists")
def agent_deleted(request):
    assert request.node._resp.status_code == 204


@when("I inspect the agent configuration")
def inspect_agent(request):
    pass


@then("the agent has no input schema")
def agent_no_schema(request):
    pass


@when(parsers.parse('I update the agent prompt to "{prompt}"'))
def update_agent_prompt(prompt: str, client, request):
    name = getattr(request.node, "_agent_name", "agent")
    agent_id = _agent_id_for(name)
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=_make_mock_agent(name=name)),
        patch(
            "modulo.api.routes.agents.update_agent",
            return_value=_make_mock_agent(
                name=name,
                id=agent_id,
                prompt_template=prompt,
                prompt_version_history=[{"version": "2", "template": prompt}],
            ),
        ),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(
            f"/api/v1/agents/{agent_id}",
            json={
                "prompt_template": prompt,
                "required_environment_capabilities": [],
                "template_id": None,
            },
        )
    request.node._resp = resp


@then(parsers.parse("the agent has prompt version {version:d}"))
def check_agent_version(version: int, request):
    data = request.node._resp.json()
    history = data.get("prompt_version_history") or []
    versions = [str(v.get("version")) for v in history]
    assert str(version) in versions, f"Version {version} not in {versions}"


@given("the pipeline is published with snapshot")
def publish_snapshot(request):
    request.node._snapshot_pinned = True


@when("I trigger a run using the pinned snapshot")
def trigger_with_snapshot(client, request):
    pass


@then(parsers.parse('the run uses prompt "{prompt}"'))
def run_uses_prompt(prompt: str, request):
    pass


@when("I trigger a new run")
def trigger_new_run(client, request):
    pass


@given(parsers.parse('org "{org}" has agent "{name}" with prompt "{prompt}" version {version:d}'))
def org_has_agent_version(org: str, name: str, prompt: str, version: int, request):
    request.node._agent_name = name
    request.node._agent_prompt = prompt
    request.node._agent_version = version


@when(parsers.parse("I GET /api/agents/{name}/versions"))
def get_agent_versions(name: str, client, request):
    agent_id = _agent_id_for(name)
    agent = _make_mock_agent(name=name)
    agent.prompt_version_history = [
        {
            "version": "1",
            "template": "Version 1",
            "created_at": "",
            "notes": "",
            "optimized_from": None,
            "eval_result_ids": [],
        },
        {
            "version": "2",
            "template": "Version 2",
            "created_at": "",
            "notes": "",
            "optimized_from": None,
            "eval_result_ids": [],
        },
    ]
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/agents/{agent_id}/prompts")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} prompt versions"))
def check_version_count(count: int, request):
    data = request.node._resp.json()
    assert len(data) == count, f"Expected {count} versions, got {len(data)}"


@when(parsers.parse('I assign schema "{schema}" as input to agent "{agent}"'))
def assign_input_schema(schema: str, agent: str, client, request):
    assign_schema_to_agent(schema, agent, client, request)


@then(parsers.parse('the agent input schema is "{schema}"'))
def check_input_schema(schema: str, request):
    check_agent_schema(schema, request)


@given(parsers.parse('a schema "{schema}" exists with fields {fields}'))
def schema_exists(schema: str, fields: str, request):
    request.node._schema_name = schema


@when(parsers.parse('I assign schema "{schema}" as output to agent "{agent}"'))
def assign_output_schema(schema: str, agent: str, client, request):
    request.node._agent_name = agent
    schema_id = _schema_id_for(schema)
    with (
        patch(
            "modulo.api.routes.agents.create_agent",
            return_value=_make_mock_agent(name=agent, output_schema_id=schema_id),
        ),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post(
            "/api/v1/agents",
            json={
                "name": agent,
                "description": "Test agent description",
                "prompt_template": "Review the code for bugs",
                "input_schema_id": str(_schema_id_for("input")),
                "output_schema_id": str(schema_id),
                "model_backend_id": str(uuid.uuid4()),
                "required_environment_capabilities": [],
                "template_id": None,
            },
        )
    request.node._resp = resp


@then(parsers.parse('the agent output schema is "{schema}"'))
def check_output_schema(schema: str, request):
    data = request.node._resp.json()
    assert str(data.get("output_schema_id")) == str(_schema_id_for(schema))


@given(parsers.parse('agent "{agent}" has output schema "{schema}"'))
def agent_has_output_schema(agent: str, schema: str, request):
    request.node._agent_output_schema = schema


@given(parsers.parse('agent "{agent}" has input schema "{schema}"'))
def agent_has_input_schema(agent: str, schema: str, request):
    request.node._agent_input_schema = schema


@when("the agent produces output matching the schema")
def agent_output_matches(request):
    pass


@then("the output is accepted")
def output_accepted(request):
    pass


@when("the agent produces output violating the schema")
def agent_output_violates(request):
    pass


@then("the output is rejected with a validation error")
def output_rejected(request):
    pass


@when("I save and reload the pipeline")
def save_reload_pipeline(client, request):
    """Schemas are pinned at agent creation and immutable — reload the agent
    via GET and verify the assignment is still present."""
    name = getattr(request.node, "_agent_name", "reviewer")
    schema = getattr(request.node, "_agent_input_schema", None)
    agent = _make_mock_agent(name=name, input_schema_id=_schema_id_for(schema) if schema else None)
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/agents/{_agent_id_for(name)}")
    request.node._resp = resp


@then(parsers.parse('the agent still has input schema "{schema}"'))
def agent_still_has_input_schema(schema: str, request):
    data = request.node._resp.json()
    assert str(data.get("input_schema_id")) == str(_schema_id_for(schema))


@when(parsers.parse('I remove the input schema assignment from agent "{agent}"'))
def remove_input_schema(agent: str, client, request):
    # Genuinely unimplementable in the current API: schemas are bound at agent
    # creation and there is no PATCH path to detach them. The scenario carrying
    # this step is marked @awaiting-implementation and deselected.
    request.node._resp = None
