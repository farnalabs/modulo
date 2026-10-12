"""Shared LLM-judge resolver and sync-to-async model-backend bridge (FAR-1652).

The judge resolver used to live in ``node_runner``'s private namespace even
though two call sites need it: the executor's post-node eval path (FAR-315)
and the node-runner HITL-gate eval path (FAR-307).  Both hand it to
:func:`modulo.core.pipeline_engine.eval_persist_order.run_evals_persist_before_decide`
as the ``resolve_llm_judge`` callback, so it now lives here and both modules
import it.

The module also owns the two bounds that keep one slow or hung judge from
wedging a run:

* :data:`BACKEND_INVOKE_TIMEOUT_SECONDS` bounds the whole resolve+invoke
  sequence - ``hub.get(backend_id)`` AND ``backend.invoke`` - under a single
  ``asyncio.wait_for``.  Cancelling the coroutine is what actually frees the
  worker thread: an un-cancelled hung resolve or invoke would occupy the
  ``max_workers=1`` bridge pool forever, because a caller-side bridge timeout
  gets ``future.cancel() is False`` on an already-running future, and
  fail-close every later judge call until the process restarts.
* :data:`SYNC_BRIDGE_TIMEOUT_SECONDS` bounds the caller-side wait in
  :func:`run_coroutine_sync` (``future.result(timeout=...)``) as a backstop
  for a coroutine that does not honour cancellation.

Either timeout surfaces as a FAIL-CLOSED eval result (``passed=False``,
``score=0.0`` with a clear detail): a judge decides a gate, so output that
was never judged must not pass.

Type note (FAR-1652 review): the judge callables built below annotate their
``eval_def`` parameter as
:class:`~modulo.core.eval_engine.EvalDefinition` because that is what the
:class:`~modulo.core.eval_engine.LLMJudgeCallable` protocol declares, while
both call sites actually pass the duck-typed
:class:`~modulo.core.pipeline_engine.eval_persist_order.EvalDefDTO` (FAR-1100
cutover).  Only ``eval_def.config`` is read, and ``EvalDefDTO`` mirrors the
legacy ``EvalDefinition`` attribute names by design, so the annotation is
intentionally looser than the runtime payload rather than an inaccurate one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import uuid
from collections.abc import Coroutine
from typing import Any

from langchain_core.messages import HumanMessage

from modulo.core.eval_engine import EvalDefinition, EvalType, LLMJudgeCallable
from modulo.core.pipeline_engine.eval_persist_order import EvalDefDTO

_log = logging.getLogger(__name__)

__all__ = [
    "BACKEND_INVOKE_TIMEOUT_SECONDS",
    "SYNC_BRIDGE_TIMEOUT_SECONDS",
    "build_llm_judge_callable",
    "invoke_backend",
    "resolve_llm_judge_callable",
    "run_coroutine_sync",
]

# Per-request bound on the whole ``hub.get`` + ``backend.invoke`` sequence.  A
# judge verdict is a single short completion, so 60s sits an order of magnitude
# above normal latency while staying well under the default pipeline node
# timeout (300s) - a hung judge fails closed INSIDE the node's own budget
# instead of tripping the node watchdog.  Read as a module global at call time
# so tests can monkeypatch it.
BACKEND_INVOKE_TIMEOUT_SECONDS = 60.0

# Caller-side bound on the sync-to-async bridge wait.  The 15s margin over
# BACKEND_INVOKE_TIMEOUT_SECONDS makes the inner ``asyncio.wait_for``
# cancellation path (which frees the worker thread) the normal timeout; this
# bound only fires when a coroutine refuses to cancel.  Also read at call time.
SYNC_BRIDGE_TIMEOUT_SECONDS = 75.0


def _timeout_detail() -> str:
    """Fail-closed detail for a judge timeout (reads the bounds at call time)."""
    return (
        "LLM judge model backend call timed out "
        f"(invoke bound {BACKEND_INVOKE_TIMEOUT_SECONDS:g}s, "
        f"bridge bound {SYNC_BRIDGE_TIMEOUT_SECONDS:g}s); "
        "failing closed with score 0.0"
    )


async def invoke_backend(
    hub: Any,
    backend_id: uuid.UUID,
    messages: list[Any],
) -> Any:
    """Resolve a backend from the hub and invoke it asynchronously.

    The WHOLE sequence - ``hub.get`` resolve then ``backend.invoke`` - sits
    under one :data:`BACKEND_INVOKE_TIMEOUT_SECONDS` ``asyncio.wait_for``
    (FAR-1652).  Bounding only the invoke left ``hub.get`` outside the bound:
    a hung resolve (e.g. a stalled failover-event emit) kept the coroutine
    running, so the caller-side bridge timeout's ``future.cancel()`` returned
    ``False`` and the ``max_workers=1`` bridge thread was never freed - every
    later judge call then blocked for the full bridge bound and failed closed
    until a process restart.

    Either hang is cancelled (freeing the bridge worker thread) and the
    resulting ``TimeoutError`` propagates to the caller.
    """

    async def _resolve_and_invoke() -> Any:
        backend = await hub.get(backend_id)
        return await backend.invoke(messages)

    return await asyncio.wait_for(
        _resolve_and_invoke(),
        timeout=BACKEND_INVOKE_TIMEOUT_SECONDS,
    )


_JUDGE_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def run_coroutine_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run an async coroutine to completion from a sync context.

    ``make_hitl_review_fn`` runs inside an already-running asyncio event loop,
    so ``asyncio.run`` and ``loop.run_until_complete`` both raise "This event
    loop is already running". The LLMJudgeCallable protocol is synchronous, so
    we bridge sync -> async by executing the coroutine on a dedicated event
    loop in a worker thread and blocking on the result.

    The wait is bounded by :data:`SYNC_BRIDGE_TIMEOUT_SECONDS` (FAR-1652): the
    bridge runs on the pipeline event loop, so an unbounded ``future.result()``
    would block the loop and - with ``max_workers=1`` - serialize every later
    judge call behind one hung request. On timeout the queued future is
    cancelled (and its coroutine closed); a running one is left to the inner
    :func:`invoke_backend` bound, which cancels it inside the worker thread.
    """
    global _JUDGE_EXECUTOR
    if _JUDGE_EXECUTOR is None:
        _JUDGE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="llm-judge",
        )

    def _run() -> Any:
        return asyncio.run(coro)

    future: concurrent.futures.Future[Any] = _JUDGE_EXECUTOR.submit(_run)
    try:
        return future.result(timeout=SYNC_BRIDGE_TIMEOUT_SECONDS)
    except TimeoutError:
        if future.done():
            # The TimeoutError came FROM the coroutine (the inner invoke bound
            # fired inside the worker thread) - it is already logged there and
            # the worker thread is free. Just propagate it.
            raise
        # The bridge bound fired: the coroutine is still queued or running.
        if future.cancel():
            # Never started - close the coroutine so it does not warn on GC.
            coro.close()
        _log.error(
            "llm_judge.bridge_timeout",
            extra={
                "timeout_seconds": SYNC_BRIDGE_TIMEOUT_SECONDS,
                "detail": _timeout_detail(),
            },
        )
        raise


