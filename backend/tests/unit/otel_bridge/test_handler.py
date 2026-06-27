"""Unit tests for LangGraphOtelBridge.

Uses OTel's InMemorySpanExporter — no network, no DB, no LangGraph process.
"""

import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from modulo.otel_bridge.handler import LangGraphOtelBridge


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture()
def bridge(exporter: InMemorySpanExporter) -> LangGraphOtelBridge:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.langgraph")
    return LangGraphOtelBridge(tracer=tracer)


def _serialized(name: str) -> dict[str, Any]:
    return {"name": name, "id": ["langchain", name]}


# ---------------------------------------------------------------------------
# Chain lifecycle
# ---------------------------------------------------------------------------


def test_chain_start_creates_span(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("MyNode"), {}, run_id=run_id)

    assert str(run_id) in bridge._spans
    spans = exporter.get_finished_spans()
    assert len(spans) == 0, "span should not be finished until on_chain_end"


def test_chain_end_finishes_span_ok(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("MyNode"), {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "langgraph.chain.MyNode"
    assert span.status.status_code == StatusCode.OK
    assert str(run_id) not in bridge._spans


def test_chain_error_finishes_span_with_error(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    err = ValueError("boom")
    bridge.on_chain_start(_serialized("ErrNode"), {}, run_id=run_id)
    bridge.on_chain_error(err, run_id=run_id)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is not None and "boom" in span.status.description
    events = [e.name for e in span.events]
    assert "exception" in events
    assert str(run_id) not in bridge._spans


def test_chain_attributes_set(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("SomeChain"), {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("langgraph.chain.name") == "SomeChain"


# ---------------------------------------------------------------------------
# Parent-child span propagation
# ---------------------------------------------------------------------------


def test_parent_child_propagation(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    parent_id = uuid.uuid4()
    child_id = uuid.uuid4()

    bridge.on_chain_start(_serialized("Root"), {}, run_id=parent_id)
    bridge.on_chain_start(_serialized("Child"), {}, run_id=child_id, parent_run_id=parent_id)
    bridge.on_chain_end({}, run_id=child_id)
    bridge.on_chain_end({}, run_id=parent_id)

    spans = exporter.get_finished_spans()
    assert len(spans) == 2

    child = next(s for s in spans if "Child" in s.name)
    parent = next(s for s in spans if "Root" in s.name)

    assert child.parent is not None
    assert child.parent.span_id == parent.context.span_id


def test_unknown_parent_run_id_is_handled_gracefully(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    run_id = uuid.uuid4()
    unknown_parent = uuid.uuid4()
    # Should not raise even though parent_run_id is not in _spans
    bridge.on_chain_start(_serialized("Orphan"), {}, run_id=run_id, parent_run_id=unknown_parent)
    bridge.on_chain_end({}, run_id=run_id)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].parent is None


# ---------------------------------------------------------------------------
# LLM lifecycle
# ---------------------------------------------------------------------------


def test_llm_start_creates_span(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)

    assert str(run_id) in bridge._spans
    span_name = bridge._spans[str(run_id)].name  # type: ignore[attr-defined]
    assert "gpt-4" in span_name


def test_llm_end_records_token_usage(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)

    result = LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="hi"))]],
        llm_output={
            "token_usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            }
        },
    )
    bridge.on_llm_end(result, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("langgraph.llm.prompt_tokens") == 10
    assert span.attributes.get("langgraph.llm.completion_tokens") == 5
    assert span.attributes.get("langgraph.llm.total_tokens") == 15
    assert span.status.status_code == StatusCode.OK


def test_llm_end_without_token_usage_does_not_raise(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)
    bridge.on_llm_end(LLMResult(generations=[], llm_output=None), run_id=run_id)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.OK


def test_llm_error_records_exception(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)
    bridge.on_llm_error(RuntimeError("rate limit"), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR


# ---------------------------------------------------------------------------
# Tool lifecycle
# ---------------------------------------------------------------------------


def test_tool_start_and_end(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_tool_start(_serialized("search"), "query", run_id=run_id)
    bridge.on_tool_end("result", run_id=run_id)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "search" in spans[0].name
    assert spans[0].status.status_code == StatusCode.OK


def test_tool_error(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_tool_start(_serialized("search"), "query", run_id=run_id)
    bridge.on_tool_error(ConnectionError("timeout"), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert str(run_id) not in bridge._spans


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_end_without_start_does_not_raise(bridge: LangGraphOtelBridge) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_end({}, run_id=run_id)
    bridge.on_llm_end(LLMResult(generations=[], llm_output=None), run_id=run_id)
    bridge.on_tool_end("x", run_id=run_id)


def test_spans_dict_empty_after_full_lifecycle(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    root = uuid.uuid4()
    llm = uuid.uuid4()
    tool = uuid.uuid4()

    bridge.on_chain_start(_serialized("root"), {}, run_id=root)
    bridge.on_llm_start(_serialized("llm"), ["p"], run_id=llm, parent_run_id=root)
    bridge.on_tool_start(_serialized("tool"), "i", run_id=tool, parent_run_id=root)
    bridge.on_llm_end(LLMResult(generations=[], llm_output=None), run_id=llm)
    bridge.on_tool_end("o", run_id=tool)
    bridge.on_chain_end({}, run_id=root)

    assert bridge._spans == {}
    assert len(exporter.get_finished_spans()) == 3


def test_serialized_name_falls_back_to_id_path(bridge: LangGraphOtelBridge) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start({"id": ["a", "b", "MyClass"]}, {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)


def test_serialized_name_handles_none(bridge: LangGraphOtelBridge) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start(None, {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)
