"""Unit tests for the FAR-332 run_context model_backend_id override.

The variant comparison and A/B test views emit a ``model_backend_id``
``run_context_overrides`` entry that is merged into the run's input payload.
The node runner must honour that override in preference to the
snapshot-embedded ``node_def["model_backend_id"]`` so every variant fires with
its own model backend.
"""

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage


class _FakeBackend:
    """Fake model backend whose invoke() returns a fixed JSON payload."""

    def __init__(self, recorder: "_RecordingHub") -> None:
        self._recorder = recorder

    async def invoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self._recorder.prompts.append(messages[0].content)
        return AIMessage(content='{"ok": true}')


class _RecordingHub:
    """Fake ModelBackendHub that records every backend_id + prompt it was given."""

    def __init__(self) -> None:
        self.requested_ids: list[str] = []
        self.prompts: list[str] = []

    async def get(self, backend_id: uuid.UUID, **kwargs: Any) -> _FakeBackend:
        self.requested_ids.append(str(backend_id))
        return _FakeBackend(self)


async def _run_node(state: dict[str, Any]) -> tuple[dict[str, Any], _RecordingHub]:
    """Build an agent node and run it, returning the result and the recording hub."""
    from modulo.core.pipeline_engine.node_runner import make_node_fn

    node_def = {
        "id": "agent-1",
        "agent_id": "22222222-2222-2222-2222-222222222222",
        "prompt_template": "Summarise the input.",
        "model_backend_id": "11111111-1111-1111-1111-111111111111",
    }
    hub = _RecordingHub()
    node_fn = make_node_fn(node_def)
    with (
        patch(
            "modulo.core.pipeline_engine.node_runner.get_conformance_ctx",
            return_value=None,
        ),
        patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ),
    ):
        result = await node_fn(state)
    return result, hub


@pytest.mark.asyncio
async def test_run_context_model_backend_override_wins_over_node_def() -> None:
    """The namespaced ``_run_overrides`` override is used, not the node_def backend.

    The executor seeds ``_run_overrides`` as a TOP-LEVEL run_context key from the
    run's frozen variant config — never inside ``input``.
    """
    override_backend = str(uuid.uuid4())
    state = {
        "run_context": {"input": {"task": "classify"}, "_run_overrides": {"model_backend_id": override_backend}},
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    assert hub.requested_ids == [override_backend]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_node_def_model_backend_used_when_no_override() -> None:
    """Without an override, the snapshot-embedded node_def backend is used."""
    state = {"run_context": {"input": {"task": "classify"}}, "artifacts": []}
    result, hub = await _run_node(state)
    assert hub.requested_ids == ["11111111-1111-1111-1111-111111111111"]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_bare_data_model_backend_id_does_not_hijack() -> None:
    """A plain top-level ``model_backend_id`` data field must NOT reroute the model."""
    state = {
        "run_context": {"input": {"task": "classify", "model_backend_id": str(uuid.uuid4())}},
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    assert hub.requested_ids == ["11111111-1111-1111-1111-111111111111"]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_override_ignored_when_not_a_dict_input() -> None:
    """A non-dict input (string) cannot supply the override — falls back to node_def."""
    state = {"run_context": {"input": "free text prompt"}, "artifacts": []}
    result, hub = await _run_node(state)
    assert hub.requested_ids == ["11111111-1111-1111-1111-111111111111"]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_prompt_template_override_wins_over_node_def() -> None:
    """A namespaced ``_run_overrides`` prompt_templates map is used for THIS node's agent.

    FAR-342: the variant comparison's prompt_version picker resolves a version
    label to a per-agent template map at run creation and stores it under
    ``_run_overrides["prompt_templates"]`` keyed by agent_id; the node runner
    must render the template for the node's OWN agent instead of the
    snapshot-embedded node_def prompt.
    """
    override_prompt = "Render THIS prompt version instead."
    state = {
        "run_context": {
            "input": {"task": "classify"},
            "_run_overrides": {"prompt_templates": {"22222222-2222-2222-2222-222222222222": override_prompt}},
        },
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    assert hub.prompts == [override_prompt]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_other_agent_prompt_override_does_not_clobber_this_node() -> None:
    """A prompt_templates override for a DIFFERENT agent must not apply here.

    FAR-342: in a multi-agent snapshot one agent's template must never clobber
    another's. This node's agent has no entry in the map, so it falls back to
    the node_def prompt.
    """
    other_agent_prompt = "This belongs to another agent."
    state = {
        "run_context": {
            "input": {"task": "classify"},
            "_run_overrides": {"prompt_templates": {"99999999-9999-9999-9999-999999999999": other_agent_prompt}},
        },
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    assert hub.prompts == ["Summarise the input."]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_node_def_prompt_used_when_no_prompt_override() -> None:
    """Without a prompt_templates override, the node_def prompt is rendered."""
    state = {"run_context": {"input": {"task": "classify"}}, "artifacts": []}
    result, hub = await _run_node(state)
    assert hub.prompts == ["Summarise the input."]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_normal_run_caller_supplied_run_overrides_is_data_not_override() -> None:
    """FAR-342 injection: a NORMAL run's ``_run_overrides`` in input is DATA.

    A caller-supplied ``input_payload={"task": ..., "_run_overrides": {...}}``
    flows into ``run_context["input"]`` untouched — the executor only ever seeds
    the TOP-LEVEL ``_run_overrides`` key from the run's frozen variant config.
    With no top-level seed, the node_runner must render the snapshot prompt, NOT
    the injected one.
    """
    injected = "INJECTED prompt via caller input."
    state = {
        "run_context": {
            "input": {
                "task": "classify",
                "_run_overrides": {"prompt_templates": {"22222222-2222-2222-2222-222222222222": injected}},
            }
        },
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    # The node_def prompt is rendered — the injected template in input never
    # reaches the override boundary.
    assert hub.prompts == ["Summarise the input."]
    assert result["artifacts"][0]["status"] == "completed"
