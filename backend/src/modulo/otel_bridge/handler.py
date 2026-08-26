"""LangGraph → OpenTelemetry bridge.

Translates LangChain/LangGraph callback events into OpenTelemetry spans with
correct parent-child propagation via run_id / parent_run_id.

Import contract (enforced by import-linter):
  This module MUST NOT import modulo.core.pipeline_engine, hitl_manager, or
  eval_engine. It is a pure instrumentation adapter with no business logic.
"""

import asyncio
import logging
import secrets
import threading
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult, LLMResult
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, Status, StatusCode, TraceFlags, set_span_in_context

from modulo.otel_bridge.trace_id import trace_id_int_for_thread

_log = logging.getLogger(__name__)


class LangGraphOtelBridge(BaseCallbackHandler):
    """Translates LangGraph/LangChain lifecycle callbacks into OTel spans.

    Attach to a LangGraph run via the ``callbacks`` parameter:

        bridge = LangGraphOtelBridge()
        graph.invoke(state, config={"callbacks": [bridge]})

    Spans are created with the active OTel tracer and linked to a parent span
    using ``parent_run_id`` when provided by LangGraph.

    Run-scoped deterministic trace ids (FAR-198): call :meth:`start_run_root`
    before streaming the graph and :meth:`end_run_root` in a ``finally``. The
    root span carries the deterministic trace id derived from the LangGraph
    thread id, and every bridge-created span inherits it — so the exported
    OTel spans carry the SAME ``trace_id`` the API reports on ``RunResponse``.
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
        # Maps str(run_id) → 16-hex span id for every span ever started by this
        # bridge instance (FAR-198 per-node span ids). The executor reads a
        # node's span id AFTER its chain-end event has fired, at which point
        # the span is already ended and popped from ``_spans`` — so the id is
        # recorded at start time. Bounded per run (one bridge per executor).
        self._span_ids: dict[str, str] = {}
        # The run root span (deterministic trace id) set by start_run_root().
        # Every bridge span inherits it as parent when no LangGraph parent
        # run_id is available.
        self._root_span: Span | None = None
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
    # Run root span (deterministic trace id, FAR-198)
    # ------------------------------------------------------------------

    def start_run_root(self, thread_id: str) -> Span:
        """Seed the run's deterministic OTel trace id via a root span.

        Creates a ``modulo.pipeline.run`` span whose SpanContext carries the
        deterministic trace id derived from *thread_id* and stores it as the
        bridge's root span, so every span the bridge creates during the run
        inherits it. Returns the root span; the caller should attach
        ``set_span_in_context(root)`` for the duration of the run and always
        call :meth:`end_run_root` in a ``finally``.
        """
        trace_id = trace_id_int_for_thread(thread_id)
        parent = set_span_in_context(
            NonRecordingSpan(
                SpanContext(
                    trace_id=trace_id,
                    span_id=self._new_span_id(),
                    is_remote=False,
                    # ``TraceFlags(SAMPLED)`` (not the bare int constant) so the
                    # value carries the ``sampled`` property the SDK sampler reads.
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
            )
        )
        root = self._tracer.start_span(
            "modulo.pipeline.run",
            context=parent,
            attributes={"modulo.run.thread_id": thread_id},
        )
        self._root_span = root
        return root

    def end_run_root(self) -> None:
        """End the run root span started by :meth:`start_run_root` (if any)."""
        root, self._root_span = self._root_span, None
        if root is None:
            return
        try:
            root.set_status(Status(StatusCode.OK))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to set status on run root span")
        try:
            root.end()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to end run root span")

    @staticmethod
    def _new_span_id() -> int:
        """Random 8-byte span id as an int (the OTel SpanContext type).

        Uses ``secrets`` because span ids must be unpredictable: a guessable
        span id lets a caller forge/collide trace ids and break the OTel
        trace-correlation invariant (B311).
        """
        return secrets.randbits(64)

    def span_id_for_run(self, run_id: Any) -> str | None:
        """Return the 16-hex span id for a LangChain run_id, or ``None``.

        Reads the live span when still open, else the id recorded at span
        start — so it works even after ``on_chain_end`` has ended the span.
        """
        key = str(run_id)
        with self._lock:
            span = self._spans.get(key)
            if span is not None:
                return f"{span.get_span_context().span_id:016x}"
            return self._span_ids.get(key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_span(
        self,
        name: str,
        run_id: UUID,
        parent_run_id: UUID | None,
        attributes: dict[str, str | int | float | bool] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        with self._lock:
            ctx: Context | None = None
            if parent_run_id is not None:
                parent = self._spans.get(str(parent_run_id))
                if parent is not None:
                    ctx = set_span_in_context(parent)
            if ctx is None and self._root_span is not None:
                # No LangGraph parent — inherit the run's deterministic trace
                # id from the run root span (FAR-198).
                ctx = set_span_in_context(self._root_span)
            existing = self._spans.pop(str(run_id), None)
            org_id = self._org_id
            pipeline_id = self._pipeline_id
        if existing is not None:
            try:
                existing.set_status(Status(StatusCode.OK))
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("Failed to finalize stale span %s", run_id)
            try:
                existing.end()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("Failed to end stale span %s", run_id)

        attrs = dict(attributes or {})
        if org_id is not None:
            attrs["organisation_id"] = org_id
        if pipeline_id is not None:
            attrs["pipeline_id"] = pipeline_id
        span = self._tracer.start_span(name, context=ctx, attributes=attrs)
        self._set_tags(span, tags)
        with self._lock:
            self._spans[str(run_id)] = span
            self._span_ids[str(run_id)] = f"{span.get_span_context().span_id:016x}"

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
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to finalize span %s", run_id)
        finally:
            try:
                span.end()
            except asyncio.CancelledError:
                raise
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
            span.set_attribute("langgraph.tags", tags)

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
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> Any:
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
        **_kwargs: Any,
    ) -> None:
        self._end_span(run_id)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        _parent_run_id: UUID | None = None,
        **_kwargs: Any,
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
        _metadata: dict[str, Any] | None = None,
        **_kwargs: Any,
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

    def _finalize_llm_span(
        self,
        run_id: UUID,
        llm_output: dict[str, Any] | None,
    ) -> None:
        with self._lock:
            span = self._spans.pop(str(run_id), None)
        if span is None:
            _log.warning("No active span found for run_id %s", run_id)
            return
        self._record_token_usage(span, llm_output)
        try:
            span.set_status(Status(StatusCode.OK))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to set status on LLM span %s", run_id)
        finally:
            try:
                span.end()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("Failed to end LLM span %s", run_id)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        _parent_run_id: UUID | None = None,
        **_kwargs: Any,
    ) -> None:
        self._finalize_llm_span(run_id, response.llm_output)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        _parent_run_id: UUID | None = None,
        **_kwargs: Any,
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
        _metadata: dict[str, Any] | None = None,
        **_kwargs: Any,
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
        _parent_run_id: UUID | None = None,
        **_kwargs: Any,
    ) -> None:
        self._finalize_llm_span(run_id, response.llm_output)

    def on_chat_model_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        _parent_run_id: UUID | None = None,
        **_kwargs: Any,
    ) -> None:
        self._end_span(run_id, error=error)

    # ------------------------------------------------------------------
    # Tool callbacks
    # ------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> Any:
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
        **_kwargs: Any,
    ) -> None:
        self._end_span(run_id)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        _parent_run_id: UUID | None = None,
        **_kwargs: Any,
    ) -> None:
        self._end_span(run_id, error=error)
