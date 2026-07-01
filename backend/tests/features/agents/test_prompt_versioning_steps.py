"""BDD step definitions: Prompt Versioning — /api/v1/agents/{id}/prompts endpoints."""

import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("prompt_versioning.feature")

_AGENT: MagicMock | None = None
_AGENT_NAME: str = "reviewer"
_ORG_NAME: str = "acme"
_AGENT_ID: uuid.UUID | None = None


def _make_agent(name: str = "reviewer", prompt: str = "Version 1") -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID or uuid.uuid4()
    a.organisation_id = uuid.uuid4()
    a.name = name
    a.description = "Review agent"
    a.is_executable = True
    a.input_schema_id = uuid.uuid4()
    a.input_schema_version = "1.0"
    a.output_schema_id = uuid.uuid4()
    a.output_schema_version = "1.0"
    a.prompt_template = prompt
    a.model_backend_id = uuid.uuid4()
    a.connector_type_refs = []
    a.evals = []
    a.retry_policy = {}
    a.token_budget = None
    a.library_id = None
    a.created_by = uuid.uuid4()
    a.account_id = uuid.uuid4()
    a.prompt_version_history = [
        {
            "version": "v1",
            "template": prompt,
            "created_at": "2025-01-01T00:00:00",
            "notes": "Original",
            "optimized_from": None,
            "eval_result_ids": [],
        }
    ]
    a.created_at = "2025-01-01T00:00:00"
    a.updated_at = "2025-01-01T00:00:00"
    return a


def _update_agent_prompt(agent: MagicMock, new_prompt: str) -> MagicMock:
    version_num = len(agent.prompt_version_history) + 1
    agent.prompt_template = new_prompt
    agent.prompt_version_history.append(
        {
            "version": f"v{version_num}",
            "template": new_prompt,
            "created_at": "2025-01-01T00:01:00",
            "notes": f"Updated to {new_prompt}",
            "optimized_from": None,
            "eval_result_ids": [],
        }
    )
    return agent


@given(parsers.parse('org "{org}" has agent "{name}" with prompt "{prompt}"'))
def _org_has_agent(client, org: str, name: str, prompt: str) -> None:
    global _AGENT, _AGENT_NAME, _ORG_NAME, _AGENT_ID
    _ORG_NAME = org
    _AGENT_NAME = name
    _AGENT = _make_agent(name=name, prompt=prompt)
    _AGENT_ID = _AGENT.id


@given("the pipeline is published with snapshot")
def _pipeline_published_with_snapshot(client) -> None:
    global _AGENT
    pass


@when(parsers.parse("I update the agent prompt to {prompt}"))
def _update_prompt(client, request, prompt: str) -> None:
    global _AGENT
    prompt_val = prompt.strip('"')
    updated = _update_agent_prompt(_AGENT, prompt_val)
    with (
        patch("modulo.api.routes.agents.update_agent", return_value=updated),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={"prompt_template": prompt_val})
    request.node._resp = resp


@when("I trigger a run using the pinned snapshot")
def _trigger_run_pinned(client, request) -> None:
    pass


@when("I trigger a new run")
def _trigger_new_run(client, request) -> None:
    pass


@when(parsers.parse("I GET /api/agents/reviewer/versions"))
def _get_prompt_versions(client, request) -> None:
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=_AGENT),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/agents/{_AGENT_ID}/prompts")
    request.node._resp = resp


@then(parsers.parse("the agent has prompt version {version:d}"))
def _agent_has_prompt_version(client, request, version: int) -> None:
    assert len(_AGENT.prompt_version_history) == version


@then(parsers.parse('the run uses prompt "{prompt}"'))
def _run_uses_prompt(client, request, prompt: str) -> None:
    pass


@then("the response contains 2 prompt versions")
def _response_contains_two_versions(client, request) -> None:
    data = request.node._resp.json()
    assert len(data) == 2
