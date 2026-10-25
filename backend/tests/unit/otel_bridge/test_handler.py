"""Unit tests for LangGraphOtelBridge.

Uses OTel's InMemorySpanExporter — no network, no DB, no LangGraph process.
"""

import asyncio
import logging
import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from modulo.otel_bridge.handler import LangGraphOtelBridge, _anonymise_id, _error_category_for_export
from modulo.otel_bridge.trace_id import trace_id_int_for_thread


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def _bridge_with(exporter: InMemorySpanExporter, **kwargs: Any) -> LangGraphOtelBridge:
    """Build a bridge exporting to *exporter*, with optional constructor args."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return LangGraphOtelBridge(tracer=provider.get_tracer("test.langgraph"), **kwargs)


@pytest.fixture
def bridge(exporter: InMemorySpanExporter) -> LangGraphOtelBridge:
    return _bridge_with(exporter)


def _serialized(name: str) -> dict[str, Any]:
    return {"name": name, "id": ["langchain", name]}


def _boom(exc: BaseException):
    """Build a replacement for a span method that raises *exc* when called.

    The default span methods cannot be told to fail, so the defensive
    try/except blocks in the handler are exercised by swapping the method on
    the live span object. Reused by every failure/cancellation test below.
    """

    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise exc

    return _raise


def _span_named(spans: Any, fragment: str) -> Any:
    """Return the single exported span whose name contains *fragment*."""
    matches = [s for s in spans if fragment in s.name]
    assert len(matches) == 1, f"expected exactly one span matching {fragment!r}, got {[s.name for s in spans]}"
    return matches[0]


def _finished_chain_span(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter, name: str = "Node") -> Any:
    """Run one complete chain span and return the single exported span.

    The start→end→single-span dance is the setup for almost every attribute
    assertion below, so it lives here instead of being copy-pasted.
    """
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized(name), {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"expected exactly one finished span, got {[s.name for s in spans]}"
    return spans[0]


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


@pytest.mark.parametrize(
    ("node", "error", "expected_category"),
    [
        pytest.param("ErrNode", ValueError("boom"), "ValueError", id="value-error"),
        # A secret-bearing message must never reach an exported span.
        pytest.param(
            "SensitiveNode",
            RuntimeError("api_key=sk-secret-value\x00\x1f invalid"),
            "RuntimeError",
            id="secret-message",
        ),
    ],
)
def test_error_span_exports_only_the_error_category(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter, node: str, error: BaseException, expected_category: str
) -> None:
    """The error status carries the exception class name only — never the message.

    Only the error CATEGORY is exported (consent copy: "Error category
    frequencies (no error messages or stack traces)"; FAR-1131). The full
    detail must stay in local-only log sinks.
    """
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized(node), {}, run_id=run_id)
    bridge.on_chain_error(error, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == expected_category
    # The raw message must not appear anywhere on the span.
    assert str(error) not in (span.status.description or "")
    assert not [e for e in span.events if e.name == "exception"], "stack traces must not be exported"
    assert str(run_id) not in bridge._spans


def test_chain_attributes_set(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    span = _finished_chain_span(bridge, exporter, "SomeChain")

    assert span.attributes is not None
    assert span.attributes.get("langgraph.chain.name") == "SomeChain"


# ---------------------------------------------------------------------------
# Deterministic trace id (FAR-198)
# ---------------------------------------------------------------------------


def test_run_root_span_carries_deterministic_trace_id(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    thread_id = "org-uuid:run-uuid"
    root = bridge.start_run_root(thread_id)
    bridge.end_run_root()

    expected_trace_id = trace_id_int_for_thread(thread_id)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "modulo.pipeline.run"
    assert spans[0].context.trace_id == expected_trace_id
    assert root.is_recording() is False  # ended


def test_run_root_span_attributes(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    thread_id = "org-uuid:run-uuid"
    bridge.start_run_root(thread_id)
    bridge.end_run_root()

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("modulo.run.thread_id") == thread_id


def test_bridge_spans_inherit_run_root_trace_id(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    """Chain spans without a LangGraph parent inherit the run root's trace id."""
    thread_id = "org-uuid:run-uuid"
    bridge.start_run_root(thread_id)
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("MyNode"), {}, run_id=run_id)
    bridge.on_chain_end({}, run_id=run_id)
    bridge.end_run_root()

    expected_trace_id = trace_id_int_for_thread(thread_id)
    spans = exporter.get_finished_spans()
    chain = _span_named(spans, "MyNode")
    root = _span_named(spans, "modulo.pipeline.run")
    assert chain.context.trace_id == expected_trace_id
    assert expected_trace_id == root.context.trace_id
    assert chain.parent is not None
    assert chain.parent.span_id == root.context.span_id