def build_llm_judge_callable(
    hub: Any,
    backend_id_str: str,
) -> LLMJudgeCallable:
    """Build a synchronous LLM judge callable backed by a model backend.

    The ``LLMJudgeCallable`` protocol is synchronous, but ``make_hitl_review_fn``
    runs inside an already-running asyncio event loop. We bridge sync -> async
    by running the async backend invoke on a dedicated event loop in a worker
    thread via :func:`run_coroutine_sync`.

    ``backend_id_str`` comes from ``eval_def.config["model_backend_id"]`` -
    unvalidated free-form JSON, so a typo or stale manual edit can put any
    string here. A malformed id does NOT raise: raising ``ValueError`` out of
    ``run_evals_persist_before_decide``'s per-eval loop would abort every
    sibling eval and terminalise the run via the generic error path. Instead
    the returned judge fails THIS eval closed with a clear configuration
    message (``passed=False``), so the block/warn decision still fires and
    the remaining evals in the loop still run.

    A timeout (either bound above) is handled the same way: fail THIS eval
    closed with a clear detail instead of raising or blocking unbounded.
    """
    try:
        backend_id = uuid.UUID(backend_id_str)
    except (ValueError, TypeError) as exc:
        detail = (
            f"llm_judge eval has a malformed model_backend_id {backend_id_str!r}: "
            f"not a valid UUID ({exc}); fix the eval definition's "
            "config['model_backend_id']"
        )
        _log.error(
            "llm_judge.malformed_model_backend_id",
            extra={"model_backend_id": backend_id_str, "detail": detail},
        )

        def _fail_closed_judge(
            output: dict[str, Any],
            eval_def: EvalDefinition,
        ) -> dict[str, Any]:
            return {"passed": False, "score": 0.0, "detail": detail}

        return _fail_closed_judge

    def _judge(
        output: dict[str, Any],
        eval_def: EvalDefinition,
    ) -> dict[str, Any]:
        field = eval_def.config.get("field", "")
        content = output.get(field, "")
        prompt = (
            "Evaluate the following output and return JSON with keys passed "
            "(bool), score (float 0-1), detail (str).\n\n"
            f"Output:\n{content}"
        )
        messages = [HumanMessage(content=prompt)]
        try:
            response = run_coroutine_sync(invoke_backend(hub, backend_id, messages))
        except TimeoutError:
            detail = _timeout_detail()
            _log.error(
                "llm_judge.timeout",
                extra={
                    "model_backend_id": str(backend_id),
                    "detail": detail,
                },
            )
            # Fail closed: an unjudged output must not pass a gate.
            return {"passed": False, "score": 0.0, "detail": detail}
        text = response.content if hasattr(response, "content") else str(response)
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = {"passed": False, "score": 0.0, "detail": text}
        detail = parsed.get("detail", "")
        return {
            "passed": bool(parsed.get("passed", False)),
            "score": parsed.get("score"),
            "detail": str(detail) if detail is not None else "",
        }

    return _judge


def resolve_llm_judge_callable(eval_def: EvalDefDTO) -> LLMJudgeCallable | None:
    """Resolve the LLM judge callable for an eval definition, if configured."""
    if eval_def.eval_type == EvalType.LLM_JUDGE and eval_def.config.get("model_backend_id"):
        from modulo.core.pipeline_engine.decorator import get_model_backend_hub

        hub = get_model_backend_hub()
        if hub is not None:
            return build_llm_judge_callable(hub, str(eval_def.config["model_backend_id"]))
    return None
