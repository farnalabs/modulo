"""FAR-1050 R4: the dispatch rewire (create / stream / kill) behind the flag.

One proof per row of the design doc's binding risk table
(``docs/design/e2b-provider-conformance-rewire.md`` §5), all without a live
E2B sandbox:

1. **Full ``_sandbox_agent_impl`` run flag-ON** with a fake provider: the ABC
   call sequence, the dispatch marker written BEFORE create, cancellation
   propagation, no zero-exit fabrication on a stream error, retry
   classification unchanged.
2. **Flag-OFF run asserts ZERO provider calls** (every seam refuses to run).
3. **Streaming semantics**: ordered chunks, ``exit_code is None`` + ``error``
   set on a proxy drop, ``done`` fires on an early consumer close.
4. **Watchdog**: a slice timeout never cancels the underlying wait (the
   FAR-97/98 ``cancelling()==0`` invariant, re-expressed over
   ``ExecProcess.done``).
5. **Retry classification**: provider ``RateLimitedError`` /
   ``ProvisionTimeoutError`` map to the EXISTING retryable codes; no new
   ``harness.unknown`` outcome on a flag-ON rate limit; no re-parenting.
6. **Cost stamping**: flag ON/OFF produce identical ``cost_estimate_usd`` for
   a fixed elapsed + output fixture.
7. **Marker / telemetry attribution**: ``provider`` + flag state on BOTH paths.
8. **T10**: flag ON hands ``provision_workspace_inputs_in_sandbox`` the
   ABC-mediated handle (four commands round-trip through the fake provider);
   flag OFF hands it the legacy handle.
9. **S2/S3/S4** (org-deletion / pipeline-execution / evidence kill sites):
   never read the flag and work with it ON and OFF.
"""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine import node_runner as nr
from modulo.core.pipeline_engine import workspace_input_orchestration as wio
from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError,
    SandboxQueueTimeoutError,
    SandboxRateLimitedError,
    SandboxTierRefusedError,
    _build_dispatch_provider,
    _dispatch_marker_json,
    _dispatch_via_provider_enabled,
    _ExecStreamOutcome,
    _ProviderMediatedHandle,
    _require_dispatch_provider,
    _translate_provider_dispatch_error,
    _wait_command_with_exec_process,
    make_sandbox_agent_fn,
)
from modulo.core.runtime_provider import (
    ExecProcess,
    ExecStreamChunk,
    ProvisionTimeoutError,
    RateLimitedError,
    RuntimeProviderError,
    WorkspaceSpec,
)
from modulo.settings import Settings, get_settings
from tests.unit.pipeline_engine.conftest import install_fake_dispatch

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
_COMPLETED_OUTPUT = '{"status": "completed", "summary": "done"}'


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "modulo_e2b_via_provider", True)


def _disable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "modulo_e2b_via_provider", False)


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


def _legacy_sandbox(sandbox_id: str, output_json: str, *, exit_code: int = 0) -> MagicMock:
    """A flag-OFF dispatch sandbox whose command completes with ``output.json``."""
    cmd_result = MagicMock()
    cmd_result.exit_code = exit_code
    cmd_result.stdout = "legacy stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)
    # The duck-typed helpers call ``sandbox.commands.run`` directly, so the
    # handle doubles as their result: empty stdout keeps the drift probe's
    # string handling on real ``str`` rather than an auto-MagicMock.
    handle.stdout = ""

    def _read(path: str, *args: Any, **kwargs: Any) -> str:
        return output_json if str(path).endswith("output.json") else ""

    sandbox = MagicMock()
    sandbox.sandbox_id = sandbox_id
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.files.list = AsyncMock(return_value=[])
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


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
# 1. Flag + seam unit
# ---------------------------------------------------------------------------


def test_flag_defaults_off_and_reader_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).modulo_e2b_via_provider is False
    _disable_flag(monkeypatch)
    assert _dispatch_via_provider_enabled() is False
    _enable_flag(monkeypatch)
    assert _dispatch_via_provider_enabled() is True
    # Fail-open: an unreadable settings store resolves to the flag's default
    # (OFF = the legacy direct path), never to a crash.
    monkeypatch.setattr(nr, "get_settings", MagicMock(side_effect=RuntimeError("settings down")))
    assert _dispatch_via_provider_enabled() is False


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
# 2. Full flag-ON dispatch: ABC call sequence + marker-before-create
# ---------------------------------------------------------------------------


