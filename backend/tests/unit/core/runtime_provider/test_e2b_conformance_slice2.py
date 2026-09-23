"""FAR-1050 slice 2: E2B conformance surface (ADR 040).

Exercises both new primitives against FAKE E2B SDK objects — no network,
no real sandbox:

1. The ABC's ``destroy_workspace_by_ref`` default raises the typed
   ``ProviderCapabilityUnsupportedError`` (error honesty — never a raw
   ``NotImplementedError``), for a provider that does not override it.
2. E2B's ``destroy_workspace_by_ref`` reconnects via
   ``AsyncSandbox.connect(sandbox_id, api_key=...)`` (NEVER
   ``self._sandboxes``), is idempotent on already-gone/foreign refs
   (``NotFoundException`` → success, no raise), swallows kill failures
   (``_kill_sandbox_best_effort`` discipline → ``False``, logged), and
   drops the tracked local handle only on confirmed-gone.
3. E2B's ``exec_command_stream`` yields decoded chunks through
   ``process.chunks``, fires ``done`` on every stream end, keeps
   ``exit_code`` ``None`` until the END of a healthy stream (incl. a
   non-zero command exit via ``CommandExitException``), reports an
   engine/proxy drop through ``error`` (never a fabricated zero exit),
   and ``kill()`` calls through to the SDK command handle.
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from e2b.exceptions import NotFoundException
from e2b.sandbox.commands.command_handle import CommandExitException

from modulo.core.runtime_provider import (
    ExecStreamChunk,
    ProviderCapabilityUnsupportedError,
    RuntimeProvider,
    RuntimeProviderError,
    WorkspaceSpec,
)
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

# ---------------------------------------------------------------------------
# Fakes with the SDK's shape (e2b 2.6.1 — no network)
# ---------------------------------------------------------------------------


class _FakeCommandHandle:
    """Shape of ``e2b.sandbox_async.commands.command_handle.AsyncCommandHandle``.

    ``wait()`` resolves when the test (or ``kill()``) delivers the outcome —
    mirroring the real handle where SIGKILL produces an end event and
    ``wait()`` then raises ``CommandExitException`` for the non-zero exit.
    """

    def __init__(self) -> None:
        self._done = asyncio.Event()
        self._kind = "result"
        self._value: object = None
        self.kill_calls = 0

    async def wait(self) -> object:
        await self._done.wait()
        if self._kind == "exc":
            exc = self._value
            assert isinstance(exc, Exception)
            raise exc
        return self._value

    async def kill(self) -> bool:
        self.kill_calls += 1
        # Mirror the SDK: SIGKILL delivers an end event → wait() resolves
        # with the (non-zero) exit of the killed command.
        if not self._done.is_set():
            self.set_exception(CommandExitException(stdout="", stderr="", exit_code=137, error=None))
        return True

    def set_result(self, exit_code: int = 0) -> None:
        self._value = SimpleNamespace(exit_code=exit_code, stdout="", stderr="")
        self._kind = "result"
        self._done.set()

    def set_exception(self, exc: Exception) -> None:
        self._value = exc
        self._kind = "exc"
        self._done.set()


class _FakeCommands:
    """Shape of ``e2b.sandbox_async.commands.command.Commands``."""

    def __init__(self, handle: _FakeCommandHandle) -> None:
        self.handle = handle
        self.run_calls: list[tuple[str, dict[str, object]]] = []
        self.on_stdout: Any = None
        self.on_stderr: Any = None

    async def run(self, cmd: str, **kwargs: object) -> _FakeCommandHandle:
        self.run_calls.append((cmd, kwargs))
        self.on_stdout = kwargs.get("on_stdout")
        self.on_stderr = kwargs.get("on_stderr")
        return self.handle


class _FakeSandbox:
    """Shape of ``e2b.sandbox_async.main.AsyncSandbox`` (id/commands/kill)."""

    def __init__(self, sandbox_id: str = "sbx-fake-001") -> None:
        self.sandbox_id = sandbox_id
        self.handle = _FakeCommandHandle()
        self.commands = _FakeCommands(self.handle)
        self.kill = AsyncMock(return_value=True)
        self.is_running = AsyncMock(return_value=True)


def _provider_with_tracked(sandbox: _FakeSandbox, ref: str) -> E2BRuntimeProvider:
    provider = E2BRuntimeProvider(api_key="sk-test")
    provider._sandboxes[ref] = sandbox
    return provider


async def _settle(provider: E2BRuntimeProvider) -> None:
    """Yield until every stream-waiter task has finished (no leaked tasks)."""
    for _ in range(50):
        if not provider._stream_waiters:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"stream waiters still pending: {provider._stream_waiters!r}")


# ---------------------------------------------------------------------------
# 1. ABC default — typed capability refusal (error honesty)
# ---------------------------------------------------------------------------


class _NoDestroyByRefProvider(RuntimeProvider):
    """Concrete provider that does NOT override destroy_workspace_by_ref."""

    provider_id = "no-byref"

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ref"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> object:
        raise AssertionError("exec_command must not be called in this test")

    async def destroy_workspace(self, provider_ref: str) -> None:
        return None

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


async def test_abc_default_destroy_workspace_by_ref_raises_typed_capability_unsupported() -> None:
    """ADR 040 error honesty: the default raises the TYPED refusal, not NotImplementedError."""
    with pytest.raises(ProviderCapabilityUnsupportedError, match="destroy_workspace_by_ref") as exc_info:
        await _NoDestroyByRefProvider().destroy_workspace_by_ref("sbx-ref")

    assert isinstance(exc_info.value, RuntimeProviderError)
    assert not isinstance(exc_info.value, NotImplementedError)
    assert "NoDestroyByRefProvider" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. E2B destroy_workspace_by_ref — connect-by-id, idempotent, best-effort
# ---------------------------------------------------------------------------


async def test_destroy_by_ref_connects_by_sandbox_id_not_tracked_dict() -> None:
    """The primitive resolves the sandbox ONLY via AsyncSandbox.connect(ref)."""
    provider = E2BRuntimeProvider(api_key="sk-test")
    fake = _FakeSandbox("sbx-orphan-9")

    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.connect = AsyncMock(return_value=fake)
        result = await provider.destroy_workspace_by_ref("sbx-orphan-9")

    assert result is True
    mock_cls.connect.assert_awaited_once_with("sbx-orphan-9", api_key="sk-test")
    fake.kill.assert_awaited_once()


async def test_destroy_by_ref_already_gone_is_idempotent_success() -> None:
    """NotFoundException (already-destroyed / foreign ref) → True, never raised."""
    provider = E2BRuntimeProvider(api_key="sk-test")
    provider._sandboxes["sbx-gone"] = _FakeSandbox("sbx-gone")

    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.connect = AsyncMock(side_effect=NotFoundException("sandbox not found"))
        result = await provider.destroy_workspace_by_ref("sbx-gone")

    assert result is True
    # Stale local handle is dropped too — the substrate says it is gone.
    assert "sbx-gone" not in provider._sandboxes


async def test_destroy_by_ref_swallows_kill_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A kill failure is logged and swallowed → False (destroy not confirmed)."""
    provider = E2BRuntimeProvider(api_key="sk-test")
    fake = _FakeSandbox("sbx-killfail")
    fake.kill = AsyncMock(side_effect=RuntimeError("E2B API error"))
    provider._sandboxes["sbx-killfail"] = fake

    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.connect = AsyncMock(return_value=fake)
        result = await provider.destroy_workspace_by_ref("sbx-killfail")

    assert result is False
    assert "Failed to kill E2B sandbox" in caplog.text
    # Unconfirmed destroy: the tracked handle is KEPT so close() can retry.
    assert "sbx-killfail" in provider._sandboxes