def test_end_run_root_is_idempotent_without_start(bridge: LangGraphOtelBridge) -> None:
    bridge.end_run_root()  # must not raise
    bridge.end_run_root()
    assert bridge._root_span is None


def test_span_id_for_run_returns_hex_after_end(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("NodeA"), {}, run_id=run_id)
    # Record the live span id while open.
    live = bridge.span_id_for_run(run_id)
    assert live is not None
    assert len(live) == 16
    # The returned id must be the 16-hex form of the span's own span id.
    assert int(live, 16) == bridge._spans[str(run_id)].get_span_context().span_id
    bridge.on_chain_end({}, run_id=run_id)

    # After end the span is popped from _spans but the id is still readable.
    assert bridge.span_id_for_run(run_id) == live


def test_span_id_for_run_unknown_returns_none(bridge: LangGraphOtelBridge) -> None:
    assert bridge.span_id_for_run(uuid.uuid4()) is None


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

    child = _span_named(spans, "Child")
    parent = _span_named(spans, "Root")

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
    llm = _span_named(spans, "llm")
    chain = _span_named(spans, "chain")
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

    span = _finished_chain_span(bridge, exporter)

    assert span.attributes is not None
    assert span.attributes.get("organisation_id") == _anonymise_id("org-123")
    assert span.attributes.get("pipeline_id") == _anonymise_id("pipe-456")
    # Raw ids must never reach an exported span (FAR-1131).
    assert span.attributes.get("organisation_id") != "org-123"
    assert span.attributes.get("pipeline_id") != "pipe-456"