async def test_flag_on_full_dispatch_runs_the_abc_call_sequence(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """marker -> create -> command -> by-ref destroy -> close.

    The dispatch marker is acquired BEFORE ``create_workspace``, and every
    lifecycle step of the run happens on the provider — the legacy
    ``AsyncSandbox.create`` is never reached (asserted below).
    """
    _enable_flag(monkeypatch)
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()

    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-r4-seq", exit_code=0)
    _spy_dispatch_marker(monkeypatch, dispatch.events)

    legacy_create = AsyncMock(side_effect=AssertionError("AsyncSandbox.create must not run when flag ON"))
    fn = make_sandbox_agent_fn(_base_node_def())
    with patch("e2b.AsyncSandbox.create", new=legacy_create):
        result = await fn(_run_state())

    assert dispatch.events == ["marker", "create", "command", "destroy_by_ref", "close"], dispatch.events
    legacy_create.assert_not_awaited()
    # The provider really provisioned with the dispatch's own WorkspaceSpec.
    assert isinstance(dispatch.created_spec, WorkspaceSpec)
    assert dispatch.created_spec.image_ref == "opencode"
    # ...and the agent command really went through the ABC stream with the
    # dispatch envs (MODULO_SCHEMA_DIR rides on the command env flag-ON).
    assert dispatch.last_command is not None
    assert dispatch.last_command[0] == "sh"
    assert dispatch.last_environment is not None
    assert dispatch.last_environment["MODULO_SCHEMA_DIR"] == "/home/user/schemas"

    assert result["output"]["status"] == "completed"
    assert result["artifacts"][0]["output"]["exit_code"] == 0


async def test_flag_off_full_dispatch_makes_zero_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag OFF (the default): NOT ONE provider seam may be constructed."""
    _disable_flag(monkeypatch)
    refusals: dict[str, AsyncMock] = {}
    for seam in (
        "_build_dispatch_provider",
        "_build_file_io_provider",
        "_build_log_tail_provider",
        "_build_isolation_provider",
    ):
        refusal = AsyncMock(side_effect=AssertionError(f"{seam} must not run when the flag is OFF"))
        monkeypatch.setattr(f"modulo.core.pipeline_engine.node_runner.{seam}", refusal)
        refusals[seam] = refusal

    sandbox = _legacy_sandbox("sbx-flagoff-r4", _COMPLETED_OUTPUT)
    fn = make_sandbox_agent_fn(_base_node_def())
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as legacy_create:
        result = await fn(_run_state())

    legacy_create.assert_awaited_once()
    assert result["output"]["status"] == "completed"
    for seam, refusal in refusals.items():
        assert refusal.await_count == 0, seam
    # The legacy handle really was the one that ran.
    assert sandbox.commands.run.await_count == 1
    assert sandbox.kill.await_count >= 1


# ---------------------------------------------------------------------------
# 3. Cancellation propagates (never swallowed, never converted to ExecResult(-1))
# ---------------------------------------------------------------------------


async def test_flag_on_cancelled_create_propagates(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    _enable_flag(monkeypatch)
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


async def test_flag_on_cancelled_stream_start_propagates(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    _enable_flag(monkeypatch)
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


async def test_flag_on_stream_error_never_fabricates_a_zero_exit(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """A proxy drop surfaces as ``exit_code = -1`` + failed status, never 0."""
    _enable_flag(monkeypatch)
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


async def test_flag_on_healthy_non_zero_exit_is_a_result_not_a_stream_error(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A command that exits non-zero on a HEALTHY stream keeps its real code."""
    _enable_flag(monkeypatch)
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


async def test_flag_on_rate_limit_enters_the_existing_backoff_then_queue_timeout(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The provider's typed rate limit drives the SAME bounded backoff loop."""
    _enable_flag(monkeypatch)
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


async def test_flag_on_wrapped_sdk_rate_limit_still_backs_off(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """The E2B provider wraps SDK failures — the ``RateLimitException`` on
    ``__cause__`` must still reach the backoff loop (no ``harness.unknown``)."""
    from e2b.exceptions import RateLimitException

    _enable_flag(monkeypatch)
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


async def test_flag_on_typed_rate_limit_escaping_a_provider_call_is_retryable(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A ``RateLimitedError`` from ANY provider call (here: a file write) is
    re-raised as the pre-existing retryable class — never swallowed into a
    synthetic ``harness.unknown`` envelope."""
    _enable_flag(monkeypatch)
    _install_log_tail(monkeypatch)
    install_fake_dispatch(monkeypatch, ref="sbx-429-outside")
    fake_file_io.write_error = RateLimitedError("429 concurrent sandbox limit")

    with pytest.raises(SandboxRateLimitedError):
        await make_sandbox_agent_fn(_base_node_def())(_run_state())


async def test_flag_on_provision_timeout_escaping_a_provider_call_is_retryable(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    _enable_flag(monkeypatch)
    _install_log_tail(monkeypatch)
    install_fake_dispatch(monkeypatch, ref="sbx-provision-timeout-outside")
    fake_file_io.write_error = ProvisionTimeoutError("workspace never became ready")

    with pytest.raises(SandboxQueueTimeoutError):
        await make_sandbox_agent_fn(_base_node_def())(_run_state())


async def test_flag_off_non_provider_failure_still_builds_the_legacy_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The translation seam must not touch the flag-OFF generic envelope."""
    _disable_flag(monkeypatch)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    sandbox = _legacy_sandbox("sbx-exc-off", "")
    sandbox.files.write = AsyncMock(side_effect=RuntimeError("legacy write exploded"))

    fn = make_sandbox_agent_fn(_base_node_def())
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "failed"
    assert result["artifacts"][0]["output"]["error_type"] == "RuntimeError"
    assert result["artifacts"][0]["output"]["via_provider"] is False


# ---------------------------------------------------------------------------
# 7. Egress fail-closed on the provider create path (ADR 040 egress rule)
# ---------------------------------------------------------------------------


async def test_flag_on_egress_restricted_policy_is_refused_not_granted(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The provider create cannot express ``allow_internet_access`` yet, so a
    restrictive policy must REFUSE (terminal named code), never silently grant
    permissive egress."""
    _enable_flag(monkeypatch)
    _install_log_tail(monkeypatch)
    legacy_create = AsyncMock(side_effect=AssertionError("AsyncSandbox.create must not run when flag ON"))
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-egress")

    fn = make_sandbox_agent_fn(_base_node_def(egress_policy="deny_all"))
    with (
        patch("e2b.AsyncSandbox.create", new=legacy_create),
        pytest.raises(SandboxTierRefusedError) as excinfo,
    ):
        await fn(_run_state())

    legacy_create.assert_not_awaited()
    assert "egress" in str(excinfo.value)
    # The refusal is terminal (never a retry loop).
    from modulo.core.pipeline_engine.runtime_retry import _NEVER_RETRYABLE_NAMES

    assert "SandboxTierRefusedError" in _NEVER_RETRYABLE_NAMES
    # No workspace was ever provisioned.
    assert "create" not in dispatch.events


async def test_flag_off_egress_deny_all_still_uses_the_legacy_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag OFF: the legacy ``allow_internet_access=False`` arm is untouched."""
    _disable_flag(monkeypatch)
    sandbox = _legacy_sandbox("sbx-egress-off", _COMPLETED_OUTPUT)
    create = AsyncMock(return_value=sandbox)

    fn = make_sandbox_agent_fn(_base_node_def(egress_policy="deny_all"))
    with patch("e2b.AsyncSandbox.create", new=create):
        await fn(_run_state())

    assert create.await_args.kwargs["allow_internet_access"] is False


# ---------------------------------------------------------------------------
# 8. Cost stamping parity
# ---------------------------------------------------------------------------


async def test_cost_estimate_is_identical_for_flag_on_and_off(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """Fixed elapsed + fixed output => identical ``cost_estimate_usd``.

    The elapsed component is pinned by wrapping ``_compute_sandbox_cost`` so
    the comparison is about the DISPATCH (which output fixture reaches the
    function, and that the flag-ON path calls it at all), not about wall
    clock. Both paths must call the SAME sanctioned helper with the SAME
    output and stamp its result unchanged.
    """
    real = nr._compute_sandbox_cost
    pinned_elapsed = 123.456
    calls: list[tuple[float, Any]] = []

    def _pinned(elapsed: float, output_json: Any) -> float:
        calls.append((elapsed, output_json))
        return real(pinned_elapsed, output_json)

    fixture = {"status": "completed", "summary": "done", "cost_estimate_usd": 1.25}
    encoded = json.dumps(fixture).encode()

    # --- flag ON ---
    _enable_flag(monkeypatch)
    monkeypatch.setattr(nr, "_compute_sandbox_cost", _pinned)
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = encoded
    install_fake_dispatch(monkeypatch, ref="sbx-cost-on", exit_code=0)
    fn_on = make_sandbox_agent_fn(_base_node_def())
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(side_effect=AssertionError("no legacy create"))):
        on_result = await fn_on(_run_state())

    # --- flag OFF ---
    _disable_flag(monkeypatch)
    legacy = _legacy_sandbox("sbx-cost-off", json.dumps(fixture))
    fn_off = make_sandbox_agent_fn(_base_node_def())
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=legacy)):
        off_result = await fn_off(_run_state())

    on_cost = on_result["output"]["cost_estimate_usd"]
    off_cost = off_result["output"]["cost_estimate_usd"]
    assert len(calls) == 2, "both paths must stamp through _compute_sandbox_cost exactly once"
    assert on_cost == off_cost
    assert on_cost == real(pinned_elapsed, fixture)


# ---------------------------------------------------------------------------
# 9. Marker / telemetry attribution on BOTH paths
# ---------------------------------------------------------------------------


async def test_dispatch_marker_and_telemetry_carry_provider_and_flag_state(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """Both flag states stamp ``provider`` + ``via_provider`` on the marker
    rewrite AND on the node telemetry (envelope)."""
    stamped = _capture_marker_store(monkeypatch)
    results: dict[bool, dict[str, Any]] = {}

    _enable_flag(monkeypatch)
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    install_fake_dispatch(monkeypatch, ref="sbx-attr-on", exit_code=0)
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(side_effect=AssertionError("no legacy create"))):
        results[True] = await make_sandbox_agent_fn(_base_node_def())(_run_state())

    _disable_flag(monkeypatch)
    legacy = _legacy_sandbox("sbx-attr-off", _COMPLETED_OUTPUT)
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=legacy)):
        results[False] = await make_sandbox_agent_fn(_base_node_def())(_run_state())

    for flag_on in (True, False):
        result = results[flag_on]
        # Node telemetry (the P1b splitter folds these verbatim).
        assert result["output"]["provider"] == "e2b"
        assert result["output"]["via_provider"] is flag_on
        assert result["artifacts"][0]["output"]["via_provider"] is flag_on

    assert len(stamped) == 2
    for entry in stamped:
        assert entry["provider"] == "e2b"
        assert entry["via_provider"] in (True, False)
        assert entry["sandbox_id"]
    assert {e["via_provider"] for e in stamped} == {True, False}


async def test_marker_telemetry_lands_in_node_telemetry_json(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """The attribution keys survive the envelope -> telemetry split."""
    from modulo.core.node_output_split import split_node_output

    _enable_flag(monkeypatch)
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    install_fake_dispatch(monkeypatch, ref="sbx-telemetry", exit_code=0)
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(side_effect=AssertionError("no legacy create"))):
        result = await make_sandbox_agent_fn(_base_node_def())(_run_state())

    _, telemetry = split_node_output(result, "sandbox_agent", None, node_id="n1")
    assert telemetry["provider"] == "e2b"
    assert telemetry["via_provider"] is True


def test_dispatch_marker_json_carries_provider_and_flag_and_stays_readable() -> None:
    """ADR 040 marker schema versioning: the extra key never breaks a reader."""
    from modulo.core.runner_capacity import parse_marker_state

    for flag_on in (True, False):
        raw = _dispatch_marker_json("run:1:node:n:2", "e2b", via_provider=flag_on)
        payload = json.loads(raw)
        assert payload["state"] == "dispatching"
        assert payload["attempt_key"] == "run:1:node:n:2"
        assert payload["provider"] == "e2b"
        assert payload["via_provider"] is flag_on
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


def _background_free_calls(mock: AsyncMock) -> list[Any]:
    """The calls that are NOT the agent command's background start."""
    return [c for c in mock.await_args_list if not c.kwargs.get("background")]


async def test_flag_on_workspace_inputs_receive_the_abc_mediated_handle(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The helper gets the ABC-mediated handle and its four commands
    (setup + clone + teardown + drift probe) round-trip the fake provider."""
    _enable_flag(monkeypatch)
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
    assert dispatch.last_command[0] == "sh"


async def test_flag_off_workspace_inputs_receive_the_legacy_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_flag(monkeypatch)
    seen = _patch_workspace_inputs(monkeypatch)
    sandbox = _legacy_sandbox("sbx-t10-off", _COMPLETED_OUTPUT)

    node_def = _base_node_def(workspace_inputs=[{"dest": "repos/example", "ref": {"kind": "branch"}}])
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await make_sandbox_agent_fn(node_def)(_run_state())

    assert result["output"]["status"] == "completed"
    assert len(seen) == 1
    assert seen[0] is sandbox
    # The four helper commands went through the legacy handle (the fifth
    # ``commands.run`` call is the agent command's own background start).
    assert len(_background_free_calls(sandbox.commands.run)) == 4


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


async def test_s3_run_watchdog_kill_works_with_the_flag_on_and_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pipeline_execution._kill_sandbox_best_effort`` never raises and its
    behaviour is identical in both flag states."""
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

    for flag_on in (True, False):
        if flag_on:
            _enable_flag(monkeypatch)
        else:
            _disable_flag(monkeypatch)
        killed.clear()
        connect.reset_mock()
        with patch("e2b.AsyncSandbox.connect", new=connect):
            await pe._kill_sandbox_best_effort(_Engine(), "run-1", "org-1")  # type: ignore[arg-type]
        assert killed == ["killed"]
        assert connect.await_count == 1


async def test_s2_org_deletion_kill_works_with_the_flag_on_and_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        for flag_on in (True, False):
            if flag_on:
                _enable_flag(monkeypatch)
            else:
                _disable_flag(monkeypatch)
            kill.reset_mock()
            assert await od._abort_org_live_sandboxes(_Session(), uuid.UUID(_ORG_ID)) == 1  # type: ignore[arg-type]
            assert kill.await_count == 1
            assert kill.await_args.args[0] == "sbx-s2"


async def test_s4_evidence_probes_work_with_the_flag_on_and_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    for flag_on in (True, False):
        if flag_on:
            _enable_flag(monkeypatch)
        else:
            _disable_flag(monkeypatch)
        with patch("e2b.AsyncSandbox.connect", new=AsyncMock(return_value=handle)):
            result = await ev._e2b_run_command("sbx-s4", "echo probe-ok")
            files = await ev._e2b_list_files("sbx-s4")
        assert result.stdout == "probe-ok"
        assert [f.name for f in files] == ["output.json"]


# ---------------------------------------------------------------------------
# 12. Legacy branch still present (flag OFF is the default revert point)
# ---------------------------------------------------------------------------


def test_flag_off_branches_stay_in_the_source() -> None:
    """Design §2: the legacy direct branch is NOT deleted by R4."""
    source = Path(nr.__file__).read_text(encoding="utf-8")
    assert "AsyncSandbox.create(" in source
    assert "sandbox.commands.run(" in source
    assert "sandbox.kill(request_timeout=" in source
    # ...and every R4 site is gated on the flag local.
    assert source.count("_via_provider_dispatch") >= 5


# ---------------------------------------------------------------------------
# 13. Flag-ON seam error arms (every defensive branch is reachable + tested)
# ---------------------------------------------------------------------------


def test_require_dispatch_spec_fails_closed_with_typed_error() -> None:
    """The create-spec narrow refuses a missing spec with the typed error.

    Companion to ``test_require_dispatch_provider_fails_closed_with_typed_error``:
    the flag-ON create loop must never reach ``create_workspace`` without a
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


async def test_flag_on_empty_provider_ref_after_create_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A provider that returns an empty ref must fail closed, never build a
    handle addressed by the empty string."""
    _enable_flag(monkeypatch)
    _install_log_tail(monkeypatch)
    install_fake_dispatch(monkeypatch, ref="")

    result = await make_sandbox_agent_fn(_base_node_def())(_run_state())

    assert result["output"]["status"] == "failed"
    assert result["artifacts"][0]["output"]["error_type"] == "RuntimeProviderError"


async def test_flag_on_dispatch_provider_close_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A provider ``close()`` failure is best-effort: the dispatch still returns
    its real outcome."""
    _enable_flag(monkeypatch)
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-close-fail", exit_code=0)
    close = AsyncMock(side_effect=RuntimeError("close blew up"))
    dispatch.close = close  # type: ignore[method-assign]

    result = await make_sandbox_agent_fn(_base_node_def())(_run_state())

    assert result["output"]["status"] == "completed"
    close.assert_awaited_once()


async def test_flag_on_dispatch_provider_close_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A cancellation during ``close()`` re-raises — it is never swallowed."""
    _enable_flag(monkeypatch)
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-close-cancel", exit_code=0)
    dispatch.close = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await make_sandbox_agent_fn(_base_node_def())(_run_state())
