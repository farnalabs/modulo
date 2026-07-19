"""Architecture + E2E tests for the composed pipeline execution system.

Tests that:
  - make_node_fn produces completed output when invoked via a real hub
  - A full LangGraph graph with an agent node compiles and runs to completion
  - The StubModelBackend fixture map is reachable from a running pipeline
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import BaseMessage

from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.pipeline_engine.decorator import set_model_backend_hub
from modulo.core.pipeline_engine.graph_cache import build_graph_from_json
from modulo.core.pipeline_engine.node_runner import make_node_fn
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.stub.backend import StubModelBackend


class _StubAdapter(ModelBackendBase):
    """Adapts StubModelBackend (BaseChatModel) to ModelBackendBase async invoke.

    ModelBackendBase defines invoke() as an abstract async method.  Real backends
    (OpenAI, Anthropic) implement it as async.  StubModelBackend inherits BaseChatModel
    which has a synchronous invoke() — not compatible with make_node_fn's
    ``await backend.invoke()`` call.
    """

    def __init__(self, fixture_map: dict[str, str]) -> None:
        self._inner = StubModelBackend(fixture_map)

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._inner.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        return self._inner.astream(messages, tools=tools, **kwargs)

    @property
    def backend_id(self) -> str:
        return "stub"


class TestMakeNodeFnWithRealHub:
    """Architecture test: make_node_fn produces completed output.

    This test validates the core loop wiring — that make_node_fn actually
    invokes a model backend, parses its output, and returns structured artifacts.
    """

    async def test_node_fn_returns_completed_with_stub_output(self) -> None:
        """A node with a StubModelBackend returns artifacts with status 'completed' and the expected output."""
        node_id = str(uuid.uuid4())
        backend_id = uuid.uuid4()
        node_def = {
            "id": node_id,
            "prompt_template": "Hello {{ state.run_context.input.name }}",
            "model_backend_id": str(backend_id),
        }
        node_fn = make_node_fn(node_def, role="agent")

        hub = ModelBackendHub()
        await hub.__aenter__()
        hub.register(
            backend_id,
            _StubAdapter(
                {
                    "Hello World": json.dumps({"greeting": "Hello, World!"}),
                }
            ),
        )
        set_model_backend_hub(hub)

        state = {
            "run_context": {"input": {"name": "World"}},
            "artifacts": [],
        }

        try:
            result = await node_fn(state)

            assert "artifacts" in result
            assert len(result["artifacts"]) == 1
            artifact = result["artifacts"][0]
            assert artifact["node_id"] == node_id
            assert artifact["status"] == "completed"
            assert artifact["output"] == {"greeting": "Hello, World!"}
        finally:
            set_model_backend_hub(None)
            await hub.__aexit__(None, None, None)


class TestPipelineE2E:
    """E2E test: a full pipeline graph compiles and runs through LangGraph.

    Uses a real StateGraph compiled via build_graph_from_json with
    StubModelBackend for LLM calls and a real ModelBackendHub.
    """

    async def test_single_agent_graph_runs_to_completion(self) -> None:
        """A graph with one agent node compiles and executes through LangGraph."""
        node_id = str(uuid.uuid4())
        backend_id = uuid.uuid4()

        hub = ModelBackendHub()
        await hub.__aenter__()
        hub.register(
            backend_id,
            _StubAdapter(
                {
                    "What is the capital of France?": json.dumps({"answer": "Paris"}),
                }
            ),
        )
        set_model_backend_hub(hub)

        graph_json = {
            "nodes": [
                {
                    "id": node_id,
                    "agent_id": str(uuid.uuid4()),
                    "role": "agent",
                    "prompt_template": "What is the capital of France?",
                    "model_backend_id": str(backend_id),
                },
            ],
            "edges": [],
        }

        mock_session_factory = MagicMock()

        try:
            compiled = build_graph_from_json(
                graph_json,
                session_factory=mock_session_factory,
                org_id=uuid.uuid4(),
            )

            assert compiled is not None

            config = {"configurable": {"thread_id": str(uuid.uuid4())}}
            state = {
                "run_context": {"input": {}, "cancelled": False},
                "artifacts": [],
            }

            result = await compiled.ainvoke(state, config)

            artifacts = result.get("artifacts", [])
            assert len(artifacts) >= 1
            completed = [a for a in artifacts if a.get("status") == "completed"]
            assert len(completed) >= 1, f"No completed artifacts found in {artifacts}"
            assert completed[0]["node_id"] == node_id
        finally:
            set_model_backend_hub(None)
            await hub.__aexit__(None, None, None)
