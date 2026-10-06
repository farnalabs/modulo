"""FAR-1050 R4/R6: the dispatch rewire (create / stream / kill) through the ABC.

One proof per row of the design doc's binding risk table
(``docs/design/e2b-provider-conformance-rewire.md`` §5), all without a live
E2B sandbox. R6 deleted the legacy direct path and the
``MODULO_E2B_VIA_PROVIDER`` flag, so every dispatch proof below runs the
provider path unconditionally:

1. **Full ``_sandbox_agent_impl`` run** with a fake provider: the ABC call
   sequence, the dispatch marker written BEFORE create, cancellation
   propagation, no zero-exit fabrication on a stream error, retry
   classification unchanged.
2. **Streaming semantics**: ordered chunks, ``exit_code is None`` + ``error``
   set on a proxy drop, ``done`` fires on an early consumer close.
3. **Watchdog**: a slice timeout never cancels the underlying wait (the
   FAR-97/98 ``cancelling()==0`` invariant, re-expressed over
   ``ExecProcess.done``).
4. **Retry classification**: provider ``RateLimitedError`` /
   ``ProvisionTimeoutError`` map to the EXISTING retryable codes; no new
   ``harness.unknown`` outcome on a rate limit; no re-parenting.
5. **Cost stamping**: the dispatch stamps through the sanctioned
   ``_compute_sandbox_cost`` helper exactly once per run.
6. **Marker / telemetry attribution**: ``provider`` + ``via_provider`` on the
   marker rewrite and on node telemetry.
7. **T10**: ``provision_workspace_inputs_in_sandbox`` gets the ABC-mediated
   handle and its four commands round-trip through the fake provider.
8. **S2/S3/S4** (org-deletion / pipeline-execution / evidence kill sites):
   stay direct by ADR 040 decision and never reference the retired flag.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine import node_runner as nr
from modulo.core.pipeline_engine import workspace_input_orchestration as wio
from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError,
    SandboxQueueTimeoutError,
    SandboxRateLimitedError,
    _build_dispatch_provider,
    _dispatch_marker_json,
    _ExecStreamOutcome,
    _ProviderMediatedHandle,
    _require_dispatch_provider,
    _translate_provider_dispatch_error,
    _wait_command_with_exec_process,
    make_sandbox_agent_fn,
)
from modulo.core.runtime_provider import (
    ExecProcess,
    ExecResult,
    ExecStreamChunk,
    ProvisionTimeoutError,
    RateLimitedError,
    RuntimeProvider,
    RuntimeProviderError,
    WorkspaceMetrics,
    WorkspaceSpec,
)
from modulo.settings import get_settings
from tests.unit.pipeline_engine.conftest import FakeDispatchProvider, install_fake_dispatch

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
_COMPLETED_OUTPUT = '{"status": "completed", "summary": "done"}'


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _base_node_def(**overrides: Any) -> dict[str, Any]:
    node_def: dict[str, Any] = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_commands": [_AGENT_COMMAND],
        "timeout_seconds": 30,
    }
    node_def.update(overrides)
    return node_def


def _run_state() -> dict[str, Any]:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-r4",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


def _install_log_tail(monkeypatch: pytest.MonkeyPatch, tail: bytes = b"r4-tail") -> AsyncMock:
    """Route the R1 log-tail seam to a fake so the dispatch never hits urllib."""
    builder = AsyncMock(return_value=MagicMock(read_log_tail=AsyncMock(return_value=tail)))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_log_tail_provider", builder)
    return builder


def _spy_dispatch_marker(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    """Record ``marker`` on *order* (the dispatch seam's own event list)."""
    original = nr._sandbox_acquire_dispatch_marker

    async def _spy(**kwargs: Any) -> str | None:
        order.append("marker")
        return await original(**kwargs)

    monkeypatch.setattr(nr, "_sandbox_acquire_dispatch_marker", _spy)


def _capture_marker_store(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what the post-create marker rewrite is told to stamp."""
    captured: list[dict[str, Any]] = []
    original = nr._sandbox_store_dispatch_marker_sandbox

    async def _spy(sandbox_id_value: str | None, **kwargs: Any) -> None:
        captured.append(
            {
                "sandbox_id": sandbox_id_value,
                "provider": kwargs.get("provider"),
                "via_provider": kwargs.get("via_provider"),
            }
        )
        await original(sandbox_id_value, **kwargs)

    monkeypatch.setattr(nr, "_sandbox_store_dispatch_marker_sandbox", _spy)
    return captured


_END = object()


class _ScriptedStream:
    """A queue-driven ``ExecProcess`` whose end the test controls.

    Queue-driven (rather than a plain ``for`` over a list) so a consumer task
    and a tick handler can both touch the stream without racing on one
    generator — the pump consumes while the watchdog's ``on_tick`` decides
    when the stream ends.
    """

    def __init__(self, *, exit_code: int | None = 0, error: str | None = None) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.exit_code = exit_code
        self.error = error
        self.process = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]
        process = self.process

        async def _chunks() -> Any:
            try:
                while True:
                    item = await self.queue.get()
                    if item is _END:
                        break
                    yield item
                if error is not None:
                    # A stream drop: ``exit_code`` stays None (no fabricated
                    # zero) and ``error`` carries the description.
                    process.error = error
                    process.exit_code = None
                else:
                    process.exit_code = exit_code
            finally:
                process.done.set()

        self.process.chunks = _chunks()

    def push(self, stream: str, data: str) -> None:
        self.queue.put_nowait(ExecStreamChunk(stream=stream, data=data))

    def end(self) -> None:
        self.queue.put_nowait(_END)


# ---------------------------------------------------------------------------
# 1. Seam units
# ---------------------------------------------------------------------------


def test_require_dispatch_provider_fails_closed_with_typed_error() -> None:
    with pytest.raises(RuntimeProviderError):
        _require_dispatch_provider(None)


async def test_build_dispatch_provider_returns_none_without_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.setattr("modulo.core.runtime_config.key_bridge.override_or", lambda *_a, **_k: None)
    assert await _build_dispatch_provider() is None


async def test_build_dispatch_provider_returns_none_when_hub_build_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("modulo.core.runtime_provider.build_hub", MagicMock(side_effect=RuntimeError("hub down")))
    assert await _build_dispatch_provider() is None


async def test_build_dispatch_provider_returns_the_e2b_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-key")
    provider = await _build_dispatch_provider()
    assert isinstance(provider, E2BRuntimeProvider)


# ---------------------------------------------------------------------------
# 2. Full dispatch: ABC call sequence + marker-before-create
# ---------------------------------------------------------------------------


async def test_provider_full_dispatch_runs_the_abc_call_sequence(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """marker -> create -> command -> by-ref destroy -> close.

    The dispatch marker is acquired BEFORE ``create_workspace``, and every
    lifecycle step of the run happens on the provider — the legacy
    ``AsyncSandbox.create`` is never reached (asserted below).
    """
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()

    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-r4-seq", exit_code=0)
    _spy_dispatch_marker(monkeypatch, dispatch.events)

    legacy_create = AsyncMock(
        side_effect=AssertionError("AsyncSandbox.create must not run when routed via the provider")
    )
    fn = make_sandbox_agent_fn(_base_node_def())
    with patch("e2b.AsyncSandbox.create", new=legacy_create):
        result = await fn(_run_state())

    assert dispatch.events == ["marker", "create", "command", "destroy_by_ref", "close"], dispatch.events
    legacy_create.assert_not_awaited()
    # The provider really provisioned with the dispatch's own WorkspaceSpec.
    assert isinstance(dispatch.created_spec, WorkspaceSpec)
    assert dispatch.created_spec.image_ref == "opencode"
    # ...and the agent command really went through the ABC stream with the
    # dispatch envs (MODULO_SCHEMA_DIR rides on the command env provider).
    assert dispatch.last_command is not None
    assert dispatch.last_command[0] == "bash"
    assert dispatch.last_environment is not None
    assert dispatch.last_environment["MODULO_SCHEMA_DIR"] == "/home/user/schemas"

    assert result["output"]["status"] == "completed"
    assert result["artifacts"][0]["output"]["exit_code"] == 0


# ---------------------------------------------------------------------------
# 3. Cancellation propagates (never swallowed, never converted to ExecResult(-1))
# ---------------------------------------------------------------------------


async def test_provider_cancelled_create_propagates(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    _install_log_tail(monkeypatch)
    dispatch = install_fake_dispatch(
        monkeypatch,
        ref="sbx-cancel-create",
        create_exception=asyncio.CancelledError(),
    )

    fn = make_sandbox_agent_fn(_base_node_def())
    with pytest.raises(asyncio.CancelledError):
        await fn(_run_state())

    # Never converted into a failed envelope / ExecResult(-1).
    assert "create" in dispatch.events


async def test_provider_cancelled_stream_start_propagates(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    _install_log_tail(monkeypatch)
    dispatch = install_fake_dispatch(
        monkeypatch,
        ref="sbx-cancel-stream",
        stream_start_exception=asyncio.CancelledError(),
    )

    fn = make_sandbox_agent_fn(_base_node_def())
    with pytest.raises(asyncio.CancelledError):
        await fn(_run_state())

    assert dispatch.events[0] == "create"


# ---------------------------------------------------------------------------
# 4. No zero-exit fabrication on a stream error
# ---------------------------------------------------------------------------


async def test_provider_stream_error_never_fabricates_a_zero_exit(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A proxy drop surfaces as ``exit_code = -1`` + failed status, never 0."""
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    dispatch = install_fake_dispatch(
        monkeypatch,
        ref="sbx-stream-error",
        exit_code=0,  # would be a fabricated success if the error were ignored
        stream_error="engine proxy dropped mid-stream",
    )

    fn = make_sandbox_agent_fn(_base_node_def())
    result = await fn(_run_state())

    inner = result["artifacts"][0]["output"]
    assert inner["exit_code"] == -1
    assert result["output"]["status"] == "failed"
    assert dispatch.events == ["create", "command", "destroy_by_ref", "close"], dispatch.events


async def test_provider_healthy_non_zero_exit_is_a_result_not_a_stream_error(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A command that exits non-zero on a HEALTHY stream keeps its real code."""
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    install_fake_dispatch(monkeypatch, ref="sbx-exit7", exit_code=7)

    fn = make_sandbox_agent_fn(_base_node_def())
    result = await fn(_run_state())

    inner = result["artifacts"][0]["output"]
    assert inner["exit_code"] == 7
    assert result["output"]["status"] == "failed"


# ---------------------------------------------------------------------------
# 5. Streaming semantics (ExecProcess contract over the watchdog)
# ---------------------------------------------------------------------------


async def test_exec_process_watchdog_delivers_chunks_in_order() -> None:
    stream = _ScriptedStream(exit_code=0)
    stream.push("stdout", "a")
    stream.push("stderr", "e")
    stream.push("stdout", "b")
    stream.end()
    seen: list[str] = []

    async def _on_stdout(data: str) -> None:
        seen.append(f"out:{data}")

    async def _on_stderr(data: str) -> None:
        seen.append(f"err:{data}")

    outcome, stall = await _wait_command_with_exec_process(
        stream.process,
        total_timeout=5.0,
        idle_timeout=5.0,
        last_activity=lambda: asyncio.get_running_loop().time(),
        tick_interval=0.05,
        on_stdout=_on_stdout,
        on_stderr=_on_stderr,
    )

    assert stall is None
    assert seen == ["out:a", "err:e", "out:b"]
    assert isinstance(outcome, _ExecStreamOutcome)
    assert outcome.exit_code == 0
    assert outcome.stdout == "ab"
    assert outcome.stderr == "e"
    assert outcome.stream_error is None


async def test_exec_process_watchdog_proxy_drop_yields_none_exit_and_error() -> None:
    stream = _ScriptedStream(exit_code=0, error="proxy dropped")
    stream.push("stdout", "partial")
    stream.end()

    outcome, stall = await _wait_command_with_exec_process(
        stream.process,
        total_timeout=5.0,
        idle_timeout=5.0,
        last_activity=lambda: asyncio.get_running_loop().time(),
        tick_interval=0.05,
    )

    assert stall is None
    assert outcome.exit_code == -1
    assert outcome.stream_error == "proxy dropped"
    # The ABC's own contract holds on the same object: None exit, error set.
    assert stream.process.exit_code is None
    assert stream.process.error == "proxy dropped"


async def test_exec_process_done_fires_on_early_consumer_close() -> None:
    stream = _ScriptedStream(exit_code=0)
    for i in range(5):
        stream.push("stdout", str(i))
    process = stream.process
    assert not process.done.is_set()
    first = await process.chunks.__anext__()
    assert first.data == "0"
    # An early consumer close runs the generator's finally — ``done`` fires
    # (ADR 040 lifecycle contract), so no waiter can hang.
    await process.chunks.aclose()
    assert process.done.is_set()


async def test_exec_process_watchdog_slice_timeouts_never_cancel_the_wait() -> None:
    """The FAR-97/98 invariant, re-expressed over ``ExecProcess.done``.

    Several poll slices must elapse (each a potential cancellation point)
    without cancelling the underlying wait or bumping the current task's
    ``cancelling()`` counter — the failure mode that used to surface as
    ``NodeCancelledError`` one tick into every sandbox run.
    """
    stream = _ScriptedStream(exit_code=7)
    stream.push("stdout", "x")
    ticks = 0

    async def _on_tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= 3:
            stream.end()

    outcome, stall = await _wait_command_with_exec_process(
        stream.process,
        total_timeout=10.0,
        idle_timeout=5.0,
        last_activity=lambda: asyncio.get_running_loop().time(),
        on_tick=_on_tick,
        tick_interval=0.05,
    )

    assert stall is None
    assert ticks >= 3, "the watchdog must survive several slice timeouts"
    assert outcome.exit_code == 7
    assert asyncio.current_task().cancelling() == 0


async def test_exec_process_watchdog_idle_window_kills_and_reports_stall() -> None:
    stream = _ScriptedStream(exit_code=None)
    killed: list[str] = []

    async def _kill() -> None:
        killed.append("killed")

    stream.process._kill = _kill

    outcome, stall = await _wait_command_with_exec_process(
        stream.process,
        total_timeout=10.0,
        idle_timeout=0.2,
        last_activity=lambda: 0.0,
        tick_interval=0.05,
    )

    assert outcome is None
    assert stall is not None
    assert "no output" in stall
    assert killed == ["killed"]


async def test_exec_process_watchdog_total_timeout_raises_without_touching_done() -> None:
    stream = _ScriptedStream(exit_code=None)

    with pytest.raises(TimeoutError, match="total timeout"):
        await _wait_command_with_exec_process(
            stream.process,
            total_timeout=0.2,
            idle_timeout=5.0,
            last_activity=lambda: asyncio.get_running_loop().time(),
            tick_interval=0.05,
        )

    # The slice timeouts must not have fired ``done`` (they cancelled nothing).
    assert stream.process.done.is_set() is False


# ---------------------------------------------------------------------------
# 6. Retry classification (no re-parenting, no new harness.unknown)
# ---------------------------------------------------------------------------


def test_provider_errors_translate_onto_the_existing_taxonomy_without_reparenting() -> None:

    translated = _translate_provider_dispatch_error(RateLimitedError("429 too many"))
    assert isinstance(translated, SandboxRateLimitedError)
    assert not isinstance(translated, RateLimitedError)

    translated = _translate_provider_dispatch_error(ProvisionTimeoutError("never ready"))
    assert isinstance(translated, SandboxQueueTimeoutError)

    # Non-provider failures translate to themselves (identity, not a copy).
    original = ValueError("nope")
    assert _translate_provider_dispatch_error(original) is original

    # No re-parenting in EITHER direction (ADR 040: additive only).
    assert not issubclass(RateLimitedError, SandboxNodeFailedError)
    assert not issubclass(SandboxRateLimitedError, RateLimitedError)
    assert not issubclass(ProvisionTimeoutError, SandboxNodeFailedError)


def test_translated_classes_resolve_to_the_existing_retryable_codes() -> None:
    from modulo.core.pipeline_engine.error_codes import (
        _CODE_SANDBOX_QUEUE_TIMEOUT,
        _CODE_SANDBOX_RATE_LIMITED,
        LEGACY_ALIASES,
    )
    from modulo.core.pipeline_engine.runtime_retry import _NEVER_RETRYABLE_NAMES

    assert LEGACY_ALIASES["SandboxRateLimitedError"] == _CODE_SANDBOX_RATE_LIMITED
    assert LEGACY_ALIASES["SandboxQueueTimeoutError"] == _CODE_SANDBOX_QUEUE_TIMEOUT
    # A rate limit must never be an unknown harness outcome...
    assert LEGACY_ALIASES["SandboxRateLimitedError"] != "harness.unknown"
    assert LEGACY_ALIASES["SandboxQueueTimeoutError"] != "harness.unknown"
    # ...and never terminal.
    assert "SandboxRateLimitedError" not in _NEVER_RETRYABLE_NAMES
    assert "SandboxQueueTimeoutError" not in _NEVER_RETRYABLE_NAMES


async def test_provider_rate_limit_enters_the_existing_backoff_then_queue_timeout(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The provider's typed rate limit drives the SAME bounded backoff loop."""
    _install_log_tail(monkeypatch)
    dispatch = install_fake_dispatch(
        monkeypatch,
        ref="sbx-429",
        create_exception=RateLimitedError("429 concurrent sandbox limit"),
    )

    with (
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_RATE_LIMIT_BASE_BACKOFF_S", 0),
        pytest.raises(SandboxQueueTimeoutError) as excinfo,
    ):
        await make_sandbox_agent_fn(_base_node_def())(_run_state())

    assert "rate-limited after" in str(excinfo.value)
    # create was retried through the existing bounded loop (MAX_RETRIES + 1).
    assert dispatch.events.count("create") == nr._SANDBOX_RATE_LIMIT_MAX_RETRIES + 1
    # ...and the stream was never started.
    assert "command" not in dispatch.events


async def test_provider_wrapped_sdk_rate_limit_still_backs_off(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """The E2B provider wraps SDK failures — the ``RateLimitException`` on
    ``__cause__`` must still reach the backoff loop (no ``harness.unknown``)."""
    from e2b.exceptions import RateLimitException

    _install_log_tail(monkeypatch)
    wrapped = RuntimeError("Failed to create E2B sandbox with template 'opencode'")
    wrapped.__cause__ = RateLimitException("429 rate limited")
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-wrapped-429", create_exception=wrapped)

    with (
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_RATE_LIMIT_BASE_BACKOFF_S", 0),
        pytest.raises(SandboxQueueTimeoutError),
    ):
        await make_sandbox_agent_fn(_base_node_def())(_run_state())

    assert dispatch.events.count("create") == nr._SANDBOX_RATE_LIMIT_MAX_RETRIES + 1


async def test_provider_typed_rate_limit_escaping_a_provider_call_is_retryable(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A ``RateLimitedError`` from ANY provider call (here: a file write) is
    re-raised as the pre-existing retryable class — never swallowed into a
    synthetic ``harness.unknown`` envelope."""
    _install_log_tail(monkeypatch)
    install_fake_dispatch(monkeypatch, ref="sbx-429-outside")
    fake_file_io.write_error = RateLimitedError("429 concurrent sandbox limit")

    with pytest.raises(SandboxRateLimitedError):
        await make_sandbox_agent_fn(_base_node_def())(_run_state())


async def test_provider_provision_timeout_escaping_a_provider_call_is_retryable(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    _install_log_tail(monkeypatch)
    install_fake_dispatch(monkeypatch, ref="sbx-provision-timeout-outside")
    fake_file_io.write_error = ProvisionTimeoutError("workspace never became ready")

    with pytest.raises(SandboxQueueTimeoutError):
        await make_sandbox_agent_fn(_base_node_def())(_run_state())


# ---------------------------------------------------------------------------
# 7. Egress carried into the provider create path (ADR 040 egress rule)
# ---------------------------------------------------------------------------


async def test_provider_restrictive_egress_reaches_the_provider_create(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """FAR-1050 R5: the stopgap refusal is GONE — a resolved ``deny_all`` is
    carried into ``create_workspace(spec)`` (and from there into the SDK's
    ``allow_internet_access``) instead of refusing the dispatch.

    Fails on the pre-R5 code: the stopgap raised ``SandboxTierRefusedError``
    whenever an egress policy was resolved, so no spec was ever built.
    """
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    legacy_create = AsyncMock(
        side_effect=AssertionError("AsyncSandbox.create must not run when routed via the provider")
    )
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-egress", exit_code=0)

    fn = make_sandbox_agent_fn(_base_node_def(egress_policy="deny_all"))
    with patch("e2b.AsyncSandbox.create", new=legacy_create):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    legacy_create.assert_not_awaited()
    assert "create" in dispatch.events
    assert dispatch.created_spec is not None
    # The restrictive policy reached the provider's own spec — the field the
    # E2B provider maps onto allow_internet_access.
    assert dispatch.created_spec.egress_policy == "deny_all"


async def test_provider_selected_allowlist_rides_the_spec_metadata(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """FAR-1050 R5: the selected-mode allowlist reaches the provider spec's
    ``workspace_metadata`` under the LEGACY create's key (``egress_allowlist``),
    and the dispatch is no longer refused for a resolved policy."""
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    legacy_create = AsyncMock(
        side_effect=AssertionError("AsyncSandbox.create must not run when routed via the provider")
    )
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-egress-selected", exit_code=0)
    # Keep the unit offline: allowlist pre-resolution is a DNS lookup on both
    # paths, and the legacy resolution helper is unchanged by this slice.
    monkeypatch.setattr(nr, "_resolve_egress_allowlist", AsyncMock(side_effect=lambda v: v))
    # The selected-mode policy step routes through apply_isolation; a
    # recording stand-in keeps the run on the fake dispatch (no network).
    isolation = MagicMock(apply_isolation=AsyncMock())
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_isolation_provider",
        AsyncMock(return_value=isolation),
    )

    allowlist = [{"host": "api.example.com", "port": 443}]
    fn = make_sandbox_agent_fn(_base_node_def(egress_policy="selected", egress_allowlist=allowlist))
    with patch("e2b.AsyncSandbox.create", new=legacy_create):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    legacy_create.assert_not_awaited()
    assert dispatch.created_spec is not None
    assert dispatch.created_spec.egress_policy == "selected"
    assert json.loads(dispatch.created_spec.workspace_metadata["egress_allowlist"]) == allowlist
    isolation.apply_isolation.assert_awaited_once()


# ---------------------------------------------------------------------------
# 8. Cost stamping parity
# ---------------------------------------------------------------------------


async def test_cost_estimate_stamps_through_the_sanctioned_helper(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """Fixed elapsed + fixed output => pinned ``cost_estimate_usd``.

    The elapsed component is pinned by wrapping ``_compute_sandbox_cost`` so
    the assertion is about the DISPATCH (which output fixture reaches the
    function, and that the provider path calls it at all), not about wall
    clock. The dispatch must call the sanctioned helper exactly once with the
    SAME output and stamp its result unchanged.

    (FAR-1050 R6 retired ``MODULO_E2B_VIA_PROVIDER``; the legacy half of the
    former on/off comparison is the legacy direct path R6 deleted.)
    """
    real = nr._compute_sandbox_cost
    pinned_elapsed = 123.456
    calls: list[tuple[float, Any]] = []

    def _pinned(elapsed: float, output_json: Any) -> float:
        calls.append((elapsed, output_json))
        return real(pinned_elapsed, output_json)

    fixture = {"status": "completed", "summary": "done", "cost_estimate_usd": 1.25}
    encoded = json.dumps(fixture).encode()

    monkeypatch.setattr(nr, "_compute_sandbox_cost", _pinned)
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = encoded
    install_fake_dispatch(monkeypatch, ref="sbx-cost-on", exit_code=0)
    fn = make_sandbox_agent_fn(_base_node_def())
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(side_effect=AssertionError("no legacy create"))):
        result = await fn(_run_state())

    assert len(calls) == 1, "the dispatch must stamp through _compute_sandbox_cost exactly once"
    assert result["output"]["cost_estimate_usd"] == real(pinned_elapsed, fixture)


# ---------------------------------------------------------------------------
# 9. Marker / telemetry attribution on BOTH paths
# ---------------------------------------------------------------------------


async def test_dispatch_marker_and_telemetry_carry_provider_attribution(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The marker rewrite AND the node telemetry stamp ``provider`` +
    ``via_provider`` so a run is attributable to provider execution."""
    stamped = _capture_marker_store(monkeypatch)

    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    install_fake_dispatch(monkeypatch, ref="sbx-attr-on", exit_code=0)
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(side_effect=AssertionError("no legacy create"))):
        result = await make_sandbox_agent_fn(_base_node_def())(_run_state())

    # Node telemetry (the P1b splitter folds these verbatim).
    assert result["output"]["provider"] == "e2b"
    assert result["output"]["via_provider"] is True
    assert result["artifacts"][0]["output"]["via_provider"] is True

    assert len(stamped) == 1
    entry = stamped[0]
    assert entry["provider"] == "e2b"
    assert entry["via_provider"] is True
    assert entry["sandbox_id"]


async def test_marker_telemetry_lands_in_node_telemetry_json(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """The attribution keys survive the envelope -> telemetry split."""
    from modulo.core.node_output_split import split_node_output

    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    install_fake_dispatch(monkeypatch, ref="sbx-telemetry", exit_code=0)
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(side_effect=AssertionError("no legacy create"))):
        result = await make_sandbox_agent_fn(_base_node_def())(_run_state())

    _, telemetry = split_node_output(result, "sandbox_agent", None, node_id="n1")
    assert telemetry["provider"] == "e2b"
    assert telemetry["via_provider"] is True


def test_dispatch_marker_json_carries_provider_and_routing_and_stays_readable() -> None:
    """ADR 040 marker schema versioning: the extra key never breaks a reader."""
    from modulo.core.runner_capacity import parse_marker_state

    for provider_flag in (True, False):
        raw = _dispatch_marker_json("run:1:node:n:2", "e2b", via_provider=provider_flag)
        payload = json.loads(raw)
        assert payload["state"] == "dispatching"
        assert payload["attempt_key"] == "run:1:node:n:2"
        assert payload["provider"] == "e2b"
        assert payload["via_provider"] is provider_flag
        # Unknown-field tolerance on read.
        assert parse_marker_state(raw) == "dispatching"


# ---------------------------------------------------------------------------
# 10. T10 — workspace inputs receive the right handle
# ---------------------------------------------------------------------------


def _resolved_input() -> Any:
    return wio.ResolvedInput(
        url="https://github.com/example/repo.git",
        dest="repos/example",
        resolved_sha="a" * 40,
        connector_instance_id=None,
        credential_setup_script="#!/bin/sh\necho setup",
        credential_teardown_script="#!/bin/sh\necho teardown",
    )


def _patch_workspace_inputs(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Resolve managed inputs host-side without touching the network."""
    monkeypatch.setattr(get_settings(), "modulo_workspace_inputs_enabled", True)
    monkeypatch.setattr(
        wio,
        "resolve_managed_inputs_host_side",
        AsyncMock(return_value=[_resolved_input()]),
    )
    # The least-privilege assertion client pins a transport at construction;
    # no network is wanted here.
    fake_client = MagicMock()
    fake_client.aclose = AsyncMock()
    monkeypatch.setattr("modulo.core.ssrf.pinned_async_client_sync", MagicMock(return_value=fake_client))
    seen: list[Any] = []
    original = wio.provision_workspace_inputs_in_sandbox

    async def _spy(sandbox_obj: Any, resolved: Any, **kwargs: Any) -> None:
        seen.append(sandbox_obj)
        await original(sandbox_obj, resolved, **kwargs)

    monkeypatch.setattr(wio, "provision_workspace_inputs_in_sandbox", _spy)
    return seen


async def test_provider_workspace_inputs_receive_the_abc_mediated_handle(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The helper gets the ABC-mediated handle and its four commands
    (setup + clone + teardown + drift probe) round-trip the fake provider."""
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    seen = _patch_workspace_inputs(monkeypatch)
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-t10-on", exit_code=0)

    node_def = _base_node_def(workspace_inputs=[{"dest": "repos/example", "ref": {"kind": "branch"}}])
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(side_effect=AssertionError("no legacy create"))):
        result = await make_sandbox_agent_fn(node_def)(_run_state())

    assert result["output"]["status"] == "completed"
    assert len(seen) == 1
    assert isinstance(seen[0], _ProviderMediatedHandle)
    assert seen[0].sandbox_id == "sbx-t10-on"
    # credential setup + clone + credential teardown + drift probe.
    assert dispatch.events.count("exec") == 4, dispatch.events
    # ...each a shell round-trip through exec_command.
    assert dispatch.last_command is not None
    assert dispatch.last_command[0] == "bash"


# ---------------------------------------------------------------------------
# 11. S2 / S3 / S4 — the sanctioned kill sites never read the flag
# ---------------------------------------------------------------------------

_S2_S3_S4_SOURCES = (
    "db/crud/org_deletion.py",
    "core/pipeline_execution.py",
    "core/pipeline_engine/evidence.py",
)


def _repo_src(rel: str) -> str:
    # node_runner.py -> .../src/modulo/core/pipeline_engine -> parents[2] = .../src/modulo
    return (Path(nr.__file__).parents[2] / rel).read_text(encoding="utf-8")


def test_s2_s3_s4_kill_sites_never_read_the_flag() -> None:
    """Footprint review (design §5): none of the sanctioned sites may branch
    on ``MODULO_E2B_VIA_PROVIDER`` — they stay direct by ADR 040 decision."""
    for rel in _S2_S3_S4_SOURCES:
        source = _repo_src(rel)
        assert "MODULO_E2B_VIA_PROVIDER" not in source, rel
        assert "via_provider" not in source, rel
        assert "_dispatch_via_provider_enabled" not in source, rel


async def test_s3_run_watchdog_kill_never_raises() -> None:
    """``pipeline_execution._kill_sandbox_best_effort`` never raises."""
    from modulo.core import pipeline_execution as pe

    killed: list[str] = []

    class _Sbx:
        async def kill(self, **kwargs: Any) -> None:
            killed.append("killed")

    class _Result:
        def first(self) -> Any:
            return ("sbx-s3",)

    class _Conn:
        async def execute(self, *args: Any, **kwargs: Any) -> _Result:
            return _Result()

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

    class _Engine:
        def connect(self) -> _Conn:
            return _Conn()

    connect = AsyncMock(side_effect=lambda _sid: _Sbx())

    with patch("e2b.AsyncSandbox.connect", new=connect):
        await pe._kill_sandbox_best_effort(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
    assert killed == ["killed"]
    assert connect.await_count == 1


async def test_s2_org_deletion_kill_is_bounded_and_best_effort() -> None:
    from modulo.db.crud import org_deletion as od

    class _Result:
        def first(self) -> Any:
            return (1,)

        def all(self) -> list[Any]:
            return [("sbx-s2",)]

    class _Session:
        async def execute(self, *args: Any, **kwargs: Any) -> _Result:
            return _Result()

    kill = AsyncMock()
    with patch("e2b.AsyncSandbox.kill", new=kill):
        kill.reset_mock()
        assert await od._abort_org_live_sandboxes(_Session(), uuid.UUID(_ORG_ID)) == 1  # type: ignore[arg-type]
        assert kill.await_count == 1
        assert kill.await_args.args[0] == "sbx-s2"


async def test_s4_evidence_probes_run_through_the_sdk_handle() -> None:
    from modulo.core.pipeline_engine import evidence as ev

    class _Commands:
        async def run(self, command: str, **kwargs: Any) -> Any:
            result = MagicMock()
            result.exit_code = 0
            result.stdout = "probe-ok"
            result.stderr = ""
            return result

    class _Files:
        async def list(self, path: str) -> list[Any]:
            entry = MagicMock()
            entry.name = "output.json"
            entry.size = 10
            entry.isdir = False
            return [entry]

    class _Sbx:
        commands = _Commands()
        files = _Files()

        async def close(self) -> None:
            return None

    handle = _Sbx()

    with patch("e2b.AsyncSandbox.connect", new=AsyncMock(return_value=handle)):
        result = await ev._e2b_run_command("sbx-s4", "echo probe-ok")
        files = await ev._e2b_list_files("sbx-s4")
    assert result.stdout == "probe-ok"
    assert [f.name for f in files] == ["output.json"]


# ---------------------------------------------------------------------------
# 12. Dispatch seam error arms (every defensive branch is reachable + tested)
# ---------------------------------------------------------------------------


def test_require_dispatch_spec_fails_closed_with_typed_error() -> None:
    """The create-spec narrow refuses a missing spec with the typed error.

    Companion to ``test_require_dispatch_provider_fails_closed_with_typed_error``:
    the provider create loop must never reach ``create_workspace`` without a
    ``WorkspaceSpec`` (there is no silent fall back to ``AsyncSandbox.create``).
    """
    with pytest.raises(RuntimeProviderError):
        nr._require_dispatch_spec(None)


def test_is_rate_limited_error_returns_false_for_non_rate_limit_failures() -> None:
    """A non-rate-limit failure (and a cyclic ``__cause__`` chain) reports False.

    The wrapped-SDK test above covers the ``True`` walk; this covers the walk
    TERMINATING without a match — including the ``id`` guard that stops a
    self-referential ``__cause__`` from looping forever.
    """
    assert nr._is_rate_limited_error(ValueError("not a rate limit")) is False
    cyclic = ValueError("cyclic cause")
    cyclic.__cause__ = cyclic
    assert nr._is_rate_limited_error(cyclic) is False


async def test_provider_commands_run_background_and_non_zero_exit_fail_loudly() -> None:
    """``_ProviderCommands.run`` refuses a background start and RAISES on a
    non-zero exit (the duck-typed helpers rely on the raise, mirroring the SDK).
    """
    from modulo.core.pipeline_engine.node_runner import _ProviderCommands
    from modulo.core.runtime_provider import ExecResult

    class _Provider:
        async def exec_command(
            self,
            provider_ref: str,
            command: list[str],
            *,
            cmd_timeout: int | None = None,
        ) -> ExecResult:
            return ExecResult(exit_code=3, stdout="", stderr="clone failed")

    commands = _ProviderCommands(_Provider(), "sbx-cmds")
    with pytest.raises(RuntimeError, match="background command start"):
        await commands.run("echo hi", background=True)
    with pytest.raises(RuntimeError, match="command exited with code 3"):
        await commands.run("echo hi")


def test_provider_mediated_handle_has_no_legacy_files_surface() -> None:
    """Touching ``files`` on the ABC-mediated handle is loud, never a downgrade."""
    handle = _ProviderMediatedHandle(MagicMock(), "sbx-handle")
    with pytest.raises(RuntimeError, match="no legacy files surface"):
        _ = handle.files


class _MetricslessProvider(RuntimeProvider):
    """Concrete provider that does NOT override ``get_metrics`` (FAR-1050 R6).

    Stands in for any tier without a metrics substrate (local, Docker): the
    ABC's typed default refusal is what the watchdog must fail open on.
    """

    provider_id = "no-metrics"

    def __init__(self) -> None:
        self.destroyed: list[str] = []
        self.metric_refs: list[str] = []

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ref"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        raise AssertionError("exec_command must not be called in this test")

    async def destroy_workspace(self, provider_ref: str) -> None:
        self.destroyed.append(provider_ref)

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


class _SampledProvider(_MetricslessProvider):
    """Provider that reports a fixed metrics sample series through the ABC."""

    def __init__(self, samples: list[WorkspaceMetrics]) -> None:
        super().__init__()
        self._samples = samples

    async def get_metrics(self, provider_ref: str) -> list[WorkspaceMetrics]:
        self.metric_refs.append(provider_ref)
        return self._samples


def _resource_watchdog(handle: Any, *, cpu_cap: int = 50) -> Any:
    """Script-mode watchdog with resource caps enabled over *handle*."""
    return nr._SandboxWatchdog(
        sandbox=handle,
        stall=MagicMock(),
        node_id="n-metrics",
        run_id="r-metrics",
        watch_log_path=None,
        watch_globs=[],
        resource_limits={"cpu_usage_pct": cpu_cap},
        sandbox_mode="script",
        stdout_percentage_delta=None,
        stream_broker=None,
        drained_chunks=[],
        wall_clock=nr._WatchdogWallClock(None, 0.0),
    )


async def test_provider_mediated_handle_exposes_get_metrics_via_abc() -> None:
    """FAR-1050 R6: the mediated handle routes ``get_metrics`` through the ABC.

    The ADR 040 metrics gap is closed — the handle no longer deliberately
    omits the primitive, it delegates to ``provider.get_metrics(ref)`` so
    the resource-cap killer has something to poll on the provider path.
    """
    provider = _SampledProvider([WorkspaceMetrics(cpu_used_pct=99.0)])
    handle = _ProviderMediatedHandle(provider, "sbx-metrics")
    assert callable(getattr(handle, "get_metrics", None))

    samples = await handle.get_metrics()
    assert provider.metric_refs == ["sbx-metrics"]
    assert [s.cpu_used_pct for s in samples] == [99.0]


async def test_watchdog_enforces_resource_caps_through_abc_metrics_primitive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gap is CLOSED: an over-cap sample through the ABC primitive kills.

    Proves the whole provider-path chain end-to-end — handle ->
    ``provider.get_metrics(ref)`` -> ``_budget_exceeded`` -> budget kill
    (``destroy_workspace`` on the provider that created the workspace) —
    with NO ``resource_caps_not_enforced_via_provider`` warning.
    """
    provider = _SampledProvider([WorkspaceMetrics(cpu_used_pct=1.0), WorkspaceMetrics(cpu_used_pct=99.0)])
    watchdog = _resource_watchdog(_ProviderMediatedHandle(provider, "sbx-caps"))
    logger = "modulo.core.pipeline_engine.node_runner"
    with caplog.at_level(logging.WARNING, logger=logger):
        assert await watchdog.enforce_resource_limits() is True
    assert watchdog.budget_killed is True
    assert provider.metric_refs == ["sbx-caps"]
    assert provider.destroyed == ["sbx-caps"]
    assert not any("resource_caps_not_enforced_via_provider" in r.getMessage() for r in caplog.records)


async def test_watchdog_under_cap_sample_through_abc_does_not_kill() -> None:
    """The comparison still discriminates: an under-cap sample is not a kill."""
    provider = _SampledProvider([WorkspaceMetrics(cpu_used_pct=1.0)])
    watchdog = _resource_watchdog(_ProviderMediatedHandle(provider, "sbx-caps"))
    assert await watchdog.enforce_resource_limits() is False
    assert watchdog.budget_killed is False
    assert not provider.destroyed


class _NoMetricsSurfaceHandle:
    """A sandbox handle with NO ``get_metrics`` surface at all (FAR-1050 R6 shape 1).

    Stands in for any non-ABC handle the watchdog may be handed: with no
    primitive to poll, the resource-cap killer must fail OPEN loudly (the
    explicit, once-per-dispatch warning) and never attribute-error into the
    generic branch.
    """


async def test_watchdog_missing_metrics_surface_fails_open_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FAR-1050 R6 shape 1: a handle with NO ``get_metrics`` fails open loudly.

    The watchdog does not attribute-error on the missing primitive — it emits
    the explicit, once-per-dispatch
    ``sandbox_agent.resource_caps_not_enforced_via_provider`` warning and
    returns ``False``, so a measurement gap never kills the run.
    """
    watchdog = _resource_watchdog(_NoMetricsSurfaceHandle())
    logger = "modulo.core.pipeline_engine.node_runner"
    with caplog.at_level(logging.WARNING, logger=logger):
        assert await watchdog.enforce_resource_limits() is False
    gap_records = [
        r for r in caplog.records if r.getMessage() == "sandbox_agent.resource_caps_not_enforced_via_provider"
    ]
    assert len(gap_records) == 1
    assert "no get_metrics primitive" in gap_records[0].reason
    assert "NOT enforced" in gap_records[0].reason
    assert not any("resource_metrics_unavailable" in r.getMessage() for r in caplog.records)
    assert watchdog.budget_killed is False


async def test_watchdog_resource_cap_gap_fails_open_loudly_once_per_dispatch(caplog: pytest.LogCaptureFixture) -> None:
    """FAR-1050 R6: a provider WITHOUT the primitive still fails open loudly.

    The ABC default's typed ``ProviderCapabilityUnsupportedError`` is
    translated into the EXPLICIT, once-per-dispatch
    ``sandbox_agent.resource_caps_not_enforced_via_provider`` warning —
    NOT the generic transient 'metrics unavailable' traceback an operator
    would read as an SDK hiccup — the run never crashes, and the warning
    fires once per dispatch rather than once per poll tick.
    """
    handle = _ProviderMediatedHandle(_MetricslessProvider(), "sbx-gap")
    watchdog = _resource_watchdog(handle)
    logger = "modulo.core.pipeline_engine.node_runner"
    with caplog.at_level(logging.WARNING, logger=logger):
        assert await watchdog.enforce_resource_limits() is False
        assert await watchdog.enforce_resource_limits() is False
    gap_event = "sandbox_agent.resource_caps_not_enforced_via_provider"
    gap_records = [r for r in caplog.records if r.getMessage() == gap_event]
    assert len(gap_records) == 1
    assert "NOT enforced" in gap_records[0].reason
    assert "ADR 040" in gap_records[0].reason
    assert "does not implement get_metrics" in gap_records[0].reason
    assert not any("resource_metrics_unavailable" in r.getMessage() for r in caplog.records)
    assert watchdog.budget_killed is False


class _DoneEarlyChunks:
    """Yield chunks while ``done`` flips mid-stream, for the post-done pump join.

    A custom async iterator (not an async generator) so nothing is left
    suspended when the pump task dies on a raising callback — the watchdog's
    ``reached_done`` pump join at the end of ``_wait_command_with_exec_process``
    is the only thing under test.
    """

    def __init__(self, process: ExecProcess, items: list[ExecStreamChunk]) -> None:
        self._process = process
        self._items = list(items)
        self._index = 0

    def __aiter__(self) -> "_DoneEarlyChunks":
        return self

    async def __anext__(self) -> ExecStreamChunk:
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        # ``done`` fires while the pump is still mid-stream, so the watchdog
        # reaches its post-done pump join with the pump still running.
        self._process.done.set()
        return item


async def test_exec_process_watchdog_defaults_tick_interval_and_tolerates_absent_callbacks() -> None:
    """Omitting ``tick_interval`` uses ``_SANDBOX_TAIL_INTERVAL``; a stderr
    chunk with no ``on_stderr`` callback is simply buffered.
    """
    stream = _ScriptedStream(exit_code=0)
    stream.push("stdout", "o")
    stream.push("stderr", "e")
    stream.end()

    outcome, stall = await _wait_command_with_exec_process(
        stream.process,
        total_timeout=5.0,
        idle_timeout=5.0,
        last_activity=lambda: asyncio.get_running_loop().time(),
        # tick_interval omitted -> the _SANDBOX_TAIL_INTERVAL default.
        # on_stdout/on_stderr omitted -> the "callback absent" arms.
    )

    assert stall is None
    assert outcome.exit_code == 0
    assert outcome.stdout == "o"
    assert outcome.stderr == "e"


async def test_exec_process_watchdog_idle_kill_failure_still_reports_stall() -> None:
    """A failing kill is swallowed (best-effort) and the stall is still reported."""
    stream = _ScriptedStream(exit_code=None)

    async def _kill() -> None:
        raise RuntimeError("kill transport down")

    stream.process._kill = _kill

    outcome, stall = await _wait_command_with_exec_process(
        stream.process,
        total_timeout=10.0,
        idle_timeout=0.2,
        last_activity=lambda: 0.0,
        tick_interval=0.05,
    )

    assert outcome is None
    assert stall is not None
    assert "no output" in stall


async def test_exec_process_watchdog_swallows_a_dying_pump_after_done() -> None:
    """Once ``done`` has fired, a pump exception during the bounded join is
    best-effort (logged at debug) and never masks the stream outcome.
    """
    process = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]
    process.chunks = _DoneEarlyChunks(process, [ExecStreamChunk(stream="stdout", data="first")])

    async def _on_stdout(data: str) -> None:
        raise RuntimeError("pump callback blew up")

    outcome, stall = await _wait_command_with_exec_process(
        process,
        total_timeout=5.0,
        idle_timeout=5.0,
        last_activity=lambda: asyncio.get_running_loop().time(),
        tick_interval=0.05,
        on_stdout=_on_stdout,
    )

    assert stall is None
    # No healthy exit code was ever observed -> an explicit stream error, never
    # a fabricated zero exit.
    assert outcome.exit_code == -1
    assert outcome.stream_error == "stream ended without an exit code"


async def test_exec_process_watchdog_propagates_a_cancelled_pump_after_done() -> None:
    """A cancelled pump during the post-done join is NOT swallowed — it
    re-raises (cancellation is never converted into a fabricated outcome).
    """
    process = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]
    process.chunks = _DoneEarlyChunks(process, [ExecStreamChunk(stream="stdout", data="first")])

    async def _on_stdout(data: str) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _wait_command_with_exec_process(
            process,
            total_timeout=5.0,
            idle_timeout=5.0,
            last_activity=lambda: asyncio.get_running_loop().time(),
            tick_interval=0.05,
            on_stdout=_on_stdout,
        )


async def test_provider_empty_provider_ref_after_create_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A provider that returns an empty ref must fail closed, never build a
    handle addressed by the empty string."""
    _install_log_tail(monkeypatch)
    install_fake_dispatch(monkeypatch, ref="")

    result = await make_sandbox_agent_fn(_base_node_def())(_run_state())

    assert result["output"]["status"] == "failed"
    assert result["artifacts"][0]["output"]["error_type"] == "RuntimeProviderError"


async def test_provider_dispatch_provider_close_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A provider ``close()`` failure is best-effort: the dispatch still returns
    its real outcome."""
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-close-fail", exit_code=0)
    close = AsyncMock(side_effect=RuntimeError("close blew up"))
    dispatch.close = close  # type: ignore[method-assign]

    result = await make_sandbox_agent_fn(_base_node_def())(_run_state())

    assert result["output"]["status"] == "completed"
    close.assert_awaited_once()


async def test_provider_dispatch_provider_close_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A cancellation during ``close()`` re-raises — it is never swallowed."""
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-close-cancel", exit_code=0)
    dispatch.close = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await make_sandbox_agent_fn(_base_node_def())(_run_state())


# ---------------------------------------------------------------------------
# 13. FAR-1051: route-resolved provider reuse + route-hub disposal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RouteProfile:
    """Minimal stand-in for a bound environment profile row."""

    network_policy: str = "outbound"
    image_ref: str | None = None


@dataclass(frozen=True)
class _Route:
    """Minimal stand-in for ``RunnerDispatchRoute`` (FAR-1051 fields)."""

    provider_type: str
    profile: Any = None
    provider: Any = None
    hub: Any = None
    image_ref_override: str | None = None


class _RouteSession:
    """Minimal async-context-manager session for the route tests.

    ``_read_org_stdout_retention_ceiling`` opens ``session_factory()`` and
    awaits ``read_system_config`` on the yielded session. A bare ``MagicMock``
    there leaves unawaited ``__aenter__``/``__aexit__`` AsyncMock coroutines
    behind on every run (PytestUnraisableExceptionWarning); a real async CM
    keeps that read a clean, fail-open no-op.
    """

    def begin(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _route_session_factory() -> _RouteSession:
    return _RouteSession()


class _FileCapableDispatchProvider(FakeDispatchProvider):
    """FakeDispatchProvider + the ABC file/log primitives the K8s route drives."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.files: dict[str, bytes] = {}

    async def read_file(self, provider_ref: str, path: str) -> bytes:
        return bytes(self.files.get(path, b""))

    async def write_file(self, provider_ref: str, path: str, data: bytes) -> None:
        self.files[path] = bytes(data)

    async def list_files(self, provider_ref: str, path: str) -> list[str]:
        return sorted(self.files)

    async def get_info(self, provider_ref: str, path: str) -> Any:
        from modulo.core.runtime_provider import WorkspaceFileInfo

        data = self.files.get(path)
        return WorkspaceFileInfo(path=path, size=len(data) if data is not None else 0, is_dir=False)

    async def read_log_tail(self, provider_ref: str, *, max_bytes: int) -> bytes:
        return b"route-tail"


async def test_route_resolved_provider_is_reused_and_its_hub_is_aclosed(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """FAR-1051: a hub-resolved route's provider is reused directly — the
    dispatch never builds a second hub — and the route's hub is aclosed in the
    dispatch finally."""
    _install_log_tail(monkeypatch)
    provider = _FileCapableDispatchProvider(ref="sbx-route", exit_code=0)
    provider.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    hub = MagicMock()
    hub.aclose = AsyncMock()
    route = _Route(provider_type="kubernetes", profile=_RouteProfile(), provider=provider, hub=hub)
    monkeypatch.setattr(
        "modulo.core.bundled_runner.runner_dispatch.resolve_sandbox_dispatch_route",
        AsyncMock(return_value=route),
    )
    fresh_hub = AsyncMock(side_effect=AssertionError("a route-resolved provider must not build a fresh hub"))
    monkeypatch.setattr(nr, "_build_dispatch_provider", fresh_hub)

    result = await make_sandbox_agent_fn(_base_node_def(), session_factory=_route_session_factory())(_run_state())

    assert result["output"]["status"] == "completed"
    assert provider.events[0] == "create"
    fresh_hub.assert_not_awaited()
    hub.aclose.assert_awaited_once()
    # No profile image_ref -> the node's E2B template_id remains the pod image.
    assert provider.created_spec is not None
    assert provider.created_spec.image_ref == "opencode"


async def test_route_uses_the_kubernetes_profiles_image_ref(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """FAR-1051 review: on a kubernetes route the profile's declared image_ref is
    the pod image — the same source the bundled-runner mapper reads — so one
    profile resolves one image whichever dispatch arm runs it. Without this the
    sandbox route silently used the node's E2B template_id instead."""
    _install_log_tail(monkeypatch)
    provider = _FileCapableDispatchProvider(ref="sbx-route-image", exit_code=0)
    provider.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    hub = MagicMock()
    hub.aclose = AsyncMock()
    route = _Route(
        provider_type="kubernetes",
        profile=_RouteProfile(image_ref="ghcr.io/acme/agent:1.2.3"),
        provider=provider,
        hub=hub,
        image_ref_override="ghcr.io/acme/agent:1.2.3",
    )
    monkeypatch.setattr(
        "modulo.core.bundled_runner.runner_dispatch.resolve_sandbox_dispatch_route",
        AsyncMock(return_value=route),
    )

    result = await make_sandbox_agent_fn(_base_node_def(), session_factory=_route_session_factory())(_run_state())

    assert result["output"]["status"] == "completed"
    assert provider.created_spec is not None
    assert provider.created_spec.image_ref == "ghcr.io/acme/agent:1.2.3"


async def test_route_hub_aclose_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """A failing route-hub aclose is best-effort: the dispatch still completes."""
    _install_log_tail(monkeypatch)
    provider = _FileCapableDispatchProvider(ref="sbx-route-close-fail", exit_code=0)
    provider.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    hub = MagicMock()
    hub.aclose = AsyncMock(side_effect=RuntimeError("aclose boom"))
    route = _Route(provider_type="kubernetes", profile=_RouteProfile(), provider=provider, hub=hub)
    monkeypatch.setattr(
        "modulo.core.bundled_runner.runner_dispatch.resolve_sandbox_dispatch_route",
        AsyncMock(return_value=route),
    )

    result = await make_sandbox_agent_fn(_base_node_def(), session_factory=_route_session_factory())(_run_state())

    assert result["output"]["status"] == "completed"
    hub.aclose.assert_awaited_once()


async def test_route_hub_aclose_cancellation_propagates(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """A cancellation during the route-hub aclose re-raises — never swallowed."""
    _install_log_tail(monkeypatch)
    provider = _FileCapableDispatchProvider(ref="sbx-route-cancel", exit_code=0)
    provider.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    hub = MagicMock()
    hub.aclose = AsyncMock(side_effect=asyncio.CancelledError())
    route = _Route(provider_type="kubernetes", profile=_RouteProfile(), provider=provider, hub=hub)
    monkeypatch.setattr(
        "modulo.core.bundled_runner.runner_dispatch.resolve_sandbox_dispatch_route",
        AsyncMock(return_value=route),
    )

    with pytest.raises(asyncio.CancelledError):
        await make_sandbox_agent_fn(_base_node_def(), session_factory=_route_session_factory())(_run_state())


async def test_dispatch_drops_empty_attribution_values(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """An absent org id is dropped from the spec metadata (never stamped ``""``)."""
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-attr", exit_code=0)
    state = {"run_context": {"input": {}}, "_run_id": "run-1", "_pipeline_id": "pipe-1"}

    await make_sandbox_agent_fn(_base_node_def())(state)

    assert dispatch.created_spec is not None
    assert dispatch.created_spec.workspace_metadata["modulo.run.id"] == "run-1"
    assert "modulo.org.id" not in dispatch.created_spec.workspace_metadata
