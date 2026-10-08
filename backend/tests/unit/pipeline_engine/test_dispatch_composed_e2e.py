"""FAR-1141 / ADR-042: one COMPOSED end-to-end test of the dispatch seam.

Acceptance criterion 1 ("there is no run-level end-to-end test: nothing
exercises create_run → executor → dispatch node → provider → downstream
continuation") is closed here for the engine-level half of that chain.

What this test composes — every layer is the REAL production object, none of it
stubbed:

1. the graph JSON, compiled by the real ``build_graph_from_json`` (LangGraph
   ``StateGraph``), and executed with ``compiled.ainvoke`` — the same entry the
   executor uses;
2. the real ``make_connector_fn`` dispatch node, routing
   ``connector_binding.operation="dispatch"`` to ``trigger_run`` and then
   awaiting a terminal substrate status through ``_await_dispatch_terminal``;
3. the real ``_TracedConnector`` proxy (tracing + ACL class) wrapping
4. the real ``CircleCIConnector`` making REAL HTTP calls — intercepted only at
   the transport by respx, so the connector's own request/response parsing is
   exercised;
5. a downstream agent node that consumes the dispatch node's output out of run
   state (its prompt template renders ``state.output``), proving the hand-off.

The CircleCI runner is used as the provider because its ``trigger_run`` result
id round-trips into ``get_run_status`` (``pipeline id`` is the polling key), so
the full fire→await→observe cycle runs against one provider contract. Every
provider now honours that run-id contract — the GitHub Actions gap this comment
once noted (bare numeric id from ``trigger_run`` vs ``owner/repo/id`` required
by ``get_run_status``) was closed by the provider-parity round-trip fix, pinned
by ``test_dispatch_parity.test_trigger_run_id_round_trips_into_get_run_status``.

Layer this test stops at: the DB/executor half of the chain — ``create_run``
persisting ``runs.execution_origin`` and the run-scoped executor plumbing
(claim, checkpoint, finalize) — is deliberately NOT exercised; it needs a live
Postgres and is covered by ``tests/unit/db/test_run_execution_origin.py`` plus
the executor suite. Everything from graph compile through provider HTTP to the
downstream node runs for real here.

It FAILS if dispatch routing, wait semantics, or the downstream hand-off
regress: no routing → the POST never fires; no wait → the output never reaches
``success``; no hand-off → the downstream prompt never sees the substrate
status.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import httpx
import respx
from langchain_core.messages import BaseMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import modulo.core.pipeline_engine.node_runner as nr
from modulo.connectors.base import ConnectorACL
from modulo.connectors.circleci import CircleCIConnector
from modulo.core.connector_hub import _TracedConnector
from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.pipeline_engine.decorator import set_connector_hub, set_model_backend_hub
from modulo.core.pipeline_engine.graph_cache import build_graph_from_json
from modulo.db.crud.run import graph_contains_dispatch
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.stub.backend import StubModelBackend

_PROJECT_SLUG = "gh/acme/widget"
_PIPELINE_URL = "https://circleci.com/api/v2/project/gh/acme/widget/pipeline"
_STATUS_URL = "https://circleci.com/api/v2/pipeline/pipe-uuid-123"
_DISPATCH_INSTANCE_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
#: Model backend the downstream node renders through (registered in the hub
#: under this UUID; stored in the graph as its JSON string form).
_BACKEND_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


class _Hub:
    """The seam the engine resolves a bound connector instance through."""

    def __init__(self, connector: Any) -> None:
        self._connector = connector

    def get(self, instance_id: uuid.UUID) -> Any:
        return self._connector


class _RecordingStubAdapter(ModelBackendBase):
    """Adapter over the strict ``StubModelBackend``, recording every prompt.

    The fixture map is keyed on the EXACT rendered prompt, so a downstream node
    that never saw the dispatch output raises ``UnexpectedInputError`` — the
    hand-off is asserted by construction, not by inspection.
    """

    def __init__(self, fixture_map: dict[str, str]) -> None:
        self._inner = StubModelBackend(fixture_map)
        self.prompts: list[str] = []

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        self.prompts.append(str(messages[0].content if messages else ""))
        return await self._inner.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], tools: list[dict[str, Any]] | None = None, **kwargs: Any):
        raise NotImplementedError("the composed dispatch test only exercises invoke")

    @property
    def backend_id(self) -> str:
        return "stub"


def _dispatch_graph() -> dict[str, Any]:
    """dispatch (fires + awaits) → downstream agent that reads the result."""
    return {
        "nodes": [
            {
                "id": "dispatch-ci",
                "node_type": "dispatch",
                "connector_binding": {
                    "instance_id": _DISPATCH_INSTANCE_ID,
                    "type": "circleci",
                    "operation": "dispatch",
                    "dispatch_action": "trigger_run",
                    "data": {"pipeline_id": _PROJECT_SLUG},
                },
                "await_completion": True,
                "wait_timeout": 10,
            },
            {
                "id": "consume-result",
                "node_type": "agent",
                # Renders the dispatch node's stamped output out of run state —
                # the downstream hand-off this test exists to prove.
                "prompt_template": "substrate={{ state.output.substrate_status }}",
                # A real graph stores the id as the JSON string; the engine
                # parses it back to a UUID before the hub lookup.
                "model_backend_id": str(_BACKEND_ID),
            },
        ],
        "edges": [{"source": "dispatch-ci", "target": "consume-result", "type": "normal"}],
    }


def _trigger_response() -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "id": "pipe-uuid-123",
            "number": 42,
            "project_slug": _PROJECT_SLUG,
            "state": "created",
            "trigger": {"actor": {"login": "octocat"}},
            "vcs": {"branch": "main", "revision": "abc123"},
        },
    )


def _status_responses() -> list[httpx.Response]:
    """First poll non-terminal, second poll terminal-success — so the wait loop
    has to actually loop, and finishes without sleeping out the window."""
    body = {
        "id": "pipe-uuid-123",
        "number": 42,
        "project_slug": _PROJECT_SLUG,
        "trigger": {"actor": {"login": "octocat"}},
        "vcs": {"branch": "main", "revision": "abc123"},
    }
    return [
        httpx.Response(200, json={**body, "state": "running"}),
        httpx.Response(200, json={**body, "state": "success"}),
    ]


@respx.mock
async def test_composed_dispatch_graph_fires_awaits_and_hands_off_downstream() -> None:
    respx.post(_PIPELINE_URL).mock(return_value=_trigger_response())
    respx.get(_STATUS_URL).mock(side_effect=_status_responses())

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    traced = _TracedConnector(
        CircleCIConnector(token="test-token"),  # nosec - test credential
        tracer=provider.get_tracer("composed-dispatch-test"),
        acl=ConnectorACL("org", allowed_operations=["read", "write"]),
    )

    graph = _dispatch_graph()
    # The SAME classification the run-creation path stamps onto
    # ``runs.execution_origin`` — dispatch provenance and engine routing must
    # agree node-for-node, or a dispatched run would read as executed.
    assert graph_contains_dispatch(graph) is True

    adapter = _RecordingStubAdapter({"substrate=success": '{"consumed":"success"}'})
    backend_hub = ModelBackendHub()
    await backend_hub.__aenter__()
    set_connector_hub(_Hub(traced))
    set_model_backend_hub(backend_hub)
    backend_hub.register(_BACKEND_ID, adapter)
    try:
        compiled = build_graph_from_json(graph, session_factory=None, org_id=uuid.uuid4())
        initial_state: dict[str, Any] = {"run_context": {"cancelled": False, "input": {}}, "artifacts": []}
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        with patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001):
            result = await compiled.ainvoke(initial_state, config)
    finally:
        set_connector_hub(None)
        set_model_backend_hub(None)
        await backend_hub.__aexit__(None, None, None)

    # --- dispatch routing: the provider was really called, exactly once -------
    trigger_calls = [c for c in respx.calls if c.request.url.path.endswith("/pipeline")]
    assert len(trigger_calls) == 1, f"a dispatch node must fire ONE job, saw {len(trigger_calls)}"

    # --- wait semantics: it polled the substrate to a terminal status ---------
    status_calls = [c for c in respx.calls if c.request.url.path.endswith("/pipe-uuid-123")]
    assert len(status_calls) == 2, f"the wait loop must poll to terminal, saw {len(status_calls)} polls"

    artifacts = {a["node_id"]: a for a in result["artifacts"]}
    assert set(artifacts) == {"dispatch-ci", "consume-result"}

    dispatch_artifact = artifacts["dispatch-ci"]
    assert dispatch_artifact["status"] == "completed"
    output = dispatch_artifact["output"]
    # Terminal substrate status, observed (not fired) — provenance says this
    # node witnessed the customer's substrate, never executed the work itself.
    assert output["substrate_status"] == "success"
    assert output["execution_identity"] == "customer_substrate"
    assert output["witnessed_via"] == _DISPATCH_INSTANCE_ID
    assert output["declared_external_cost"] is None

    # --- downstream hand-off: the consumer read the dispatch output -----------
    assert artifacts["consume-result"]["status"] == "completed"
    assert adapter.prompts == ["substrate=success"], (
        "the downstream node must render the dispatch node's output from run state"
    )

    # --- the traced layer really proxied both provider calls ------------------
    # Span names are ``connector.<type>.<op>`` — derived from the proxy's own
    # connector type so the assertion tracks the enum, not a re-spelling of it.
    ci_type = str(traced.connector_type)
    span_names = {span.name for span in exporter.get_finished_spans()}
    assert span_names == {f"connector.{ci_type}.trigger_run", f"connector.{ci_type}.get_run_status"}
    assert all(span.status.status_code == StatusCode.OK for span in exporter.get_finished_spans())
