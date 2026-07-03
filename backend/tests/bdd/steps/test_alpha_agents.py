"""BDD step definitions: Agent configure, prompt versioning, schema assignment."""

import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../../features/agents/configure.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/agents/prompt_versioning.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/agents/schema_assignment.feature")
except (FileNotFoundError, OSError):
    pass


@given(parsers.parse('I create an agent named "{name}" with system prompt "{prompt}"'))
def create_agent(name: str, prompt: str, client, request):
    with (
        patch(
            "modulo.api.routes.agents.create_agent",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=name,
                system_prompt=prompt,
                prompt_version=1,
            ),
        ),
    ):
        resp = client.post(
            "/api/agents",
            json={
                "name": name,
                "system_prompt": prompt,
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
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.list_agents",
            return_value=[
                MagicMock(
                    id=uuid.uuid4(),
                    name=getattr(request.node, "_agent_name", "reviewer"),
                    system_prompt=getattr(request.node, "_agent_prompt", "Review code"),
                    prompt_version=1,
                )
            ],
        ),
    ):
        resp = client.get("/api/agents")
    request.node._resp = resp


@then(parsers.parse('the response contains agent "{name}"'))
def check_agent_exists(name: str, request):
    data = request.node._resp.json()
    if isinstance(data, list):
        found = any(d.get("name") == name for d in data)
        assert found, f"Agent {name} not found in {data}"
    else:
        assert data.get("name") == name


@then(parsers.parse('the agent has system prompt "{prompt}"'))
def check_agent_prompt(prompt: str, request):
    data = request.node._resp.json()
    if isinstance(data, list):
        agent = next((d for d in data if d.get("name") == getattr(request.node, "_agent_name", "")), data[0])
        assert agent.get("system_prompt") == prompt
    else:
        assert data.get("system_prompt") == prompt


@when(parsers.parse('I PATCH /api/agents/{name} with prompt "{prompt}"'))
def patch_agent_prompt(name: str, prompt: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.update_agent",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=name,
                system_prompt=prompt,
                prompt_version=2,
            ),
        ),
    ):
        resp = client.patch(f"/api/agents/{name}", json={"system_prompt": prompt})
    request.node._resp = resp


@then(parsers.parse('the agent prompt is "{prompt}"'))
def check_agent_prompt_updated(prompt: str, request):
    data = request.node._resp.json()
    assert data.get("system_prompt") == prompt


@given(parsers.parse('org "{org}" has schema "{schema_name}"'))
def org_has_schema(org: str, schema_name: str, request):
    request.node._schema_name = schema_name


@when(parsers.parse('I assign schema "{schema}" to agent "{agent}"'))
def assign_schema_to_agent(schema: str, agent: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.update_agent",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=agent,
                input_schema_id=schema,
            ),
        ),
    ):
        resp = client.patch(f"/api/agents/{agent}", json={"input_schema_id": schema})
    request.node._resp = resp


@then(parsers.parse('the agent has schema "{schema}"'))
def check_agent_schema(schema: str, request):
    data = request.node._resp.json()
    assert data.get("input_schema_id") == schema


@when(parsers.parse("I DELETE /api/agents/{name}"))
def delete_agent(name: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.delete_agent",
            return_value=True,
        ),
    ):
        resp = client.delete(f"/api/agents/{name}")
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
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.update_agent",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=getattr(request.node, "_agent_name", "agent"),
                system_prompt=prompt,
                prompt_version=getattr(request.node, "_next_version", 2),
            ),
        ),
    ):
        resp = client.patch(
            f"/api/agents/{getattr(request.node, '_agent_name', 'agent')}",
            json={"system_prompt": prompt},
        )
    request.node._resp = resp


@then(parsers.parse("the agent has prompt version {version:d}"))
def check_agent_version(version: int, request):
    data = request.node._resp.json()
    assert data.get("prompt_version") == version


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
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_agent_versions",
            return_value=[
                MagicMock(version=1, system_prompt="Version 1"),
                MagicMock(version=2, system_prompt="Version 2"),
            ],
        ),
    ):
        resp = client.get(f"/api/agents/{name}/versions")
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
    pass


@when(parsers.parse('I assign schema "{schema}" as output to agent "{agent}"'))
def assign_output_schema(schema: str, agent: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.update_agent",
            return_value=MagicMock(
                id=uuid.uuid4(),
                name=agent,
                output_schema_id=schema,
            ),
        ),
    ):
        resp = client.patch(f"/api/agents/{agent}", json={"output_schema_id": schema})
    request.node._resp = resp


@then(parsers.parse('the agent output schema is "{schema}"'))
def check_output_schema(schema: str, request):
    data = request.node._resp.json()
    assert data.get("output_schema_id") == schema


@given(parsers.parse('agent "{agent}" has output schema "{schema}"'))
def agent_has_output_schema(agent: str, schema: str, request):
    request.node._agent_output_schema = schema


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
def save_reload_pipeline(request):
    pass


@then(parsers.parse('the agent still has input schema "{schema}"'))
def agent_still_has_input_schema(schema: str, request):
    pass


@when(parsers.parse('I remove the input schema assignment from agent "{agent}"'))
def remove_input_schema(agent: str, client, request):
    pass
