"""FAR-1050 R1: flag-gated log-probe routing (``MODULO_E2B_VIA_PROVIDER``).

Drives the T6 call sites inside ``_sandbox_agent_impl`` through the real
dispatch (sandbox mock, no network) and proves:

1. Flag OFF (default): callers still hit urllib — the legacy
   ``_fetch_sandbox_log_tail`` runs, the provider builder never does.
2. Flag ON: callers hit the provider — ``FakeRuntimeProvider.read_log_tail``
   returns a fixed tail that lands in the failure message, the legacy
   helper and ``urllib`` are never touched (hostname absence at runtime),
   and the fetch still precedes the kill (pre-kill ordering preserved).
3. ``_read_log_tail_via_provider`` itself: empty on no key (provider never
   built), empty on provider build failure, empty on provider exception,
   decode of the provider's bytes on success.
4. The settings flag: default False, env-var enable True.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError,
    _build_log_tail_provider,
    _read_log_tail_via_provider,
    make_sandbox_agent_fn,
)
from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceSpec
from modulo.settings import Settings, get_settings

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"


# ---------------------------------------------------------------------------
# Fakes / harness (mirrors test_node_runner_sandbox's no-output scenario)
# ---------------------------------------------------------------------------


class FakeRuntimeProvider(RuntimeProvider):
    """``read_log_tail`` returns a fixed tail (plan R1 unit requirement)."""

    def __init__(self, tail: bytes) -> None:
        self.tail = tail
        self.calls: list[tuple[str, int]] = []

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ws-fake"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def destroy_workspace(self, provider_ref: str) -> None:
        return None

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"

    async def read_log_tail(self, provider_ref: str, *, max_bytes: int) -> bytes:
        self.calls.append((provider_ref, max_bytes))
        return self.tail


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
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


async def _completed_no_output_sandbox(sandbox_id: str) -> MagicMock:
    """Command completed but output.json missing → T6 site 2 fires."""
    cmd_result = MagicMock()
    cmd_result.exit_code = 1
    cmd_result.stdout = ""
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.sandbox_id = sandbox_id
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(return_value="")
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


def _sandbox_with_completed_command(sandbox_id: str, output_json: str) -> MagicMock:
    """Sandbox whose command completed, routing ``output.json`` vs the log file.

    The redirected agent log (any non-output.json path) reads empty so the
    drain probe sees no growth, while the declared output.json is returned
    verbatim — letting the schema-validation arm (site 3) fire.
    """

    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    def _read(path: str, format: str = "text", **kwargs: Any) -> str:
        return output_json if str(path).endswith("output.json") else ""

    sandbox = MagicMock()
    sandbox.sandbox_id = sandbox_id
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "modulo_e2b_via_provider", True)


def _disable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "modulo_e2b_via_provider", False)


# ---------------------------------------------------------------------------
# Settings flag
# ---------------------------------------------------------------------------


def test_flag_defaults_off() -> None:
    assert Settings(_env_file=None).modulo_e2b_via_provider is False


def test_flag_enabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_E2B_VIA_PROVIDER", "true")
    assert Settings(_env_file=None).modulo_e2b_via_provider is True


# ---------------------------------------------------------------------------
# _build_log_tail_provider: the REAL construction seam (not the injected fake)
# ---------------------------------------------------------------------------


async def test_build_log_tail_provider_constructs_real_e2b_provider() -> None:
    """The real builder body runs the hub factory and returns the E2B provider.

    No network: ``RuntimeProviderHub.initialise`` only constructs and registers
    the provider object (no E2B API call).  Every flag call-site test injects a
    fake builder, so this is the only test that executes the real body.
    """
    from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

    provider = await _build_log_tail_provider("test-key")
    assert isinstance(provider, E2BRuntimeProvider)


async def test_build_log_tail_provider_returns_none_when_hub_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hub/config failure is swallowed and surfaces as ``None`` (never raises)."""
    from modulo.core.runtime_provider.hub import RuntimeProviderHub

    async def _boom(self: RuntimeProviderHub, config: dict[str, Any]) -> None:
        raise RuntimeError("hub exploded")

    monkeypatch.setattr(RuntimeProviderHub, "initialise", _boom)
    assert await _build_log_tail_provider("test-key") is None


# ---------------------------------------------------------------------------
# Call-site routing: flag OFF → urllib, flag ON → provider
# ---------------------------------------------------------------------------


