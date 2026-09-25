"""FAR-1050 R2b: flag-gated file-I/O call-site rewire (site T4, 13 sites).

Proves the three things the rewire plan asks of this slice, all without a live
E2B sandbox:

1. **Bytes-in/bytes-out round-trip.** ``_write_file_via_provider`` UTF-8-encodes
   the text the legacy arm passes as ``str``; ``_read_file_via_provider`` decodes
   the primitive's bytes back to the same text — and the provider is addressed
   with the dispatch's sandbox id.
2. **Write-before-command ordering.** Every flag-ON write still lands before
   ``sandbox.commands.run`` starts the agent.
3. **Drain-window equivalence.** A scripted log drives the legacy
   ``sandbox.files`` arm and the flag-ON provider arm tick-for-tick; both
   produce identical drained chunks, offset and retained length — including the
   window truncation.

Plus the routing invariants: with the flag ON **no** ``sandbox.files`` call is
reachable in ``node_runner.py`` (asserted through a handle that records every
attribute touch, so a swallowed exception cannot hide a legacy-path call), and
the flag-OFF arm never touches the provider seam.
"""

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine import node_runner as nr
from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError,
    _build_file_io_provider,
    _file_io_provider_for,
    _file_io_via_provider_enabled,
    _get_info_via_provider,
    _list_fs_entries_via_provider,
    _read_file_via_provider,
    _write_file_via_provider,
    make_sandbox_agent_fn,
)
from modulo.core.runtime_provider import RuntimeProviderError, WorkspaceFileInfo
from modulo.settings import Settings, get_settings
from tests.unit.pipeline_engine.conftest import FakeFileIOProvider

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
_PROMPT_PATH = "/home/user/prompt.md"


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
        "_run_id": "run-r2b",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


def _sandbox_mock(sandbox_id: str) -> MagicMock:
    """A dispatch sandbox whose command finishes without producing output.json."""
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
    sandbox.files.list = AsyncMock(return_value=[])
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


class _ForbiddenFiles:
    """Records every attribute touch on ``sandbox.files`` and raises.

    The dispatch swallows some file failures (the output.json read sits in a
    try/except), so an AssertionError alone could be masked — ``touched`` is
    the assertion surface: it must stay empty on the flag-ON path.
    """

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.touched.append(name)
        raise AssertionError(f"sandbox.files.{name} must not be touched while MODULO_E2B_VIA_PROVIDER is ON")


def _watchdog(
    *,
    sandbox: Any,
    watch_globs: list[str] | None = None,
    watch_log_path: str | None = None,
    drain_window_bytes: int | None = None,
) -> nr._SandboxWatchdog:
    stall = nr._configure_stall_detector(
        enable_heartbeat=True,
        watch_log_path=watch_log_path,
        stdout_percentage_delta=None,
        watch_globs=watch_globs or [],
    )
    return nr._SandboxWatchdog(
        sandbox=sandbox,
        stall=stall,
        node_id="n1",
        run_id="run-r2b",
        watch_log_path=watch_log_path,
        watch_globs=watch_globs or [],
        resource_limits=None,
        sandbox_mode="llm",
        stdout_percentage_delta=None,
        stream_broker=None,
        drained_chunks=[],
        wall_clock=nr._WatchdogWallClock(None, 0.0),
        drain_window_bytes=drain_window_bytes,
    )


class _ScriptedLog:
    """A log that grows in stages so the drain window is actually exercised."""

    def __init__(self, stages: list[str]) -> None:
        self.stages = stages
        self.index = 0

    @property
    def text(self) -> str:
        return self.stages[self.index]

    def advance(self) -> None:
        self.index = min(self.index + 1, len(self.stages) - 1)


def _legacy_sandbox_for_log(log: _ScriptedLog) -> MagicMock:
    sandbox = MagicMock()
    sandbox.sandbox_id = "sbx-drain-legacy"
    sandbox.files.get_info = AsyncMock(side_effect=lambda path, **kw: MagicMock(size=len(log.text)))
    sandbox.files.read = AsyncMock(side_effect=lambda path, **kw: log.text)
    return sandbox


def _provider_for_log(log: _ScriptedLog) -> FakeFileIOProvider:
    """A fake provider whose stat/read are driven by the same scripted log."""
    provider = FakeFileIOProvider()

    async def _get_info(provider_ref: str, path: str) -> WorkspaceFileInfo:
        provider.refs.append(provider_ref)
        provider.events.append(f"get_info:{path}")
        return WorkspaceFileInfo(path=path, size=len(log.text), is_dir=False)

    async def _read_file(provider_ref: str, path: str) -> bytes:
        provider.refs.append(provider_ref)
        provider.events.append(f"read:{path}")
        return log.text.encode()

    provider.get_info = _get_info  # type: ignore[method-assign]
    provider.read_file = _read_file  # type: ignore[method-assign]
    return provider