async def test_destroy_by_ref_connect_failure_returns_false_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A non-NotFound connect failure cannot confirm destruction → False, logged."""
    provider = E2BRuntimeProvider(api_key="sk-test")

    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.connect = AsyncMock(side_effect=RuntimeError("control plane unreachable"))
        result = await provider.destroy_workspace_by_ref("sbx-x")

    assert result is False
    assert "failed to reconnect to sandbox sbx-x" in caplog.text


async def test_destroy_by_ref_confirmed_kill_pops_tracked_handle() -> None:
    """A confirmed kill drops the now-stale local handle."""
    provider = E2BRuntimeProvider(api_key="sk-test")
    fake = _FakeSandbox("sbx-tracked")
    provider._sandboxes["sbx-tracked"] = fake

    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.connect = AsyncMock(return_value=fake)
        result = await provider.destroy_workspace_by_ref("sbx-tracked")

    assert result is True
    assert "sbx-tracked" not in provider._sandboxes
    fake.kill.assert_awaited_once()


async def test_destroy_by_ref_cancellation_propagates() -> None:
    """Cancellation is never swallowed by the best-effort wrapper."""
    provider = E2BRuntimeProvider(api_key="sk-test")

    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.connect = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await provider.destroy_workspace_by_ref("sbx-c")


# ---------------------------------------------------------------------------
# 3. E2B exec_command_stream — chunks / done / exit_code / error / kill
# ---------------------------------------------------------------------------


async def test_exec_stream_yields_chunks_sets_done_and_exit_code() -> None:
    """Healthy stream: decoded chunks in order, done fires, real exit code lands."""
    fake = _FakeSandbox()
    provider = _provider_with_tracked(fake, "sbx-1")

    process = await provider.exec_command_stream(
        "sbx-1",
        ["bash", "-lc", "echo hi"],
        environment={"A": "1"},
    )

    # Contract: exit_code stays None until the END of the stream; done unset.
    assert process.exit_code is None
    assert not process.done.is_set()

    # Inject output through the SDK callbacks the provider registered.
    await fake.commands.on_stdout("out-1\n")
    await fake.commands.on_stderr("err-1\n")
    fake.handle.set_result(exit_code=3)

    chunks = [chunk async for chunk in process.chunks]

    assert [c.stream for c in chunks] == ["stdout", "stderr"]
    assert [c.data for c in chunks] == ["out-1\n", "err-1\n"]
    assert isinstance(chunks[0], ExecStreamChunk)
    await asyncio.wait_for(process.done.wait(), timeout=1)
    assert process.exit_code == 3
    assert process.error is None
    await _settle(provider)


async def test_exec_stream_passes_environment_and_unbounded_stream_timeout() -> None:
    """run() receives envs, background=True and timeout=0 (dispatch owns kills)."""
    fake = _FakeSandbox()
    provider = _provider_with_tracked(fake, "sbx-1")

    process = await provider.exec_command_stream(
        "sbx-1",
        ["bash", "-lc", "echo hi"],
        environment={"A": "1"},
    )

    cmd, kwargs = fake.commands.run_calls[0]
    assert cmd == "bash -lc 'echo hi'"
    assert kwargs["background"] is True
    assert kwargs["envs"] == {"A": "1"}
    assert kwargs["timeout"] == 0

    fake.handle.set_result(exit_code=0)
    _ = [chunk async for chunk in process.chunks]
    await _settle(provider)


async def test_exec_stream_nonzero_command_exit_is_healthy_end() -> None:
    """CommandExitException carries a REAL non-zero exit — a result, not a stream error."""
    fake = _FakeSandbox()
    provider = _provider_with_tracked(fake, "sbx-1")

    process = await provider.exec_command_stream("sbx-1", ["false"])

    await fake.commands.on_stdout("before-fail")
    fake.handle.set_exception(CommandExitException(stdout="", stderr="boom", exit_code=127, error=None))

    chunks = [chunk async for chunk in process.chunks]

    assert [c.data for c in chunks] == ["before-fail"]
    await asyncio.wait_for(process.done.wait(), timeout=1)
    assert process.exit_code == 127
    assert process.error is None
    await _settle(provider)


async def test_exec_stream_drop_sets_error_never_fabricates_zero_exit() -> None:
    """Engine/proxy drop mid-stream: error set, exit_code stays None, done fires."""
    fake = _FakeSandbox()
    provider = _provider_with_tracked(fake, "sbx-1")

    process = await provider.exec_command_stream("sbx-1", ["long-running"])

    await fake.commands.on_stdout("partial")
    fake.handle.set_exception(ConnectionResetError("proxy dropped"))

    chunks = [chunk async for chunk in process.chunks]

    # Output delivered before the drop is preserved.
    assert [c.data for c in chunks] == ["partial"]
    await asyncio.wait_for(process.done.wait(), timeout=1)
    assert process.error is not None
    assert "proxy dropped" in process.error
    # NEVER a fabricated zero-exit completion on a dropped stream.
    assert process.exit_code is None
    await _settle(provider)


async def test_exec_stream_kill_calls_through_to_sdk_handle() -> None:
    """kill() calls the SDK handle's kill; the end event then completes the stream."""
    fake = _FakeSandbox()
    provider = _provider_with_tracked(fake, "sbx-1")

    process = await provider.exec_command_stream("sbx-1", ["sleep", "30"])

    await fake.commands.on_stdout("started")
    await process.kill()

    assert fake.handle.kill_calls == 1

    chunks = [chunk async for chunk in process.chunks]
    assert [c.data for c in chunks] == ["started"]
    await asyncio.wait_for(process.done.wait(), timeout=1)
    # The kill's end event delivered the real killed-command exit code.
    assert process.exit_code == 137
    assert process.error is None
    await _settle(provider)


async def test_exec_stream_unknown_sandbox_raises() -> None:
    """Unknown ref fails fast — same ValueError shape as exec_command."""
    provider = E2BRuntimeProvider(api_key="sk-test")

    with pytest.raises(ValueError, match="Unknown sandbox"):
        await provider.exec_command_stream("missing", ["echo", "hi"])
