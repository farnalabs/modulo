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
    """The namespaced ``_run_overrides`` override is used, not the node_def backend."""
    override_backend = str(uuid.uuid4())
    state = {
        "run_context": {"input": {"task": "classify", "_run_overrides": {"model_backend_id": override_backend}}},
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
    """A namespaced ``_run_overrides`` prompt_template is used, not the node_def's.

    FAR-342: the variant comparison's prompt_version picker resolves a version
    label to a template at run creation and stores it under
    ``_run_overrides["prompt_template"]``; the node runner must render that
    template instead of the snapshot-embedded node_def prompt.
    """
    override_prompt = "Render THIS prompt version instead."
    state = {
        "run_context": {
            "input": {
                "task": "classify",
                "_run_overrides": {"prompt_template": override_prompt},
            }
        },
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    assert hub.prompts == [override_prompt]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_node_def_prompt_used_when_no_prompt_override() -> None:
    """Without a prompt_template override, the node_def prompt is rendered."""
    state = {"run_context": {"input": {"task": "classify"}}, "artifacts": []}
    result, hub = await _run_node(state)
    assert hub.prompts == ["Summarise the input."]
    assert result["artifacts"][0]["status"] == "completed"