def _patch_provider_builder(monkeypatch: pytest.MonkeyPatch, provider: Any) -> AsyncMock:
    builder = AsyncMock(return_value=provider)
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_file_io_provider", builder)
    # ``_file_io_provider_for`` resolves a credential BEFORE building, and
    # these tests deliberately bypass the ``fake_file_io`` fixture.
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    return builder


# ---------------------------------------------------------------------------
# 1. Bytes-in / bytes-out round-trip
# ---------------------------------------------------------------------------


async def test_write_then_read_round_trip_is_bytes_in_bytes_out(fake_file_io, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_flag(monkeypatch)
    text = "héllo — ünïcode prompt ✅"

    await _write_file_via_provider("sbx-rt", _PROMPT_PATH, text)

    # Bytes-in: the ABC primitive received UTF-8 bytes, not str.
    assert fake_file_io.files[_PROMPT_PATH] == text.encode("utf-8")
    assert all(isinstance(v, bytes) for v in fake_file_io.files.values())
    # The provider is addressed with the dispatch's sandbox id.
    assert fake_file_io.refs == ["sbx-rt"]

    # Bytes-out: the read helper hands back exactly the text that went in.
    assert await _read_file_via_provider("sbx-rt", _PROMPT_PATH) == text


async def test_read_round_trip_preserves_length_on_multibyte_content(
    fake_file_io, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decode must not change length — drain windowing slices by char."""
    _enable_flag(monkeypatch)
    text = "é" * 100 + "\n" + "→" * 50
    await _write_file_via_provider("sbx-len", "/tmp/multibyte.txt", text)

    back = await _read_file_via_provider("sbx-len", "/tmp/multibyte.txt")
    assert back == text
    assert len(back) == len(text)


# ---------------------------------------------------------------------------
# 2. Write-before-command ordering
# ---------------------------------------------------------------------------


async def test_flag_on_writes_land_before_the_agent_command(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    _enable_flag(monkeypatch)
    sandbox = _sandbox_mock("sbx-order")
    order = fake_file_io.events

    original_run = sandbox.commands.run

    async def _run_and_record(*args: Any, **kwargs: Any) -> Any:
        order.append("command")
        return await original_run(*args, **kwargs)

    sandbox.commands.run = AsyncMock(side_effect=_run_and_record)

    fn = make_sandbox_agent_fn(_base_node_def())
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    write_indexes = [i for i, e in enumerate(order) if e.startswith("write:")]
    assert write_indexes, "the flag-ON dispatch performed no provider writes"
    assert order.index("command") > write_indexes[-1]
    assert not sandbox.files.write.called


# ---------------------------------------------------------------------------
# 3. Drain-window equivalence on a scripted log
# ---------------------------------------------------------------------------


async def test_drain_window_equivalence_flag_on_matches_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tick-for-tick parity: same drained chunks, offset and retained length.

    The flag is toggled per tick so each watchdog really exercises ITS OWN
    arm — otherwise both would run the provider path and the comparison would
    be vacuous. The third stage overflows the 64-char window, so the
    comparison covers truncation, not just the first tick.
    """
    stages = [
        "stage-one-" * 5,  # 50 chars
        ("stage-one-" * 5) + ("stage-two-" * 5),  # 100 chars > window
        ("stage-one-" * 5) + ("stage-two-" * 5) + ("stage-three-" * 10),  # 220 chars
    ]
    log = _ScriptedLog(stages)

    legacy_wd = _watchdog(sandbox=_legacy_sandbox_for_log(log), drain_window_bytes=64)
    _patch_provider_builder(monkeypatch, _provider_for_log(log))
    provider_wd = _watchdog(sandbox=_sandbox_mock("sbx-drain-on"), drain_window_bytes=64)

    for _ in range(len(stages)):
        _disable_flag(monkeypatch)
        await legacy_wd.drain_sandbox_log()
        _enable_flag(monkeypatch)
        await provider_wd.drain_sandbox_log()
        log.advance()

    assert provider_wd._drained_chunks == legacy_wd._drained_chunks
    assert provider_wd._drain_offset == legacy_wd._drain_offset
    assert provider_wd._drained_len == legacy_wd._drained_len
    # The window really did truncate (otherwise this would pass vacuously).
    assert legacy_wd._drained_len <= 64
    assert provider_wd._drained_len <= 64
    assert provider_wd._drain_offset == len(stages[-1])


async def test_drain_window_probe_failure_is_quiet_on_the_flag_on_path(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """A provider-side stat failure degrades exactly like the legacy probe."""
    _enable_flag(monkeypatch)
    sandbox = _sandbox_mock("sbx-drain-fail")
    wd = _watchdog(sandbox=sandbox)

    async def _boom(provider_ref: str, path: str) -> WorkspaceFileInfo:
        raise RuntimeError("provider stat down")

    fake_file_io.get_info = _boom  # type: ignore[method-assign]

    await wd.drain_sandbox_log()

    assert wd._drain_offset == 0
    assert not wd._drained_chunks
    assert not sandbox.files.get_info.called


# ---------------------------------------------------------------------------
# 4. Routing invariants
# ---------------------------------------------------------------------------


async def test_no_sandbox_files_call_is_reachable_when_the_flag_is_on(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The legacy handle is unreachable flag-ON — even where a failure is swallowed.

    ``_ForbiddenFiles`` records every attribute touch, so an arm that catches
    its own AssertionError cannot make this test pass vacuously.
    """
    _enable_flag(monkeypatch)
    sandbox = _sandbox_mock("sbx-forbidden")
    forbidden = _ForbiddenFiles()
    sandbox.files = forbidden

    fn = make_sandbox_agent_fn(_base_node_def())
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert not forbidden.touched
    # ...and the provider path really did run.
    assert any(e.startswith("write:") for e in fake_file_io.events)
    assert any(e.startswith("read:") for e in fake_file_io.events)


async def test_flag_off_stays_on_the_legacy_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag OFF (the default): the provider seam is never constructed."""
    _disable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    builder = AsyncMock(side_effect=AssertionError("provider builder must not run when the flag is OFF"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_file_io_provider", builder)

    sandbox = _sandbox_mock("sbx-flagoff")
    fn = make_sandbox_agent_fn(_base_node_def())
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert sandbox.files.write.called
    builder.assert_not_awaited()


async def test_flag_off_context_files_stay_on_the_legacy_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag OFF: the context-files write loop stays on ``sandbox.files.write``.

    The flag-OFF dispatch test above runs with an empty ``context_files``
    map, so the loop body (and the flag-OFF arm inside it) never executes.
    This exercises the legacy arm explicitly and asserts the provider seam
    is still never constructed.
    """
    _disable_flag(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    builder = AsyncMock(side_effect=AssertionError("provider builder must not run when the flag is OFF"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_file_io_provider", builder)

    sandbox = _sandbox_mock("sbx-flagoff-ctx")
    node_def = _base_node_def(context_files={"/home/user/context/notes.txt": "ctx-body"})
    fn = make_sandbox_agent_fn(node_def)
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    sandbox.files.write.assert_any_await("/home/user/context/notes.txt", "ctx-body")
    builder.assert_not_awaited()


async def test_watchdog_fs_probe_flag_off_stays_on_the_legacy_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Site 4 (``files.list``): flag OFF lists through the legacy handle."""
    _disable_flag(monkeypatch)
    sandbox = _sandbox_mock("sbx-fs-flagoff")
    sandbox.files.list = AsyncMock(return_value=[])
    wd = _watchdog(sandbox=sandbox, watch_globs=["*.log"])
    wd._fs_min_stat_interval = 0.0

    await wd.probe_filesystem()

    assert sandbox.files.list.called
    assert sandbox.files.list.await_args.kwargs["path"] == "/"


async def test_watchdog_fs_probe_flag_on_routes_through_the_provider(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """Site 4 (``files.list``): flag-ON lists + stats through the ABC."""
    _enable_flag(monkeypatch)
    path = "/home/user/out/build.log"
    fake_file_io.files[path] = b"a" * 10

    sandbox = _sandbox_mock("sbx-fs")
    wd = _watchdog(sandbox=sandbox, watch_globs=["*.log"])
    wd._fs_min_stat_interval = 0.0

    await wd.probe_filesystem()
    # Probe 1 only seeds the tracked state (same as the legacy path).
    assert list(wd._fs_state) == [path]
    seeded = wd._stall._activity["filesystem"]

    fake_file_io.files[path] = b"a" * 999  # stat changed -> real fs activity
    wd._stall._now = lambda: seeded + 1000.0
    await wd.probe_filesystem()
    assert wd._stall._activity["filesystem"] == seeded + 1000.0
    assert not sandbox.files.list.called


async def test_watchdog_log_growth_probe_flag_on_routes_through_the_provider(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """Site 3 (``files.get_info`` on the watch log): flag-ON uses the ABC."""
    _enable_flag(monkeypatch)
    watch_path = "/home/user/out.json"
    fake_file_io.files[watch_path] = b"a" * 10

    sandbox = _sandbox_mock("sbx-growth")
    wd = _watchdog(sandbox=sandbox, watch_log_path=watch_path)

    await wd.probe_log_growth()
    assert wd._watch_log_prev_size == 10

    fake_file_io.files[watch_path] = b"a" * 40
    await wd.probe_log_growth()
    assert wd._watch_log_prev_size == 40
    assert not sandbox.files.get_info.called


async def test_watchdog_drain_probe_flag_on_routes_through_the_provider(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """Sites 1+2 (drain stat + read): flag-ON uses the ABC."""
    _enable_flag(monkeypatch)
    log_path = nr._SANDBOX_LOG_PATH
    fake_file_io.files[log_path] = b"hello from the agent\n"

    sandbox = _sandbox_mock("sbx-drain-route")
    wd = _watchdog(sandbox=sandbox)

    await wd.drain_sandbox_log()

    assert wd._drained_chunks == ["hello from the agent\n"]
    assert not sandbox.files.get_info.called
    assert not sandbox.files.read.called


# ---------------------------------------------------------------------------
# 5. Flag + provider-resolution units
# ---------------------------------------------------------------------------


def test_flag_defaults_off() -> None:
    assert Settings(_env_file=None).modulo_e2b_via_provider is False


def test_flag_read_failure_fails_open_to_the_legacy_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings outage resolves to the flag-OFF value — never a crash."""

    def _boom() -> Any:
        raise RuntimeError("settings down")

    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner.get_settings", _boom)
    assert _file_io_via_provider_enabled() is False


def test_flag_read_uses_the_runtime_settings_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "modulo_e2b_via_provider", True)
    assert _file_io_via_provider_enabled() is True
    monkeypatch.setattr(get_settings(), "modulo_e2b_via_provider", False)
    assert _file_io_via_provider_enabled() is False


async def test_build_file_io_provider_constructs_real_e2b_provider() -> None:
    """The real builder body runs the hub factory — no network, no injection."""
    from modulo.core.runtime_provider.e2b import E2BRuntimeProvider

    provider = await _build_file_io_provider("test-key")
    assert isinstance(provider, E2BRuntimeProvider)


async def test_build_file_io_provider_returns_none_when_hub_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modulo.core.runtime_provider.hub import RuntimeProviderHub

    async def _boom(self: RuntimeProviderHub, config: dict[str, Any]) -> None:
        raise RuntimeError("hub exploded")

    monkeypatch.setattr(RuntimeProviderHub, "initialise", _boom)
    assert await _build_file_io_provider("test-key") is None


async def test_provider_resolution_fails_closed_without_a_sandbox_ref(
    fake_file_io, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dispatch sandbox id -> typed failure, never a legacy-handle fallback."""
    _enable_flag(monkeypatch)
    with pytest.raises(RuntimeProviderError, match="dispatch sandbox id"):
        await _file_io_provider_for(None)
    with pytest.raises(RuntimeProviderError, match="dispatch sandbox id"):
        await _file_io_provider_for("")
    assert not fake_file_io.events


async def test_provider_resolution_fails_closed_without_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    builder = AsyncMock(side_effect=AssertionError("must not build a provider without a key"))
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner._build_file_io_provider", builder)

    with pytest.raises(RuntimeProviderError, match="E2B runtime provider"):
        await _file_io_provider_for("sbx-nokey")
    builder.assert_not_awaited()


async def test_provider_resolution_fails_closed_when_the_builder_yields_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_file_io_provider",
        AsyncMock(return_value=None),
    )
    with pytest.raises(RuntimeProviderError, match="E2B runtime provider"):
        await _file_io_provider_for("sbx-none")


async def test_write_helper_propagates_a_provider_failure(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """A flag-ON write failure raises into the caller's existing handling."""
    _enable_flag(monkeypatch)
    fake_file_io.write_error = RuntimeError("provider write exploded")

    with pytest.raises(RuntimeError, match="provider write exploded"):
        await _write_file_via_provider("sbx-write-fail", _PROMPT_PATH, "x")


# ---------------------------------------------------------------------------
# 6. Remaining flag-ON call-site arms (context files, script input, bridge)
# ---------------------------------------------------------------------------


def _script_sandbox_mock(sandbox_id: str) -> MagicMock:
    """Sandbox mock for a script-mode dispatch whose command succeeds."""
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "script stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.sandbox_id = sandbox_id
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(return_value="")
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.files.list = AsyncMock(return_value=[])
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    sandbox.get_metrics = AsyncMock(return_value=MagicMock(cpu_used_pct=1.0, mem_used=1, disk_used=1))
    return sandbox


def _script_node_def(**overrides: Any) -> dict[str, Any]:
    node_def: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "mode": "script",
        "script_command": "python3 /home/user/main.py",
        "agent_prompt": "ignored in script mode",
        "timeout_seconds": 30,
    }
    node_def.update(overrides)
    return node_def


async def test_get_info_via_provider_requires_a_non_empty_path(fake_file_io, monkeypatch: pytest.MonkeyPatch) -> None:
    """Site 3: a blank/absent watch path fails closed, never reaching the ABC."""
    _enable_flag(monkeypatch)
    with pytest.raises(RuntimeProviderError, match="non-empty path"):
        await _get_info_via_provider("sbx-info", None)
    with pytest.raises(RuntimeProviderError, match="non-empty path"):
        await _get_info_via_provider("sbx-info", "")
    assert not fake_file_io.events


async def test_list_fs_entries_filters_untracked_rows(fake_file_io, monkeypatch: pytest.MonkeyPatch) -> None:
    """Site 4: non-str rows, the redirected log, the watch log and glob misses
    are all excluded BEFORE a stat; only tracker-kept rows are statted."""
    _enable_flag(monkeypatch)
    keep = "/home/user/out/build.log"
    watch_log = "/home/user/watch.json"
    fake_file_io.files[keep] = b"1234567890"

    async def _list(provider_ref: str, path: str) -> list[Any]:
        fake_file_io.refs.append(provider_ref)
        fake_file_io.events.append(f"list:{path}")
        return [
            12345,  # not a str -> discarded
            nr._SANDBOX_LOG_PATH,  # redirected agent log -> excluded
            watch_log,  # configured watch log -> excluded
            "/home/user/notes.txt",  # does not match ``*.log`` -> excluded
            keep,  # tracked -> statted
        ]

    fake_file_io.list_files = _list  # type: ignore[method-assign]

    entries = await _list_fs_entries_via_provider("sbx-filter", "/", watch_log_path=watch_log, watch_globs=["*.log"])

    assert [entry.path for entry in entries] == [keep]
    # One listing + exactly one stat (for the kept row).
    assert fake_file_io.refs == ["sbx-filter", "sbx-filter"]
    assert "get_info:/home/user/notes.txt" not in fake_file_io.events


async def test_flag_on_context_files_land_through_the_provider(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """The context-files arm (written before the command) routes to the ABC."""
    _enable_flag(monkeypatch)
    sandbox = _sandbox_mock("sbx-ctx")
    node_def = _base_node_def(context_files={"/home/user/context/notes.txt": "ctx-body"})

    fn = make_sandbox_agent_fn(node_def)
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert fake_file_io.files["/home/user/context/notes.txt"] == b"ctx-body"
    assert "write:/home/user/context/notes.txt" in fake_file_io.events
    assert not sandbox.files.write.called


async def test_flag_on_script_mode_input_json_lands_through_the_provider(
    monkeypatch: pytest.MonkeyPatch, fake_file_io
) -> None:
    """The script-mode input.json arm routes to the ABC, not ``sandbox.files``."""
    _enable_flag(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = b'{"result": "ok"}'
    sandbox = _script_sandbox_mock("sbx-script")

    fn = make_sandbox_agent_fn(_script_node_def())
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    assert fake_file_io.files["/home/user/input.json"] == json.dumps({"task": "x"}).encode("utf-8")
    assert not sandbox.files.write.called


async def test_flag_on_bridge_writes_land_through_the_provider(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """All three loop-intercept bridge files route to the ABC when the flag is ON."""
    _enable_flag(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = b'{"summary": "done"}'
    sandbox = _script_sandbox_mock("sbx-bridge")

    server = MagicMock()
    server.start = AsyncMock(return_value=47591)
    server.close = AsyncMock()

    node_def = _base_node_def(loop_intercept={"enabled": True, "latency_budget_ms": 100})
    fn = make_sandbox_agent_fn(node_def)
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.guardrails.loop_intercept.load_loop_intercept_guardrails",
            new=AsyncMock(return_value=[MagicMock()]),
        ),
        patch("modulo.core.guardrails.loop_intercept.LoopInterceptCallbackServer", return_value=server),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    for bridge_path in (
        "/home/user/modulo_bridge.py",
        "/home/user/modulo_bridge_config.json",
        "/home/user/.modulo_bridge_cmd.sh",
    ):
        assert bridge_path in fake_file_io.files
    assert not sandbox.files.write.called
    server.close.assert_awaited_once()
