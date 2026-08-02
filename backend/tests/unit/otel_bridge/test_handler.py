"""Unit tests for LangGraphOtelBridge.

Uses OTel's InMemorySpanExporter — no network, no DB, no LangGraph process.
"""

import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
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


def test_llm_span_inherits_parent_context(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    chain_id = uuid.uuid4()
    llm_id = uuid.uuid4()

    bridge.on_chain_start(_serialized("Chain"), {}, run_id=chain_id)
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=llm_id, parent_run_id=chain_id)
    bridge.on_llm_end(LLMResult(generations=[], llm_output=None), run_id=llm_id)
    bridge.on_chain_end({}, run_id=chain_id)

    spans = exporter.get_finished_spans()
    llm = next(s for s in spans if "llm" in s.name)
    chain = next(s for s in spans if "chain" in s.name)
    assert llm.parent is not None
    assert llm.parent.span_id == chain.context.span_id


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
# Run context (org_id / pipeline_id)
# ---------------------------------------------------------------------------


def test_set_run_context_attributes_on_spans(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    bridge.set_run_context(org_id="org-123", pipeline_id="pipe-456")
    run_id = uuid.uuid4()

    bridge.on_chain_start(_serialized("Node"), {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("organisation_id") == "org-123"
    assert span.attributes.get("pipeline_id") == "pipe-456"


def test_run_context_via_constructor(
    exporter: InMemorySpanExporter,
) -> None:
    """The constructor's org_id/pipeline_id must seed span attributes."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.langgraph")
    bridge = LangGraphOtelBridge(tracer=tracer, org_id="org-ctor", pipeline_id="pipe-ctor")
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("Node"), {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("organisation_id") == "org-ctor"
    assert span.attributes.get("pipeline_id") == "pipe-ctor"


def test_run_context_not_set_leaves_no_attributes(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("Node"), {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert "organisation_id" not in span.attributes
    assert "pipeline_id" not in span.attributes


def test_run_context_applies_to_new_spans_only(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    early_id = uuid.uuid4()
    late_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("Early"), {}, run_id=early_id)
    bridge.set_run_context(org_id="org-late", pipeline_id="pipe-late")
    bridge.on_chain_start(_serialized("Late"), {}, run_id=late_id)
    bridge.on_chain_end({}, run_id=late_id)
    bridge.on_chain_end({}, run_id=early_id)

    spans = exporter.get_finished_spans()
    early = next(s for s in spans if "Early" in s.name)
    late = next(s for s in spans if "Late" in s.name)
    # Context captured at span creation — early span has no attrs, late span does.
    assert "organisation_id" not in (early.attributes or {})
    assert (late.attributes or {}).get("organisation_id") == "org-late"


# ---------------------------------------------------------------------------
# LLM lifecycle
# ---------------------------------------------------------------------------


def test_llm_start_creates_span(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)

    assert str(run_id) in bridge._spans
    span_name = bridge._spans[str(run_id)].name  # type: ignore[attr-defined]
    assert "gpt-4" in span_name


def test_llm_start_records_prompt_count(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello", "World"], run_id=run_id)
    bridge.on_llm_end(LLMResult(generations=[], llm_output=None), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("langgraph.llm.prompt_count") == 2


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


def test_llm_end_token_usage_ignores_non_int_values(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)
    result = LLMResult(
        generations=[[]],
        llm_output={"token_usage": {"prompt_tokens": "ten", "total_tokens": None}},
    )
    bridge.on_llm_end(result, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert "langgraph.llm.prompt_tokens" not in span.attributes
    assert "langgraph.llm.total_tokens" not in span.attributes
    assert span.status.status_code == StatusCode.OK


def test_llm_end_token_usage_ignores_non_dict_usage(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)
    result = LLMResult(generations=[[]], llm_output={"token_usage": "not-a-dict"})
    bridge.on_llm_end(result, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.OK


def test_llm_error_records_exception(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)
    bridge.on_llm_error(RuntimeError("rate limit"), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR


def test_llm_start_sets_tags(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id, tags=["llm", "tagged"])
    bridge.on_llm_end(LLMResult(generations=[], llm_output=None), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("langgraph.tags") == ("llm", "tagged")


# ---------------------------------------------------------------------------
# Chat model lifecycle (BaseChatModel callbacks — production path)
# ---------------------------------------------------------------------------


def _chat_start(bridge: LangGraphOtelBridge, run_id: uuid.UUID, name: str = "gpt-4") -> None:
    bridge.on_chat_model_start(
        _serialized(name),
        [[HumanMessage(content="hello"), SystemMessage(content="sys")]],
        run_id=run_id,
    )


def test_chat_model_start_creates_span(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    _chat_start(bridge, run_id)

    assert str(run_id) in bridge._spans
    span_name = bridge._spans[str(run_id)].name  # type: ignore[attr-defined]
    assert "gpt-4" in span_name
    assert len(exporter.get_finished_spans()) == 0


def test_chat_model_end_finishes_span_ok(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    _chat_start(bridge, run_id)
    result = ChatResult(
        generations=[ChatGeneration(message=AIMessage(content="hi"))],
        llm_output={"token_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
    )
    bridge.on_chat_model_end(result, run_id=run_id)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert "gpt-4" in span.name
    assert span.status.status_code == StatusCode.OK
    assert span.attributes is not None
    assert span.attributes.get("langgraph.llm.prompt_tokens") == 7
    assert span.attributes.get("langgraph.llm.message_count") == 2
    assert str(run_id) not in bridge._spans


def test_chat_model_error_records_exception(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    _chat_start(bridge, run_id)
    bridge.on_chat_model_error(RuntimeError("provider down"), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert "provider down" in (span.status.description or "")
    assert str(run_id) not in bridge._spans


def test_chat_model_start_empty_messages(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chat_model_start(_serialized("gpt-4"), [], run_id=run_id)
    bridge.on_chat_model_end(ChatResult(generations=[]), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("langgraph.llm.message_count") == 0


def test_chat_model_span_inherits_parent_context(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    """Chat-model spans (the BaseChatModel production path) parent correctly."""
    chain_id = uuid.uuid4()
    chat_id = uuid.uuid4()

    bridge.on_chain_start(_serialized("Chain"), {}, run_id=chain_id)
    bridge.on_chat_model_start(
        _serialized("claude"),
        [[HumanMessage(content="hello")]],
        run_id=chat_id,
        parent_run_id=chain_id,
    )
    bridge.on_chat_model_end(ChatResult(generations=[]), run_id=chat_id)
    bridge.on_chain_end({}, run_id=chain_id)

    spans = exporter.get_finished_spans()
    chat = next(s for s in spans if "claude" in s.name)
    chain = next(s for s in spans if "chain" in s.name)
    assert chat.parent is not None
    assert chat.parent.span_id == chain.context.span_id


def test_chat_model_end_without_token_usage_does_not_raise(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    run_id = uuid.uuid4()
    _chat_start(bridge, run_id)
    bridge.on_chat_model_end(ChatResult(generations=[]), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.OK
    assert "langgraph.llm.prompt_tokens" not in (span.attributes or {})


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
    bridge.on_chat_model_end(ChatResult(generations=[]), run_id=run_id)

    assert bridge._spans == {}


def test_error_without_start_does_not_raise(bridge: LangGraphOtelBridge) -> None:
    """Error callbacks for unknown run_ids must be no-ops, not raise."""
    run_id = uuid.uuid4()
    bridge.on_chain_error(ValueError("boom"), run_id=run_id)
    bridge.on_llm_error(ValueError("boom"), run_id=run_id)
    bridge.on_chat_model_error(ValueError("boom"), run_id=run_id)
    bridge.on_tool_error(ValueError("boom"), run_id=run_id)

    assert bridge._spans == {}


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


def test_serialized_name_falls_back_to_id_path(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start({"id": ["a", "b", "MyClass"]}, {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.name == "langgraph.chain.MyClass"


def test_serialized_name_handles_none(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start(None, {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.name == "langgraph.chain.unknown"


def test_serialized_name_prefers_name_over_id(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start({"name": "ExplicitName", "id": ["ignored", "Fallback"]}, {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.name == "langgraph.chain.ExplicitName"


def test_serialized_name_empty_name_falls_back_to_id(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start({"name": "", "id": ["a", "b", "IdName"]}, {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.name == "langgraph.chain.IdName"


def test_serialized_name_handles_empty_dict(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start({}, {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.name == "langgraph.chain.unknown"


def test_serialized_name_non_list_id_falls_back_to_unknown(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    run_id = uuid.uuid4()
    # id is not a list — must not raise and must fall back to "unknown".
    bridge.on_chain_start({"id": "not-a-list"}, {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.name == "langgraph.chain.unknown"


def test_tags_set_on_span(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("Tagged"), {}, run_id=run_id, tags=["alpha", "beta"])
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("langgraph.tags") == ("alpha", "beta")


def test_no_tags_leaves_no_tags_attribute(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("Untagged"), {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert "langgraph.tags" not in span.attributes


# ---------------------------------------------------------------------------
# Stale-span and defensive exception paths
# ---------------------------------------------------------------------------


def test_start_with_same_run_id_finalizes_previous_span(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    """Reusing a run_id while the first span is still open ends the first span."""
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("First"), {}, run_id=run_id)
    bridge.on_chain_start(_serialized("Second"), {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    first = next(s for s in spans if "First" in s.name)
    second = next(s for s in spans if "Second" in s.name)
    assert first.status.status_code == StatusCode.OK
    assert second.status.status_code == StatusCode.OK


def test_stale_span_finalization_errors_are_swallowed(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    """Reusing a run_id whose previous span fails to finalize must not raise."""
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("First"), {}, run_id=run_id)
    stale = bridge._spans[str(run_id)]
    stale.set_status = lambda _status: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    stale.end = lambda: (_ for _ in ()).throw(RuntimeError("end boom"))  # type: ignore[method-assign]

    bridge.on_chain_start(_serialized("Second"), {}, run_id=run_id)  # must not raise
    bridge.on_chain_end({}, run_id=run_id)

    assert str(run_id) not in bridge._spans


def test_llm_finalize_errors_are_swallowed(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    """A failing set_status/end on an LLM span must not break the bridge."""
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)
    bad_span = bridge._spans[str(run_id)]
    bad_span.set_status = lambda _status: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    bad_span.end = lambda: (_ for _ in ()).throw(RuntimeError("end boom"))  # type: ignore[method-assign]

    bridge.on_llm_end(LLMResult(generations=[], llm_output=None), run_id=run_id)  # must not raise
    assert str(run_id) not in bridge._spans


def test_span_finalization_errors_are_swallowed(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    """A span whose set_status/end raise must not break the bridge."""
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("Broken"), {}, run_id=run_id)

    bad_span = bridge._spans[str(run_id)]
    bad_span.set_status = lambda _status: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    bad_span.end = lambda: (_ for _ in ()).throw(RuntimeError("end boom"))  # type: ignore[method-assign]

    bridge.on_chain_end({}, run_id=run_id)  # must not raise
    assert str(run_id) not in bridge._spans
