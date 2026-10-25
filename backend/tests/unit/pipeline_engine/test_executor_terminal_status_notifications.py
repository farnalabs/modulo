"""Terminal-status notifications wired by FAR-2138.

``run_stalled``, ``budget_exceeded`` and ``eval_blocked`` were declared notifier
constants with full in-app render configs but NO ``dispatch_event`` call site, so
they could never fire — an operator could subscribe to ``run_stalled`` /
``budget_exceeded`` (both webhook-subscribable) and never hear anything.

These tests pin the wiring in the executor's ONE shared terminalization tail and
prove the emitted payload renders through the REAL mapper templates, not merely
that dispatch was called:

* stalled / budget_exceeded are resolved from the COMMITTED run row, so the
  per-agent token budget override applied inside ``finalize_cost`` is observed
  even when the pre-finalize status was something else;
* eval_blocked is the status+error_code pair ``eval_failed`` / ``eval_blocked``;
* a failure-isolated dispatch never blocks or alters the terminal write.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.notifier.event_mapper import (
    _ACTION_URL_TEMPLATES,
    _BODY_TEMPLATES,
    _TITLE_TEMPLATES,
    NotificationEventMapper,
)
from modulo.core.pipeline_engine.executor import PipelineExecutor

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


@asynccontextmanager
async def _begin_cm() -> Any:
    yield None


def _make_session_factory() -> MagicMock:
    session = AsyncMock(spec=AsyncSession)
    # A fresh CM per call: finalization enters more than one session on the
    # same factory, and an async-generator CM cannot be entered twice.
    session.begin = MagicMock(side_effect=lambda: _begin_cm())

    @asynccontextmanager
    async def _ctx() -> Any:
        yield session

    return MagicMock(side_effect=lambda: _ctx())


def _run_row(*, status: str, error_code: str | None) -> MagicMock:
    run = MagicMock()
    run.status = status
    run.error_code = error_code
    return run


def _finalize_args(
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    *,
    final_status: str,
    error_code: str | None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "org_id": org_id,
        "pipeline_id": pipeline_id,
        "node_type_map": {},
        "final_status": final_status,
        "error_code": error_code,
        "error_detail": None,
        "node_token_usage": {},
        "completed_node_outputs": {},
        "node_ids": set(),
    }


def _executor_with_notifier(notifier: Any) -> PipelineExecutor:
    executor = PipelineExecutor(MagicMock(), notifier=notifier)
    executor._session_factory = _make_session_factory()  # type: ignore[assignment]
    # Keep the helper's DB read out of every dispatch test.
    executor._notification_pipeline_name = AsyncMock(return_value="My Pipeline")  # type: ignore[method-assign]
    return executor


async def _drive_finalize(
    executor: PipelineExecutor,
    *,
    final_run: MagicMock,
    final_status: str,
    error_code: str | None,
    run_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
) -> MagicMock:
    """Drive ``_finalize_run_after_stream`` with its DB-facing tail patched out."""
    run_id = run_id or uuid.uuid4()
    pipeline_id = pipeline_id or uuid.uuid4()
    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_load_stored_node_columns", new=AsyncMock(return_value=({}, {}))),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_seal_run_teardown", new=AsyncMock()),
        patch.object(executor, "_record_eval_blocked_audit", new=AsyncMock()),
        patch.object(executor, "_compensate_blocked_run_best_effort", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.finalize_cost", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=final_run)),
    ):
        returned = await executor._finalize_run_after_stream(
            **_finalize_args(run_id, _ORG_ID, pipeline_id, final_status=final_status, error_code=error_code)
        )
    assert returned is final_run
    return final_run


def _render(event_type: str, payload: dict[str, Any]) -> tuple[str, str, str | None]:
    """Render through the REAL mapper templates — the same path a notification takes."""
    mapper = NotificationEventMapper()
    title = mapper._resolve_template(_TITLE_TEMPLATES[event_type], payload)
    body = mapper._resolve_template(_BODY_TEMPLATES[event_type], payload)
    action = _ACTION_URL_TEMPLATES[event_type]
    action_url = mapper._resolve_template(action, payload) if action is not None else None
    return title, body, action_url


@pytest.mark.asyncio
async def test_finalize_dispatches_run_stalled_with_a_renderable_payload() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)
    run_id = uuid.uuid4()
    final_run = _run_row(status="stalled", error_code="executor_stalled")

    await _drive_finalize(
        executor,
        final_run=final_run,
        final_status="stalled",
        error_code="executor_stalled",
        run_id=run_id,
    )

    assert notifier.dispatch_event.await_count == 1
    kwargs = notifier.dispatch_event.await_args.kwargs
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["event_type"] == "run_stalled"
    assert kwargs["run_id"] == run_id
    assert kwargs["payload"] == {
        "run_id": str(run_id),
        "error_code": "executor_stalled",
        "pipeline_name": "My Pipeline",
    }
    title, body, action_url = _render("run_stalled", kwargs["payload"])
    assert title == "Run stalled — My Pipeline"
    assert "My Pipeline" in body
    assert action_url == f"/runs/{run_id}"


@pytest.mark.asyncio
async def test_finalize_dispatches_budget_exceeded_read_from_the_committed_row() -> None:
    """The budget override lands INSIDE finalize_cost, so the pre-finalize
    ``final_status`` is not the signal — the committed row is."""
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)
    run_id = uuid.uuid4()
    # A failed run that also blew its per-agent token budget finalizes
    # ``budget_exceeded``: the committed row differs from the passing local.
    # ``sandbox.no_output_json`` is retryable, so it never arms the unrelated
    # ``run_failed`` dispatch.
    final_run = _run_row(status="budget_exceeded", error_code="budget_exceeded")

    await _drive_finalize(
        executor,
        final_run=final_run,
        final_status="failed",
        error_code="sandbox.no_output_json",
        run_id=run_id,
    )

    assert notifier.dispatch_event.await_count == 1
    kwargs = notifier.dispatch_event.await_args.kwargs
    assert kwargs["event_type"] == "budget_exceeded"
    assert kwargs["run_id"] == run_id
    assert kwargs["payload"] == {
        "run_id": str(run_id),
        "error_code": "budget_exceeded",
        "pipeline_name": "My Pipeline",
    }
    title, body, action_url = _render("budget_exceeded", kwargs["payload"])
    assert title == "Budget exceeded — My Pipeline"
    assert "My Pipeline" in body
    assert action_url == f"/runs/{run_id}"


@pytest.mark.asyncio
async def test_finalize_dispatches_eval_blocked_for_a_guardrail_blocked_run() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)
    run_id = uuid.uuid4()
    final_run = _run_row(status="eval_failed", error_code="eval_blocked")

    await _drive_finalize(
        executor,
        final_run=final_run,
        final_status="eval_failed",
        error_code="eval_blocked",
        run_id=run_id,
    )

    assert notifier.dispatch_event.await_count == 1
    kwargs = notifier.dispatch_event.await_args.kwargs
    assert kwargs["event_type"] == "eval_blocked"
    assert kwargs["run_id"] == run_id
    assert kwargs["payload"] == {
        "run_id": str(run_id),
        "error_code": "eval_blocked",
        "pipeline_name": "My Pipeline",
    }
    title, body, action_url = _render("eval_blocked", kwargs["payload"])
    assert title == "Eval blocked — My Pipeline"
    assert "My Pipeline" in body
    assert action_url == f"/runs/{run_id}"


@pytest.mark.asyncio
async def test_finalize_dispatches_nothing_for_a_completed_run() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)

    await _drive_finalize(
        executor,
        final_run=_run_row(status="complete", error_code=None),
        final_status="complete",
        error_code=None,
    )

    notifier.dispatch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_dispatches_nothing_for_a_non_block_eval_failure() -> None:
    """Only the ``eval_blocked`` pair carries the event — a plain eval failure does not."""
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)

    await _drive_finalize(
        executor,
        final_run=_run_row(status="eval_failed", error_code="eval.failed"),
        final_status="eval_failed",
        error_code="eval.failed",
    )

    notifier.dispatch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_skips_without_a_notifier() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = PipelineExecutor(MagicMock(), notifier=notifier)
    executor._notifier = None

    await executor._dispatch_terminal_status_notification(
        org_id=_ORG_ID,
        run_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        final_run=_run_row(status="stalled", error_code="executor_stalled"),
    )

    notifier.dispatch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_failure_is_isolated_and_logged() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock(side_effect=RuntimeError("boom"))
    executor = _executor_with_notifier(notifier)

    with patch("modulo.core.pipeline_engine.executor._log.exception") as mock_exc:
        await executor._dispatch_terminal_status_notification(
            org_id=_ORG_ID,
            run_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            final_run=_run_row(status="stalled", error_code="executor_stalled"),
        )

    assert notifier.dispatch_event.await_count == 1
    mock_exc.assert_called_once()
    assert mock_exc.call_args.args[0] == "pipeline.terminal_status_notification_failed"


@pytest.mark.asyncio
async def test_payload_omits_pipeline_name_when_the_lookup_fails() -> None:
    """A failed name lookup degrades the rendered title to the mapper's own
    ``[unknown]`` fallback rather than a ``KeyError`` or a lost notification."""
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)
    executor._notification_pipeline_name = AsyncMock(return_value=None)  # type: ignore[method-assign]
    run_id = uuid.uuid4()

    await executor._dispatch_terminal_status_notification(
        org_id=_ORG_ID,
        run_id=run_id,
        pipeline_id=uuid.uuid4(),
        final_run=_run_row(status="budget_exceeded", error_code="budget_exceeded"),
    )

    payload = notifier.dispatch_event.await_args.kwargs["payload"]
    assert payload == {"run_id": str(run_id), "error_code": "budget_exceeded"}
    title, _, _ = _render("budget_exceeded", payload)
    assert title == "Budget exceeded — [unknown]"
