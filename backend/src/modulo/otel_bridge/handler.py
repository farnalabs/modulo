"""LangGraph → OpenTelemetry bridge.

Translates LangChain/LangGraph callback events into OpenTelemetry spans with
correct parent-child propagation via run_id / parent_run_id.

Import contract (enforced by import-linter):
  This module MUST NOT import modulo.core.pipeline_engine, hitl_manager, or
  eval_engine. It is a pure instrumentation adapter with no business logic.
"""

import logging
import threading
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult, LLMResult
from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

_log = logging.getLogger(__name__)


class LangGraphOtelBridge(BaseCallbackHandler):
    """Translates LangGraph/LangChain lifecycle callbacks into OTel spans.

    Attach to a LangGraph run via the ``callbacks`` parameter:

        bridge = LangGraphOtelBridge()
        graph.invoke(state, config={"callbacks": [bridge]})

    Spans are created with the active OTel tracer and linked to a parent span
    using ``parent_run_id`` when provided by LangGraph.
    """

    def __init__(
        self,
        tracer: trace.Tracer | None = None,
        tracer_name: str = "modulo.langgraph",
        org_id: str | None = None,
        pipeline_id: str | None = None,
    ) -> None:
        super().__init__()
        # Accept an injected tracer so tests can provide a TracerProvider with
        # an InMemorySpanExporter without mutating the global OTel state.
        self._tracer: trace.Tracer = tracer or trace.get_tracer(tracer_name)
        # Maps str(run_id) → active Span for that run.  Entries are removed
        # on end/error so the dict stays bounded to the depth of the call stack.
        self._spans: dict[str, Span] = {}
        # Protects _spans from concurrent access in async/coroutine contexts.
        self._lock = threading.Lock()
        # Run context — set by set_run_context() or constructor.
        self._org_id: str | None = org_id
        self._pipeline_id: str | None = pipeline_id

    def set_run_context(self, org_id: str, pipeline_id: str) -> None:
        with self._lock:
            self._org_id = org_id
            self._pipeline_id = pipeline_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parent_context(self, parent_run_id: UUID | None) -> context_api.Context | None:
        if parent_run_id is None:
            return None
        with self._lock:
            parent = self._spans.get(str(parent_run_id))
        if parent is None:
            return None
        return trace.set_span_in_context(parent)

    def _start_span(
        self,
        name: str,
        run_id: UUID,
        parent_run_id: UUID | None,
        attributes: dict[str, str | int | float | bool] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        # End any existing span for this run_id before creating a new one
        with self._lock:
            existing = self._spans.pop(str(run_id), None)
        if existing is not None:
            try:
                existing.set_status(Status(StatusCode.OK))
            except Exception:  # noqa: S110
                pass  # orphaned span; nothing useful to log
            try:
                existing.end()
            except Exception:  # noqa: S110
                pass  # orphaned span; nothing useful to log

        ctx = self._parent_context(parent_run_id)
        attrs = dict(attributes or {})
        if self._org_id is not None:
            attrs["organisation_id"] = self._org_id
        if self._pipeline_id is not None:
            attrs["pipeline_id"] = self._pipeline_id
        span = self._tracer.start_span(name, context=ctx, attributes=attrs)
        self._set_tags(span, tags)
        with self._lock:
            if existing is not None:
                try:
                    existing.set_status(Status(StatusCode.OK))
                except Exception:  # noqa: S110
                    pass  # orphaned span; nothing useful to log
                try:
                    existing.end()
                except Exception:  # noqa: S110
                    pass  # orphaned span; nothing useful to log
            self._spans[str(run_id)] = span

    def _end_span(self, run_id: UUID, *, error: BaseException | None = None) -> None:
        with self._lock:
            span = self._spans.pop(str(run_id), None)
        if span is None:
            _log.warning("No active span found for run_id %s", run_id)
            return
        try:
            if error is not None:
                span.set_status(Status(StatusCode.ERROR, str(error)))
                span.record_exception(error)
            else:
                span.set_status(Status(StatusCode.OK))
        except Exception:
            _log.exception("Failed to finalize span %s", run_id)
        finally:
            try:
                span.end()
            except Exception:
                _log.exception("Failed to end span %s", run_id)

    @staticmethod
    def _serialized_name(serialized: dict[str, Any] | None) -> str:
        if not serialized:
            return "unknown"
        # LangChain serialized dicts may use "name" or the last element of "id"
        name = serialized.get("name")
        if name is not None and name != "":
            return str(name)
        id_path = serialized.get("id")
        if id_path and isinstance(id_path, list):
            return str(id_path[-1])
        return "unknown"

    @staticmethod
    def _set_tags(span: Span, tags: list[str] | None) -> None:
        if tags:
            span.set_attribute("langgraph.tags", list(tags))

    @staticmethod
    def _record_token_usage(span: Span, llm_output: dict[str, Any] | None) -> None:
        if not llm_output:
            return
        usage = llm_output.get("token_usage") or {}
        if not isinstance(usage, dict):
            return
        for attr, key in (
            ("langgraph.llm.prompt_tokens", "prompt_tokens"),
            ("langgraph.llm.completion_tokens", "completion_tokens"),
            ("langgraph.llm.total_tokens", "total_tokens"),
        ):
            val = usage.get(key)
            if isinstance(val, int):
                span.set_attribute(attr, val)

    # ------------------------------------------------------------------
    # Chain (graph / node) callbacks
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        name = self._serialized_name(serialized)
        self._start_span(
            f"langgraph.chain.{name}",
            run_id,
            parent_run_id,
            {"langgraph.chain.name": name},
            tags=tags,
        )

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._end_span(run_id)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._end_span(run_id, error=error)

    # ------------------------------------------------------------------
    # LLM callbacks
    # ------------------------------------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        name = self._serialized_name(serialized)
        self._start_span(
            f"langgraph.llm.{name}",
            run_id,
            parent_run_id,
            {
                "langgraph.llm.name": name,
                "langgraph.llm.prompt_count": len(prompts),
            },
            tags=tags,
        )

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with self._lock:
            span = self._spans.get(str(run_id))
        if span is not None:
            self._record_token_usage(span, response.llm_output)
        self._end_span(run_id)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._end_span(run_id, error=error)

    # ------------------------------------------------------------------
    # Chat model callbacks (used by BaseChatModel — all production backends)
    # ------------------------------------------------------------------
    # BaseChatModel subclasses fire on_chat_model_start/end/error rather than
    # on_llm_start/end/error.  These handlers mirror the LLM ones so that
    # Anthropic, OpenAI, and Ollama backends produce OTel spans.

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        name = self._serialized_name(serialized)
        msg_count = sum(len(msgs) for msgs in messages) if messages else 0
        self._start_span(
            f"langgraph.llm.{name}",
            run_id,
            parent_run_id,
            {
                "langgraph.llm.name": name,
                "langgraph.llm.message_count": msg_count,
            },
            tags=tags,
        )

    def on_chat_model_end(
        self,
        response: ChatResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        with self._lock:
            span = self._spans.get(str(run_id))
        if span is not None:
            self._record_token_usage(span, response.llm_output)
        self._end_span(run_id)

    def on_chat_model_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._end_span(run_id, error=error)

    # ------------------------------------------------------------------
    # Tool callbacks
    # ------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        name = self._serialized_name(serialized)
        self._start_span(
            f"langgraph.tool.{name}",
            run_id,
            parent_run_id,
            {"langgraph.tool.name": name},
            tags=tags,
        )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._end_span(run_id)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._end_span(run_id, error=error)