async def test_flag_off_caller_hits_urllib_not_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    builder = AsyncMock(side_effect=AssertionError("provider builder must not run when flag OFF"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_log_tail_provider", builder)

    import urllib.request

    payload = b'{"logEntries": [{"message": "legacy-urllib-tail", "level": "error"}]}'

    def _fake_urlopen(req: Any, timeout: Any) -> Any:
        class _Resp:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def read(self) -> bytes:
                return payload

        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    fn = make_sandbox_agent_fn(_base_node_def())
    sandbox = await _completed_no_output_sandbox("sbx-flagoff")
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    assert "legacy-urllib-tail" in str(excinfo.value)
    builder.assert_not_awaited()


async def test_flag_on_caller_hits_provider_not_urllib(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = FakeRuntimeProvider(b"provider-fixed-tail")
    builder = AsyncMock(return_value=fake)
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_log_tail_provider", builder)
    legacy = AsyncMock(side_effect=AssertionError("legacy helper must not run when flag ON"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail", legacy)
    # urlopen must never fire on the flag-ON path (hostname absence at runtime).
    monkeypatch.setattr("urllib.request.urlopen", AsyncMock(side_effect=AssertionError("urlopen must not run")))

    fn = make_sandbox_agent_fn(_base_node_def())
    sandbox = await _completed_no_output_sandbox("sbx-flagon")
    events: list[str] = []

    def _record_kill(*args: Any, **kwargs: Any) -> None:
        events.append("kill")

    sandbox.kill.side_effect = _record_kill
    fake_orig_read = fake.read_log_tail

    async def _recording_read(provider_ref: str, *, max_bytes: int) -> bytes:
        events.append("fetch")
        return await fake_orig_read(provider_ref, max_bytes=max_bytes)

    fake.read_log_tail = _recording_read  # type: ignore[method-assign]

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    message = str(excinfo.value)
    assert "provider-fixed-tail" in message
    builder.assert_awaited_once()
    legacy.assert_not_awaited()
    # provider primitive called with the dispatch's sandbox id + plan's bound
    assert fake.calls == [("sbx-flagon", 6000)]
    # pre-kill ordering preserved on the flag-ON path
    assert events[0] == "fetch"
    assert events.index("fetch") < events.index("kill")


async def test_flag_on_timeout_kill_path_uses_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """T6 site 1 (stalled/timed-out → pre-kill probe) routes through the provider.

    ``commands.run`` raises so ``cmd_result`` is None; the flag-ON arm must call
    ``_read_log_tail_via_provider`` (not the legacy urllib helper) before the kill.
    """
    _enable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = FakeRuntimeProvider(b"timeout-tail")
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_log_tail_provider",
        AsyncMock(return_value=fake),
    )
    legacy = AsyncMock(side_effect=AssertionError("legacy helper must not run when flag ON"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail", legacy)

    fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30))
    sandbox = MagicMock()
    sandbox.sandbox_id = "sbx-timeout"
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=TimeoutError("command timed out"))
    sandbox.files.read = AsyncMock(side_effect=TimeoutError("no output.json"))
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert fake.calls == [("sbx-timeout", 6000)]
    legacy.assert_not_awaited()


async def test_flag_on_schema_failure_path_uses_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """T6 site 3 (declared-schema violation → SandboxNodeFailedError) uses provider."""
    _enable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = FakeRuntimeProvider(b"schema-tail")
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_log_tail_provider",
        AsyncMock(return_value=fake),
    )
    legacy = AsyncMock(side_effect=AssertionError("legacy helper must not run when flag ON"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail", legacy)

    node_def = _base_node_def(timeout_seconds=30, output_schema_json={"required": ["status", "summary"]})
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _sandbox_with_completed_command("sbx-schema", '{"summary": "done"}')

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    assert "schema validation" in str(excinfo.value)
    assert fake.calls == [("sbx-schema", 6000)]
    legacy.assert_not_awaited()


async def test_flag_on_generic_exception_path_uses_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """T6 site 4 (generic exception envelope) routes through the provider."""
    _enable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = FakeRuntimeProvider(b"exc-tail")
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_log_tail_provider",
        AsyncMock(return_value=fake),
    )
    legacy = AsyncMock(side_effect=AssertionError("legacy helper must not run when flag ON"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail", legacy)

    fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30))
    sandbox = MagicMock()
    sandbox.sandbox_id = "sbx-exc"
    sandbox.files.write = AsyncMock(side_effect=RuntimeError("e2b file write exploded"))
    sandbox.files.read = AsyncMock(return_value="")
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock()
    sandbox.kill = AsyncMock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "failed"
    assert fake.calls == [("sbx-exc", 6000)]
    legacy.assert_not_awaited()


# ---------------------------------------------------------------------------
# _read_log_tail_via_provider unit behaviours
# ---------------------------------------------------------------------------


async def test_helper_returns_empty_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    builder = AsyncMock(side_effect=AssertionError("must not build a provider without a key"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_log_tail_provider", builder)
    assert not await _read_log_tail_via_provider("sbx-1")
    builder.assert_not_awaited()


async def test_helper_returns_empty_on_invalid_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = AsyncMock()
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_log_tail_provider", builder)
    assert not await _read_log_tail_via_provider(None)
    assert not await _read_log_tail_via_provider("")
    builder.assert_not_awaited()


async def test_helper_returns_empty_when_provider_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_log_tail_provider",
        AsyncMock(return_value=None),
    )
    assert not await _read_log_tail_via_provider("sbx-1")


async def test_helper_returns_empty_on_provider_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    exploding = MagicMock()
    exploding.read_log_tail = AsyncMock(side_effect=RuntimeError("provider backend down"))
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_log_tail_provider",
        AsyncMock(return_value=exploding),
    )
    assert not await _read_log_tail_via_provider("sbx-1")


async def test_helper_decodes_provider_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    fake = FakeRuntimeProvider("héllo tail".encode())
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_log_tail_provider",
        AsyncMock(return_value=fake),
    )
    assert await _read_log_tail_via_provider("sbx-1", max_bytes=128) == "héllo tail"
    assert fake.calls == [("sbx-1", 128)]