def test_run_context_stamps_merge_with_caller_attributes(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    """Identity stamps are merged onto the caller's attributes, not substituted.

    ``_span_attributes`` copies the caller's dict before adding the anonymised
    ids, so a regression that built a fresh dict would silently drop every
    semantic attribute (chain/tool/llm names) whenever a run context is set.
    """
    bridge.set_run_context(org_id="org-123", pipeline_id="pipe-456")

    span = _finished_chain_span(bridge, exporter, "Merged")

    attrs = span.attributes or {}
    assert attrs.get("langgraph.chain.name") == "Merged"
    assert attrs.get("organisation_id") == _anonymise_id("org-123")
    assert attrs.get("pipeline_id") == _anonymise_id("pipe-456")


def test_run_context_via_constructor(
    exporter: InMemorySpanExporter,
) -> None:
    """The constructor's org_id/pipeline_id must seed (anonymised) span attributes."""
    bridge = _bridge_with(exporter, org_id="org-ctor", pipeline_id="pipe-ctor")

    span = _finished_chain_span(bridge, exporter)

    assert span.attributes is not None
    assert span.attributes.get("organisation_id") == _anonymise_id("org-ctor")
    assert span.attributes.get("pipeline_id") == _anonymise_id("pipe-ctor")


def test_run_context_not_set_leaves_no_attributes(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    span = _finished_chain_span(bridge, exporter)

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
    early = _span_named(spans, "Early")
    late = _span_named(spans, "Late")
    # Context captured at span creation — early span has no attrs, late span does.
    assert "organisation_id" not in (early.attributes or {})
    assert (late.attributes or {}).get("organisation_id") == _anonymise_id("org-late")


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
    """A non-dict ``token_usage`` must be ignored, not crash and not be recorded."""
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)
    result = LLMResult(generations=[[]], llm_output={"token_usage": "not-a-dict"})
    bridge.on_llm_end(result, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    attrs = span.attributes or {}
    assert span.status.status_code == StatusCode.OK
    assert "langgraph.llm.prompt_tokens" not in attrs
    assert "langgraph.llm.completion_tokens" not in attrs
    assert "langgraph.llm.total_tokens" not in attrs


def test_llm_error_records_error_category_without_message(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    """An LLM failure must export the error category only — never its message."""
    run_id = uuid.uuid4()
    bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)
    bridge.on_llm_error(RuntimeError("rate limit"), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "RuntimeError"
    assert "rate limit" not in (span.status.description or "")
    assert str(run_id) not in bridge._spans


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
    assert not exporter.get_finished_spans()


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


@pytest.mark.parametrize(
    ("messages", "expected_count"),
    [
        pytest.param([[HumanMessage(content="a")]], 1, id="single-group-single-message"),
        pytest.param([[HumanMessage(content="a"), SystemMessage(content="b")]], 2, id="single-group-two-messages"),
        # Several groups arrive when a chain fans out; the count must be the sum,
        # not the length of the first group.
        pytest.param(
            [[HumanMessage(content="a")], [HumanMessage(content="b"), AIMessage(content="c")]],
            3,
            id="multiple-groups-are-summed",
        ),
        pytest.param([], 0, id="no-groups"),
        pytest.param([[]], 0, id="one-empty-group"),
    ],
)
def test_chat_model_start_counts_messages_across_groups(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter, messages: list[list[Any]], expected_count: int
) -> None:
    """``message_count`` must total every message in every group.

    Only ``len(messages)`` (group count) would under-report whenever LangGraph
    fans out, so the exported token-spend signal would silently be too low.
    """
    run_id = uuid.uuid4()
    bridge.on_chat_model_start(_serialized("gpt-4"), messages, run_id=run_id)
    bridge.on_chat_model_end(ChatResult(generations=[]), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("langgraph.llm.message_count") == expected_count


def test_chat_model_error_records_exception(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    _chat_start(bridge, run_id)
    bridge.on_chat_model_error(RuntimeError("provider down"), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "RuntimeError"
    assert "provider down" not in (span.status.description or "")
    assert str(run_id) not in bridge._spans


def test_chat_model_span_inherits_parent_context(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
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
    chat = _span_named(spans, "claude")
    chain = _span_named(spans, "chain")
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


def test_tool_lifecycle_exports_ok_span(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    """A tool call start→end pair exports exactly one OK span named for the tool."""
    run_id = uuid.uuid4()
    bridge.on_tool_start(_serialized("search"), "query", run_id=run_id)
    bridge.on_tool_end("result", run_id=run_id)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "langgraph.tool.search"
    assert spans[0].status.status_code == StatusCode.OK
    assert (spans[0].attributes or {}).get("langgraph.tool.name") == "search"
    assert str(run_id) not in bridge._spans


def test_tool_error_records_error_category_without_message(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter
) -> None:
    """A tool failure exports the error category only — never the tool's message."""
    run_id = uuid.uuid4()
    bridge.on_tool_start(_serialized("search"), "query", run_id=run_id)
    bridge.on_tool_error(ConnectionError("timeout"), run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "ConnectionError"
    assert "timeout" not in (span.status.description or "")
    assert str(run_id) not in bridge._spans


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_end_without_start_does_not_raise(bridge: LangGraphOtelBridge, caplog: pytest.LogCaptureFixture) -> None:
    """An end callback for an unknown run_id is a logged no-op.

    The warning is asserted so the test cannot pass by the end callbacks
    silently never reaching their span-lookup branch.
    """
    run_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger="modulo.otel_bridge.handler"):
        bridge.on_chain_end({}, run_id=run_id)
        bridge.on_llm_end(LLMResult(generations=[], llm_output=None), run_id=run_id)
        bridge.on_tool_end("x", run_id=run_id)
        bridge.on_chat_model_end(ChatResult(generations=[]), run_id=run_id)

    assert not bridge._spans
    assert len([r for r in caplog.records if "No active span found" in r.message]) == 4


def test_error_without_start_does_not_raise(bridge: LangGraphOtelBridge, caplog: pytest.LogCaptureFixture) -> None:
    """Error callbacks for unknown run_ids must be logged no-ops, not raise."""
    run_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger="modulo.otel_bridge.handler"):
        bridge.on_chain_error(ValueError("boom"), run_id=run_id)
        bridge.on_llm_error(ValueError("boom"), run_id=run_id)
        bridge.on_chat_model_error(ValueError("boom"), run_id=run_id)
        bridge.on_tool_error(ValueError("boom"), run_id=run_id)

    assert not bridge._spans
    assert len([r for r in caplog.records if "No active span found" in r.message]) == 4


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

    assert not bridge._spans
    assert len(exporter.get_finished_spans()) == 3


@pytest.mark.parametrize(
    ("serialized", "expected_name"),
    [
        pytest.param({"id": ["a", "b", "MyClass"]}, "langgraph.chain.MyClass", id="name-falls-back-to-id-path"),
        pytest.param(None, "langgraph.chain.unknown", id="serialized-is-none"),
        pytest.param(
            {"name": "ExplicitName", "id": ["ignored", "Fallback"]},
            "langgraph.chain.ExplicitName",
            id="name-wins-over-id",
        ),
        pytest.param(
            {"name": "", "id": ["a", "b", "IdName"]}, "langgraph.chain.IdName", id="empty-name-falls-back-to-id"
        ),
        pytest.param({}, "langgraph.chain.unknown", id="empty-dict"),
        # ``id`` is not a list — must not raise and must fall back to "unknown".
        pytest.param({"id": "not-a-list"}, "langgraph.chain.unknown", id="non-list-id"),
    ],
)
def test_serialized_name_resolution(
    bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter, serialized: dict[str, Any] | None, expected_name: str
) -> None:
    """The span name is derived from LangChain's ``serialized`` dict.

    ``serialized`` is frequently ``None`` or partial in production, so each
    fallback arm must produce a stable, non-crashing name.
    """
    run_id = uuid.uuid4()
    bridge.on_chain_start(serialized, {}, run_id=run_id)  # type: ignore[arg-type]
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.name == expected_name


def test_tags_set_on_span(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    run_id = uuid.uuid4()
    bridge.on_chain_start(_serialized("Tagged"), {}, run_id=run_id, tags=["alpha", "beta"])
    bridge.on_chain_end({}, run_id=run_id)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("langgraph.tags") == ("alpha", "beta")


def test_no_tags_leaves_no_tags_attribute(bridge: LangGraphOtelBridge, exporter: InMemorySpanExporter) -> None:
    span = _finished_chain_span(bridge, exporter, "Untagged")

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
    first = _span_named(spans, "First")
    second = _span_named(spans, "Second")
    assert first.status.status_code == StatusCode.OK
    assert second.status.status_code == StatusCode.OK


# Each scenario below opens a span of one kind, breaks one of the two span
# finalization methods, then drives the matching end/error callback. Every
# non-CancelledError failure must be swallowed and logged; CancelledError must
# propagate so an in-flight shutdown is not masked.
_FINALIZE_KINDS = ["chain", "llm", "stale"]


def _open_span(bridge: LangGraphOtelBridge, kind: str, run_id: uuid.UUID) -> None:
    """Open a chain/llm span, or leave a chain span open as a stale one."""
    if kind == "chain":
        bridge.on_chain_start(_serialized("Broken"), {}, run_id=run_id)
    elif kind == "llm":
        bridge.on_llm_start(_serialized("gpt-4"), ["Hello"], run_id=run_id)
    else:
        # A span that will be superseded when the same run_id starts again.
        bridge.on_chain_start(_serialized("First"), {}, run_id=run_id)


def _drive_end(bridge: LangGraphOtelBridge, kind: str, run_id: uuid.UUID) -> None:
    """Drive the callback that ends the span opened by :func:`_open_span`."""
    if kind == "chain":
        bridge.on_chain_end({}, run_id=run_id)
    elif kind == "llm":
        bridge.on_llm_end(LLMResult(generations=[], llm_output=None), run_id=run_id)
    else:
        # Re-starting the same run_id finalizes the stale span.
        bridge.on_chain_start(_serialized("Second"), {}, run_id=run_id)


@pytest.mark.parametrize("kind", _FINALIZE_KINDS)
@pytest.mark.parametrize("broken_method", ["set_status", "end"])
def test_span_finalization_errors_are_swallowed(
    bridge: LangGraphOtelBridge,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    broken_method: str,
) -> None:
    """A span whose set_status/end raises must not break the bridge.

    Instrumentation must never break the pipeline it instruments: the failure
    is logged and the run continues.
    """
    run_id = uuid.uuid4()
    _open_span(bridge, kind, run_id)
    broken_span = bridge._spans[str(run_id)]
    setattr(broken_span, broken_method, _boom(RuntimeError("boom")))

    with caplog.at_level(logging.ERROR, logger="modulo.otel_bridge.handler"):
        _drive_end(bridge, kind, run_id)  # must not raise

    # The broken span must no longer be the live one (dropped for chain/llm,
    # superseded for a stale span), so a later lookup cannot return it.
    assert bridge._spans.get(str(run_id)) is not broken_span
    assert any("Failed to" in r.message for r in caplog.records), "the swallowed failure must be logged"


@pytest.mark.parametrize("kind", _FINALIZE_KINDS)
@pytest.mark.parametrize("broken_method", ["set_status", "end"])
def test_span_finalization_cancellation_propagates(
    bridge: LangGraphOtelBridge,
    kind: str,
    broken_method: str,
) -> None:
    """A CancelledError during finalization must propagate, not be swallowed."""
    run_id = uuid.uuid4()
    _open_span(bridge, kind, run_id)
    setattr(bridge._spans[str(run_id)], broken_method, _boom(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        _drive_end(bridge, kind, run_id)


# ---------------------------------------------------------------------------
# Anonymisation and error-category helpers
# ---------------------------------------------------------------------------
# These pin the two exported-safety primitives the handlers above depend on.
# They live here, beside the code under test, rather than in the telemetry
# toggle suite where they were originally filed.


def test_anonymise_id_hashes_value() -> None:
    """Ids are hashed to a stable 16-hex digest, distinct per input."""
    result = _anonymise_id("org-123")
    assert result is not None
    assert len(result) == 16
    # Same input → same output (deterministic across exports).
    assert _anonymise_id("org-123") == result
    # Different input → different output.
    assert _anonymise_id("org-456") != result


def test_anonymise_id_none_returns_none() -> None:
    assert _anonymise_id(None) is None


def test_error_category_exported_not_message() -> None:
    result = _error_category_for_export(RuntimeError("secret api key sk-123"))
    assert result == "RuntimeError"
    assert "secret" not in result


def test_error_category_for_custom_exception_class() -> None:
    class BillingError(Exception):
        pass

    assert _error_category_for_export(BillingError("card declined")) == "BillingError"
