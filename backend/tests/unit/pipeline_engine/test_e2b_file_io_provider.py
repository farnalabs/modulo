"""FAR-1050 R2b/R6: file-I/O call-site routing through the ABC (site T4).

Proves the three things the rewire plan asks of this site, all without a live
E2B sandbox:

1. **Bytes-in/bytes-out round-trip.** ``_write_file_via_provider`` UTF-8-encodes
   the text; ``_read_file_via_provider`` decodes the primitive's bytes back to
   the same text — and the provider is addressed with the dispatch's sandbox id.
2. **Write-before-command ordering.** Every write lands before
   ``sandbox.commands.run`` starts the agent.
3. **Drain-window progression.** A growing log drives the drain across stages:
   the offset tracks the log end and the retained window truncates at
   ``drain_window_bytes``.

Plus the routing invariant: **no** ``sandbox.files`` call is reachable in
``node_runner.py`` (asserted through a handle that records every attribute
touch, so a swallowed exception cannot hide a legacy-path call).

R6 deleted the legacy ``sandbox.files`` arm and the ``MODULO_E2B_VIA_PROVIDER``
flag, so this file's original flag-OFF matrix — including the tick-for-tick
parity test between the two arms, which had nothing left to compare against —
retired with them.
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
    _get_info_via_provider,
    _list_fs_entries_via_provider,
    _read_file_via_provider,
    _write_file_via_provider,
    make_sandbox_agent_fn,
)
from modulo.core.runtime_provider import RuntimeProviderError, WorkspaceFileInfo
from tests.unit.pipeline_engine.conftest import install_fake_dispatch

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
_PROMPT_PATH = "/home/user/prompt.md"


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
    the assertion surface: it must stay empty on the provider path.
    """

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.touched.append(name)
        raise AssertionError(
            f"sandbox.files.{name} must not be touched: the legacy direct path is retired (FAR-1050 R6)"
        )


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


# ---------------------------------------------------------------------------
# 1. Bytes-in / bytes-out round-trip
# ---------------------------------------------------------------------------


async def test_write_then_read_round_trip_is_bytes_in_bytes_out(fake_file_io) -> None:
    text = "héllo — ünïcode prompt ✅"

    await _write_file_via_provider("sbx-rt", _PROMPT_PATH, text)

    # Bytes-in: the ABC primitive received UTF-8 bytes, not str.
    assert fake_file_io.files[_PROMPT_PATH] == text.encode("utf-8")
    assert all(isinstance(v, bytes) for v in fake_file_io.files.values())
    # The provider is addressed with the dispatch's sandbox id.
    assert fake_file_io.refs == ["sbx-rt"]

    # Bytes-out: the read helper hands back exactly the text that went in.
    assert await _read_file_via_provider("sbx-rt", _PROMPT_PATH) == text


async def test_read_round_trip_preserves_length_on_multibyte_content(fake_file_io) -> None:
    """The decode must not change length — drain windowing slices by char."""
    text = "é" * 100 + "\n" + "→" * 50
    await _write_file_via_provider("sbx-len", "/tmp/multibyte.txt", text)

    back = await _read_file_via_provider("sbx-len", "/tmp/multibyte.txt")
    assert back == text
    assert len(back) == len(text)


# ---------------------------------------------------------------------------
# 2. Write-before-command ordering
# ---------------------------------------------------------------------------


