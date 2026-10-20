"""Dispatch the run-failure notification for non-retryable critical codes.

``EVENT_RUN_FAILED`` was declared, listed and fully rendered by the notifier's
event mapper but had NO dispatch call site anywhere in the backend, so a
terminal run failure produced no notification at all. These tests pin the
deliberately narrow wiring: notify ONLY for a code that is BOTH non-retryable
AND ``alert_severity == "critical"``, and never let the attempt block or alter
the terminal write.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.pipeline_engine import error_codes as error_codes_module
from modulo.core.pipeline_engine.error_codes import should_notify_terminal_failure
from modulo.core.pipeline_engine.executor import PipelineExecutor


@asynccontextmanager
async def _begin_cm() -> Any:
    yield None


def _make_session_factory() -> MagicMock:
    session = AsyncMock(spec=AsyncSession)
    session.begin = MagicMock(return_value=_begin_cm())

    @asynccontextmanager
    async def _ctx() -> Any:
        yield session

    return MagicMock(side_effect=lambda: _ctx())


def _finalize_args(
    run_id: uuid.UUID, org_id: uuid.UUID, *, final_status: str, error_code: str | None
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "org_id": org_id,
        "pipeline_id": uuid.uuid4(),
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
    # Keep the helper's DB read out of every dispatch test.
    executor._notification_pipeline_name = AsyncMock(return_value="My Pipeline")  # type: ignore[method-assign]
    return executor


@pytest.mark.parametrize(
    "code",
    [
        "model.insufficient_funds",
        "model_insufficient_funds",  # raw alias spelling
        "model_disabled",
        "provider.authentication",
    ],
    ids=["insufficient_funds", "insufficient_funds_raw", "model_disabled", "provider_authentication"],
)
def test_should_notify_true_for_non_retryable_critical(code: str) -> None:
    assert should_notify_terminal_failure(code)


@pytest.mark.parametrize(
    "code",
    [
        "sandbox.no_output_json",  # retryable
        "run.no_progress",  # non-retryable but warning
        "agent.no_op",  # non-retryable but warning
        "model.rate_limited",  # retryable
        "harness.unknown",  # fallback is warning
        "totally_unknown_code",  # resolves to harness.unknown
        None,
        "",
    ],
    ids=[
        "retryable_no_output",
        "non_retryable_warning_no_progress",
        "non_retryable_warning_no_op",
        "retryable_rate_limited",
        "unknown_fallback_warning",
        "unregistered_code",
        "none_code",
        "empty_code",
    ],
)
def test_should_notify_false_for_everything_else(code: str | None) -> None:
    assert not should_notify_terminal_failure(code)


@pytest.mark.asyncio
async def test_non_retryable_critical_code_dispatches() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    await executor._dispatch_run_failed(
        org_id=org_id,
        run_id=run_id,
        pipeline_id=pipeline_id,
        error_code="model.insufficient_funds",
    )

    kwargs = notifier.dispatch_event.await_args.kwargs
    assert kwargs["org_id"] == org_id
    assert kwargs["event_type"] == "run_failed"
    assert kwargs["run_id"] == run_id
    assert kwargs["payload"] == {
        "run_id": str(run_id),
        "error_code": "model.insufficient_funds",
        "pipeline_name": "My Pipeline",
    }


@pytest.mark.asyncio
async def test_payload_carries_the_canonical_dotted_code_for_a_legacy_spelling() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)

    await executor._dispatch_run_failed(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        error_code="model_insufficient_funds",
    )

    assert notifier.dispatch_event.await_args.kwargs["payload"]["error_code"] == "model.insufficient_funds"


@pytest.mark.asyncio
async def test_retryable_code_is_not_dispatched() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)

    await executor._dispatch_run_failed(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        error_code="sandbox.no_output_json",
    )

    notifier.dispatch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_retryable_non_critical_code_is_not_dispatched() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)

    await executor._dispatch_run_failed(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        error_code="run.no_progress",
    )

    notifier.dispatch_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_code_is_not_dispatched_and_does_not_raise() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)

    await executor._dispatch_run_failed(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        error_code="not_a_registered_code",
    )

    notifier.dispatch_event.assert_not_awaited()


def test_unresolvable_registry_entry_does_not_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    """A canonical code with no registry entry is a silent no-notify, never a raise."""
    monkeypatch.setattr(error_codes_module, "ERROR_CODE_REGISTRY", {})

    assert not should_notify_terminal_failure("model.insufficient_funds")


@pytest.mark.asyncio
async def test_unresolvable_pipeline_name_omits_the_payload_key() -> None:
    """No pipeline name means the payload simply lacks ``pipeline_name``."""
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock()
    executor = _executor_with_notifier(notifier)
    executor._notification_pipeline_name = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await executor._dispatch_run_failed(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        error_code="model.insufficient_funds",
    )

    payload = notifier.dispatch_event.await_args.kwargs["payload"]
    assert "pipeline_name" not in payload
    assert set(payload) == {"run_id", "error_code"}


@pytest.mark.asyncio
async def test_pipeline_name_lookup_without_a_session_factory_is_none() -> None:
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = None

    name = await executor._notification_pipeline_name(
        org_id=uuid.uuid4(), pipeline_id=uuid.uuid4(), run_id=uuid.uuid4()
    )

    assert name is None


@pytest.mark.asyncio
async def test_pipeline_name_lookup_reads_through_the_session() -> None:
    """Success path: org + execution RLS set, then the shared name reader."""
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = _make_session_factory()  # type: ignore[assignment]
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()) as set_org,
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()) as set_exec,
        patch(
            "modulo.core.pipeline_engine.executor._safe_pipeline_name",
            new=AsyncMock(return_value="Pipeline X"),
        ) as read_name,
    ):
        name = await executor._notification_pipeline_name(org_id=org_id, pipeline_id=pipeline_id, run_id=uuid.uuid4())

    assert name == "Pipeline X"
    set_org.assert_awaited_once()
    set_exec.assert_awaited_once()
    read_name.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_name_lookup_failure_returns_none_and_logs() -> None:
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = _make_session_factory()  # type: ignore[assignment]

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch(
            "modulo.core.pipeline_engine.executor._safe_pipeline_name",
            new=AsyncMock(side_effect=RuntimeError("lookup boom")),
        ),
        patch("modulo.core.pipeline_engine.executor._log.warning") as warn,
    ):
        name = await executor._notification_pipeline_name(
            org_id=uuid.uuid4(), pipeline_id=uuid.uuid4(), run_id=uuid.uuid4()
        )

    assert name is None
    warn.assert_called_once()


@pytest.mark.asyncio
async def test_pipeline_name_lookup_cancellation_propagates() -> None:
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = _make_session_factory()  # type: ignore[assignment]

    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch(
            "modulo.core.pipeline_engine.executor._safe_pipeline_name",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await executor._notification_pipeline_name(org_id=uuid.uuid4(), pipeline_id=uuid.uuid4(), run_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_missing_notifier_is_a_noop() -> None:
    executor = PipelineExecutor(MagicMock(), notifier=None)

    await executor._dispatch_run_failed(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        error_code="model.insufficient_funds",
    )

    assert executor._notifier is None


@pytest.mark.asyncio
async def test_notifier_exception_is_swallowed_and_logged() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock(side_effect=RuntimeError("notifier boom"))
    executor = _executor_with_notifier(notifier)

    with patch("modulo.core.pipeline_engine.executor._log.exception") as mock_log:
        await executor._dispatch_run_failed(
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            error_code="model.insufficient_funds",
        )

    assert notifier.dispatch_event.await_count == 1
    mock_log.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_error_is_re_raised() -> None:
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock(side_effect=asyncio.CancelledError())
    executor = _executor_with_notifier(notifier)

    with pytest.raises(asyncio.CancelledError):
        await executor._dispatch_run_failed(
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            error_code="model.insufficient_funds",
        )


@pytest.mark.asyncio
async def test_finalize_failed_terminal_fires_run_failed_notification() -> None:
    """The WIRING: a failed terminal write dispatches after the write commits."""
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = _make_session_factory()  # type: ignore[assignment]
    executor._dispatch_run_failed = AsyncMock()  # type: ignore[method-assign]
    args = _finalize_args(run_id, org_id, final_status="failed", error_code="model.insufficient_funds")

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_finalize_cost_transaction", new=AsyncMock()),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_seal_run_teardown", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=MagicMock())),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
    ):
        await executor._finalize_run_after_stream(**args)

    executor._dispatch_run_failed.assert_awaited_once_with(
        org_id=org_id,
        run_id=run_id,
        pipeline_id=args["pipeline_id"],
        error_code="model.insufficient_funds",
    )


@pytest.mark.asyncio
async def test_finalize_non_failed_terminal_does_not_fire_run_failed_notification() -> None:
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = _make_session_factory()  # type: ignore[assignment]
    executor._dispatch_run_failed = AsyncMock()  # type: ignore[method-assign]
    args = _finalize_args(run_id, org_id, final_status="cancelled", error_code="user.cancelled")

    with (
        patch.object(executor, "_compute_run_work_intact", return_value=None),
        patch.object(executor, "_run_finalize_cost_transaction", new=AsyncMock()),
        patch.object(executor, "_run_post_terminal_evidence_probes", new=AsyncMock()),
        patch.object(executor, "_seal_run_teardown", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=MagicMock())),
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
    ):
        await executor._finalize_run_after_stream(**args)

    executor._dispatch_run_failed.assert_not_awaited()
