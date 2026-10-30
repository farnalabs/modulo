"""FAR-2238: agents seeded with bare-string connector refs must still serialise."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from modulo.api.routes.agents import AgentResponse


def _row(connector_type_refs: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        name="PR Review Agent",
        description=None,
        is_executable=True,
        input_schema_id=None,
        input_schema_version=None,
        output_schema_id=None,
        output_schema_version=None,
        prompt_template="x",
        prompt_version_history=[],
        model_backend_id=None,
        connector_type_refs=connector_type_refs,
        evals=None,
        retry_policy={},
        token_budget=None,
        max_input_length=None,
        library_id=None,
        prompt_always_visible=False,
        required_environment_capabilities=[],
        template_id=None,
        agent_commands=None,
        schema_profile=None,
        account_id=uuid.uuid4(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_bare_string_connector_ref_is_normalised_to_object() -> None:
    resp = AgentResponse.model_validate(_row(["github"]))
    assert resp.connector_type_refs == [{"connector_type": "github", "capabilities": []}]


def test_object_connector_ref_is_unchanged() -> None:
    ref = {"connector_type": "github", "capabilities": ["create_pr"]}
    resp = AgentResponse.model_validate(_row([ref]))
    assert resp.connector_type_refs == [ref]