async def test_provider_writes_land_before_the_agent_command(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    sandbox = _sandbox_mock("sbx-order")
    order = fake_file_io.events

    original_run = sandbox.commands.run

    async def _run_and_record(*args: Any, **kwargs: Any) -> Any:
        order.append("command")
        return await original_run(*args, **kwargs)

    sandbox.commands.run = AsyncMock(side_effect=_run_and_record)
    # FAR-1050 R4: the provider path no longer reaches ``AsyncSandbox.create`` — the
    # command starts through the ABC stream primitive, so the ordering probe
    # is wired to the dispatch seam's own "command" milestone.
    install_fake_dispatch(monkeypatch, ref="sbx-order", command_events=order)

    fn = make_sandbox_agent_fn(_base_node_def())
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    write_indexes = [i for i, e in enumerate(order) if e.startswith("write:")]
    assert write_indexes, "the dispatch performed no provider writes"
    assert order.index("command") > write_indexes[-1]
    assert not sandbox.files.write.called


# ---------------------------------------------------------------------------
# 3. Drain-window progression (provider path)
# ---------------------------------------------------------------------------


async def test_drain_window_progression_and_truncation(fake_file_io) -> None:
    """Offset tracks the growing log; the retained window truncates.

    The provider half of the original flag-parity drain test. R6 deleted the
    legacy arm, so there is nothing left to compare against, but every
    invariant that test pinned on the provider arm still stands: offset
    progression across stages, a retained window capped at
    ``drain_window_bytes``, and truncation actually exercised (the third
    stage overflows the 64-char window).
    """
    stages = [
        "stage-one-" * 5,  # 50 chars
        ("stage-one-" * 5) + ("stage-two-" * 5),  # 100 chars > window
        ("stage-one-" * 5) + ("stage-two-" * 5) + ("stage-three-" * 10),  # 220 chars
    ]
    sandbox = _sandbox_mock("sbx-drain-window")
    wd = _watchdog(sandbox=sandbox, drain_window_bytes=64)

    for stage in stages:
        fake_file_io.files[nr._SANDBOX_LOG_PATH] = stage.encode()
        await wd.drain_sandbox_log()

    assert wd._drain_offset == len(stages[-1])
    assert wd._drained_chunks
    # Truncation really happened: the retained buffer is capped, not the
    # whole 220-char log.
    assert wd._drained_len <= 64


async def test_drain_window_probe_failure_is_quiet_on_the_provider_path(fake_file_io) -> None:
    """A provider-side stat failure degrades exactly like the legacy probe."""
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


async def test_no_sandbox_files_call_is_reachable(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """The legacy handle is unreachable — even where a failure is swallowed.

    ``_ForbiddenFiles`` records every attribute touch, so an arm that catches
    its own AssertionError cannot make this test pass vacuously. FAR-1050 R4
    additionally routes the dispatch through the provider seam, so the only
    handle the body ever holds is the ABC-mediated one (whose ``files``
    surface raises by construction).
    """
    sandbox = _sandbox_mock("sbx-forbidden")
    forbidden = _ForbiddenFiles()
    sandbox.files = forbidden
    install_fake_dispatch(monkeypatch, ref="sbx-forbidden")

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


async def test_watchdog_fs_probe_routes_through_the_provider(fake_file_io) -> None:
    """Site 4 (``files.list``): lists + stats through the ABC."""
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


async def test_watchdog_log_growth_probe_routes_through_the_provider(fake_file_io) -> None:
    """Site 3 (``files.get_info`` on the watch log): uses the ABC."""
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


async def test_watchdog_drain_probe_routes_through_the_provider(fake_file_io) -> None:
    """Sites 1+2 (drain stat + read): uses the ABC."""
    log_path = nr._SANDBOX_LOG_PATH
    fake_file_io.files[log_path] = b"hello from the agent\n"

    sandbox = _sandbox_mock("sbx-drain-route")
    wd = _watchdog(sandbox=sandbox)

    await wd.drain_sandbox_log()

    assert wd._drained_chunks == ["hello from the agent\n"]
    assert not sandbox.files.get_info.called
    assert not sandbox.files.read.called


# ---------------------------------------------------------------------------
# 5. Provider-resolution units
# ---------------------------------------------------------------------------


async def test_build_file_io_provider_constructs_real_e2b_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real builder body runs the hub factory — no network, no injection.

    Restores the R1 hub seam the autouse bridge fixture replaced, so this
    exercises the REAL chain rather than the mock-sandbox bridge.
    """
    from modulo.core.runtime_provider.e2b import E2BRuntimeProvider
    from tests.unit._e2b_sandbox_bridge import original_seam

    monkeypatch.setattr(nr, "_build_log_tail_provider", original_seam("_build_log_tail_provider"))
    provider = await _build_file_io_provider("test-key")
    assert isinstance(provider, E2BRuntimeProvider)


async def test_build_file_io_provider_returns_none_when_hub_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modulo.core.runtime_provider.hub import RuntimeProviderHub
    from tests.unit._e2b_sandbox_bridge import original_seam

    async def _boom(self: RuntimeProviderHub, config: dict[str, Any]) -> None:
        raise RuntimeError("hub exploded")

    monkeypatch.setattr(RuntimeProviderHub, "initialise", _boom)
    monkeypatch.setattr(nr, "_build_log_tail_provider", original_seam("_build_log_tail_provider"))
    assert await _build_file_io_provider("test-key") is None


async def test_provider_resolution_fails_closed_without_a_sandbox_ref(fake_file_io) -> None:
    """No dispatch sandbox id -> typed failure, never a legacy-handle fallback."""
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


async def test_write_helper_propagates_a_provider_failure(fake_file_io) -> None:
    """A provider write failure raises into the caller's existing handling."""
    fake_file_io.write_error = RuntimeError("provider write exploded")

    with pytest.raises(RuntimeError, match="provider write exploded"):
        await _write_file_via_provider("sbx-write-fail", _PROMPT_PATH, "x")


# ---------------------------------------------------------------------------
# 6. Remaining provider call-site arms (context files, script input, bridge)
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


async def test_get_info_via_provider_requires_a_non_empty_path(fake_file_io) -> None:
    """Site 3: a blank/absent watch path fails closed, never reaching the ABC."""
    with pytest.raises(RuntimeProviderError, match="non-empty path"):
        await _get_info_via_provider("sbx-info", None)
    with pytest.raises(RuntimeProviderError, match="non-empty path"):
        await _get_info_via_provider("sbx-info", "")
    assert not fake_file_io.events


async def test_list_fs_entries_filters_untracked_rows(fake_file_io) -> None:
    """Site 4: non-str rows, the redirected log, the watch log and glob misses
    are all excluded BEFORE a stat; only tracker-kept rows are statted."""
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


async def test_context_files_land_through_the_provider(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """The context-files arm (written before the command) routes to the ABC."""
    sandbox = _sandbox_mock("sbx-ctx")
    node_def = _base_node_def(context_files={"/home/user/context/notes.txt": "ctx-body"})
    # FAR-1050 R4: the dispatch itself runs through the provider
    # seam, so the dispatch must be a fake (the command still fails, as the
    # empty fixture sandbox does, keeping the SandboxNodeFailedError arm).
    install_fake_dispatch(monkeypatch, ref="sbx-ctx")

    fn = make_sandbox_agent_fn(node_def)
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert fake_file_io.files["/home/user/context/notes.txt"] == b"ctx-body"
    assert "write:/home/user/context/notes.txt" in fake_file_io.events
    assert not sandbox.files.write.called


async def test_script_mode_input_json_lands_through_the_provider(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """The script-mode input.json arm routes to the ABC, not ``sandbox.files``."""
    fake_file_io.files["/home/user/output.json"] = b'{"result": "ok"}'
    sandbox = _script_sandbox_mock("sbx-script")
    # FAR-1050 R4: the dispatch itself runs through the provider
    # seam; the scripted command completes with exit code 0.
    install_fake_dispatch(monkeypatch, ref="sbx-script", exit_code=0)

    fn = make_sandbox_agent_fn(_script_node_def())
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    assert fake_file_io.files["/home/user/input.json"] == json.dumps({"task": "x"}).encode("utf-8")
    assert not sandbox.files.write.called


async def test_bridge_writes_land_through_the_provider(monkeypatch: pytest.MonkeyPatch, fake_file_io) -> None:
    """All three loop-intercept bridge files route to the ABC, never the SDK handle."""
    fake_file_io.files["/home/user/output.json"] = b'{"summary": "done"}'
    sandbox = _script_sandbox_mock("sbx-bridge")
    # FAR-1050 R4: the dispatch itself runs through the provider
    # seam; the scripted command completes with exit code 0.
    install_fake_dispatch(monkeypatch, ref="sbx-bridge", exit_code=0)

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
