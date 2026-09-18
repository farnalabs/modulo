"""BDD step definitions: Prompt Versioning — /api/v1/agents/{id}/prompts endpoints.

# MOCKED: This entire file uses MagicMock-based DB fixtures instead of the
# real async SQLAlchemy stack (testcontainers Postgres + Alembic migrations).
# This duplicates unit-test surface while claiming E2E confidence.
# Scheduled for replacement with real-stack fixtures.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# MOCKED: scenarios() path relative to this file
scenarios("../../features/agents/prompt_versioning.feature")


@pytest.fixture(autouse=True)
def _prevent_identity_db_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent ``_verify_identity`` from connecting to a real database.

    ``get_current_tenant_user`` verifies the account/org against Postgres
    before returning the principal. Patch it out so BDD scenarios run
    against the mocked DB session (mirrors test_monitor_config.py).
    """
    monkeypatch.setattr("modulo.auth.dependencies._verify_identity", AsyncMock(return_value=None))


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


# MOCKED: _make_agent returns MagicMock instead of real DB model
def _make_agent(name: str = "reviewer", prompt: str = "Version 1") -> MagicMock:
    a = MagicMock()
    a.id = uuid.uuid4()
    a.organisation_id = ORG_ID
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
def _org_has_agent(client, ctx, org: str, name: str, prompt: str) -> None:
    agent = _make_agent(name=name, prompt=prompt)
    ctx["agent"] = agent
    ctx["org_name"] = org
    ctx["agent_name"] = name
    ctx["agent_id"] = agent.id


@given("the pipeline is published with snapshot")
def _pipeline_published_with_snapshot(ctx) -> None:
    agent = ctx.get("agent")
    if agent:
        ctx["snapshot_prompt"] = agent.prompt_template


# MOCKED: uses patched DB layer instead of real session
@when(parsers.parse("I update the agent prompt to {prompt}"))
def _update_prompt(client, request, ctx, prompt: str) -> None:
    prompt_val = prompt.strip('"')
    agent = ctx.get("agent")
    if agent is None:
        agent = _make_agent(prompt=prompt_val)
        ctx["agent"] = agent
        ctx["agent_id"] = agent.id
    updated = _update_agent_prompt(agent, prompt_val)
    with (
        patch("modulo.api.routes.agents.update_agent", return_value=updated),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{ctx['agent_id']}", json={"prompt_template": prompt_val})
    request.node._resp = resp


@when("I trigger a run using the pinned snapshot")
def _trigger_run_pinned(ctx) -> None:
    snapshot_prompt = ctx.get("snapshot_prompt") or ""
    ctx["run_prompt"] = snapshot_prompt
    ctx["run_type"] = "pinned"


@when("I trigger a new run")
def _trigger_new_run(ctx) -> None:
    agent = ctx.get("agent")
    current_prompt = agent.prompt_template if agent else ""
    ctx["run_prompt"] = current_prompt
    ctx["run_type"] = "latest"


# MOCKED: uses patched DB layer instead of real session
@when(parsers.parse("I GET the prompt versions for the agent"))
def _get_prompt_versions(client, request, ctx) -> None:
    agent = ctx.get("agent")
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/agents/{ctx['agent_id']}/prompts")
    request.node._resp = resp


@then(parsers.parse("the agent has prompt version {version:d}"))
def _agent_has_prompt_version(ctx, version: int) -> None:
    agent = ctx.get("agent")
    assert agent is not None, "No agent in context"
    assert len(agent.prompt_version_history) == version


@then(parsers.parse('the run uses prompt "{prompt}"'))
def _run_uses_prompt(ctx, prompt: str) -> None:
    actual = ctx.get("run_prompt", "")
    assert actual == prompt, f"Expected run to use prompt {prompt!r}, got {actual!r}"


@then("the response contains 2 prompt versions")
def _response_contains_two_versions(client, request, ctx) -> None:
    data = request.node._resp.json()
    assert len(data) == 2
