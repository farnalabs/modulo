"""FAR-1088 W-A — make the sandbox stall detector able to fire (output-liveness).

The probe-success heartbeat touch used to refresh liveness on every successful
``get_info`` (~5s cadence), so ``last_activity()`` never went stale and the
idle watchdog could never fire — only the total node timeout applied, and a
Branch Fixer run hit the 35-minute nodeless backstop instead of the stall
detector. Each test here FAILS without the FAR-1088 change:

1. The idle bound is 600s (was 300) and, once real output has been observed,
   output silence alone drives the stall — the ``connection`` channel (probe
   success) is excluded and cannot mask it.
2. A successful probe no longer refreshes the real-output channels: the drain
   records probe liveness on ``connection`` only, so a silent-after-first-output
   node stalls even while the connection is healthy.
3. The ``enable_heartbeat=False`` / no-``agent.log`` path stays window-bound
   (it does not stall "unconditionally"): probe touches are a no-op on the
   disabled channel (unchanged FAR-306 behaviour) and the stall fires only
   once the 600s output-silence window has elapsed.
4. (F1) That tight rule is SCOPED to STREAMING nodes: a script-mode node
   (``sandbox_mode == "script"``) keeps connection liveness after its first
   output too, so a block-buffered, legitimately-quiet child is never
   hard-killed by the 600s window (the false-stall regression).
5. (F5) A streaming node that NEVER emits output stays alive on the
   connection fallback for its whole life — bounded only by the node's total
   ``timeout_seconds``, never by the silence window. That limitation is
   intended, so it is asserted rather than implied.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.pipeline_engine.node_runner as nr
from modulo.core.pipeline_engine.node_runner import (
    _configure_stall_detector,
    _SandboxWatchdog,
    _StallDetector,
    _wait_command_with_exec_process,
    _WatchdogWallClock,
)


def _detector_with_clock(*, enable_heartbeat: bool, clock: list[float], sandbox_mode: str = "llm") -> _StallDetector:
    """Build the REAL default/strict detector bound to a fake monotonic clock.

    ``_configure_stall_detector`` constructs its ``_StallDetector`` with
    ``time.monotonic`` captured at construction time, so patching the module
    attribute for the duration of the call wires the fake clock in — the
    enabled-channel set comes from production code, never from a re-declared
    test copy. ``sandbox_mode`` is the FAR-1088 F1 discriminator: ``"llm"``
    (streaming, the default) vs ``"script"`` (quiet, connection stays live).
    """
    with patch("modulo.core.pipeline_engine.node_runner.time.monotonic", new=lambda: clock[0]):
        return _configure_stall_detector(
            enable_heartbeat=enable_heartbeat,
            watch_log_path=None,
            stdout_percentage_delta=None,
            watch_globs=[],
            sandbox_mode=sandbox_mode,
        )


def test_idle_bound_is_600_and_output_silence_stalls_only_past_it() -> None:
    """The default idle bound is 600s (2.2x the measured 268.8s max
    inter-output gap), and a node that HAS produced output is judged by output
    silence alone: not stalled one second before the window, stalled after it,
    even while a probe keeps the ``connection`` channel fresh."""
    # Fails without FAR-1088: the bound was 300.
    assert nr._SANDBOX_IDLE_TIMEOUT == 600.0

    clock: list[float] = [1000.0]
    stall = _detector_with_clock(enable_heartbeat=True, clock=clock)
    # Real log growth — the first observed output flips the detector out of
    # the connection-fallback mode (attribute added by FAR-1088).
    stall.touch("output")
    assert stall._output_seen is True

    bound = nr._SANDBOX_IDLE_TIMEOUT
    clock[0] = 1000.0 + bound - 1.0  # 599s of output silence
    assert clock[0] - stall.last_activity() < bound  # NOT stalled yet

    clock[0] = 1000.0 + bound + 1.0  # 601s
    assert clock[0] - stall.last_activity() >= bound  # stalled

    # A successful probe mid-window must NOT resurrect an output-silent node:
    # `connection` is excluded from last_activity() once output has been seen.
    clock[0] = 1000.0 + 599.0
    stall.touch("connection")
    clock[0] = 1000.0 + bound + 1.0
    assert clock[0] - stall.last_activity() >= bound


async def test_probe_success_alone_no_longer_keeps_a_silent_node_alive() -> None:
    """The real drain path: a successful ``get_info`` probe no longer refreshes
    the real-output channels. After the log has grown once (output observed), a
    probe-only tick — connection healthy, log flat — leaves ``output`` and
    ``heartbeat`` untouched, so the node is judged stalled by output silence
    alone (deterministic fake clock, no wall-clock elapsed assertion)."""
    clock: list[float] = [1000.0]
    stall = _detector_with_clock(enable_heartbeat=True, clock=clock)
    sandbox = MagicMock()
    sandbox.sandbox_id = "sbx-far1088"
    wd = _SandboxWatchdog(
        sandbox=sandbox,
        stall=stall,
        node_id="n1",
        run_id="run-far1088",
        watch_log_path=None,
        watch_globs=[],
        resource_limits=None,
        sandbox_mode="llm",
        stdout_percentage_delta=None,
        stream_broker=None,
        drained_chunks=[],
        wall_clock=_WatchdogWallClock(None, 0.0),
    )

    log_text = "x" * 64
    with (
        patch.object(nr, "_get_info_via_provider", new=AsyncMock(return_value=SimpleNamespace(size=len(log_text)))),
        patch.object(nr, "_read_file_via_provider", new=AsyncMock(return_value=log_text)),
    ):
        await wd.drain_sandbox_log()  # tick 1: the log grows -> real output observed
    # Fails without FAR-1088: the mode flag does not exist.
    assert stall._output_seen is True
    assert stall.last_activity() == 1000.0  # the output touch landed on the fake clock

    # A probe-only tick: the connection is healthy, the log does NOT grow.
    clock[0] = 1000.0 + nr._SANDBOX_IDLE_TIMEOUT + 1.0  # 601s later
    with patch.object(nr, "_get_info_via_provider", new=AsyncMock(return_value=SimpleNamespace(size=64))):
        await wd.drain_sandbox_log()

    # Fails without FAR-1088: the probe refreshed `heartbeat` (refreshed on
    # every successful get_info), so last_activity() would have jumped to
    # "now" and the node would never be judged stalled.
    assert stall.last_activity() == 1000.0, "a probe must not refresh the real-output channels"
    assert clock[0] - stall.last_activity() >= nr._SANDBOX_IDLE_TIMEOUT


def test_strict_mode_no_agent_log_stalls_only_after_the_window() -> None:
    """``enable_heartbeat=False`` + a log that never grows: the node does NOT
    stall unconditionally — it stalls exactly when the output-silence window
    has elapsed, and probe success has no say either way.

    FAR-1088 did not change this path: ``touch()`` has always ignored the
    disabled ``heartbeat``/``connection`` channel (FAR-306), so removing the
    probe's heartbeat touch could not start a strict-mode stall — strict mode
    has always been output-only, now at the 600s default instead of 300s.
    """
    clock: list[float] = [100.0]
    stall = _detector_with_clock(enable_heartbeat=False, clock=clock)
    assert stall.enabled == {"output"}  # strict: no connection liveness
    # The window itself is the FAR-1088 change on this path (was 300): a
    # strict node with no agent.log stalls at 600s of silence, not before.
    assert nr._SANDBOX_IDLE_TIMEOUT == 600.0

    bound = nr._SANDBOX_IDLE_TIMEOUT

    # A successful probe attempt mid-window is a no-op (channel not enabled),
    # so it can neither stall nor rescue the node.
    stall.touch("connection")
    clock[0] = 100.0 + bound - 1.0  # 599s of output silence
    assert clock[0] - stall.last_activity() < bound  # bounded window, not unconditional

    stall.touch("connection")  # probes still succeeding as the window closes
    clock[0] = 100.0 + bound + 1.0  # 601s
    assert clock[0] - stall.last_activity() >= bound  # stalls on the window, as strict mode always has


def test_default_detector_wires_the_connection_channel() -> None:
    """Default wiring (enable_heartbeat=True) enables the dedicated
    ``connection`` channel that probe-success liveness moved onto; strict
    wiring does not."""
    clock: list[float] = [0.0]
    default = _detector_with_clock(enable_heartbeat=True, clock=clock)
    assert default.enabled == {"output", "heartbeat", "connection"}

    strict = _detector_with_clock(enable_heartbeat=False, clock=clock)
    assert strict.enabled == {"output"}

    # Backstop: a hand-built detector with every channel disabled never stalls.
    never = _StallDetector(now=lambda: 1.0)
    assert never.last_activity() == 1.0


# ---------------------------------------------------------------------------
# F1 — the tight output-silence kill is scoped to nodes whose output is
# EXPECTED TO STREAM; a legitimately quiet script-mode workload keeps
# connection-liveness (pre-FAR-1088 behaviour), bounded by its node deadline.
# ---------------------------------------------------------------------------


def test_script_mode_keeps_connection_liveness_after_first_output() -> None:
    """F1: ``sandbox_mode == "script"`` does NOT take the streaming rule.

    A script-mode command redirects stdout to ``agent.log``, so only log growth
    feeds output-liveness — a block-buffered child (``docker pull``,
    ``pnpm``/``uv install``, a buffered test run) can legitimately be silent
    for far longer than 600s. Without the F1 scoping it would be HARD-KILLED
    and retried by the idle watchdog (a false-stall regression): output was
    observed once, so ``connection`` was excluded and the flat log tripped the
    window. Here the same sequence never stalls on a script node — while the
    identical sequence on a streaming (``llm``) node does, proving the
    discriminator, not a weakened window, is what changed.
    """
    # Script node: first output observed, then 1500s (2.5 windows) of output
    # silence with a healthy probe every 300s -> alive throughout.
    clock: list[float] = [1000.0]
    script = _detector_with_clock(enable_heartbeat=True, clock=clock, sandbox_mode="script")
    script.touch("output")  # the log grew once
    assert script._output_seen is True  # the flag flips in BOTH modes...
    for _ in range(5):
        clock[0] += 300.0
        script.touch("connection")  # probes keep succeeding, log stays flat
        assert clock[0] - script.last_activity() < nr._SANDBOX_IDLE_TIMEOUT, (
            "a quiet script node must stay alive on connection liveness, "
            "not be hard-killed at the output-silence window"
        )

    # Control: the SAME sequence on a streaming node IS stalled past the window.
    llm_clock: list[float] = [1000.0]
    streaming = _detector_with_clock(enable_heartbeat=True, clock=llm_clock, sandbox_mode="llm")
    streaming.touch("output")
    llm_clock[0] = 1000.0 + nr._SANDBOX_IDLE_TIMEOUT + 1.0
    streaming.touch("connection")  # a probe succeeding mid-window must not save it
    assert llm_clock[0] - streaming.last_activity() >= nr._SANDBOX_IDLE_TIMEOUT


# ---------------------------------------------------------------------------
# F5 — stated and covered: a streaming node that NEVER emits output is held
# alive by the connection fallback and bounded ONLY by its node deadline.
# ---------------------------------------------------------------------------


def test_never_output_streaming_node_is_held_alive_by_connection_liveness() -> None:
    """F5(a): output NEVER seen (``_output_seen`` stays False) — the
    ``connection`` fallback keeps ``last_activity()`` tracking the probe for
    the node's whole life, so the silence window cannot fire no matter how much
    wall time passes. The limitation: this node is bounded by its total
    ``timeout_seconds`` only, NOT by ``stall_timeout_seconds``."""
    clock: list[float] = [0.0]
    stall = _detector_with_clock(enable_heartbeat=True, clock=clock, sandbox_mode="llm")
    assert stall.enabled == {"output", "heartbeat", "connection"}

    for _ in range(20):
        clock[0] += nr._SANDBOX_IDLE_TIMEOUT + 1.0  # 12020s of total silence
        stall.touch("connection")  # probe succeeds; the log never grows
        assert stall._output_seen is False  # still never produced output
        assert stall.last_activity() == clock[0]  # liveness tracks the probe...
        assert clock[0] - stall.last_activity() < nr._SANDBOX_IDLE_TIMEOUT  # ...so never stalled


async def test_never_output_node_dies_by_total_timeout_not_by_the_silence_window() -> None:
    """F5(b): through the REAL wait loop, a never-output streaming node whose
    probe refreshes liveness on every tick outlives a silence window (0.5s)
    that is never fed by output, and dies only when the node's total
    ``timeout_seconds`` fires — proving the node deadline, not the silence
    window, is its bound.

    Timing margins (the FAR-306 lesson, >=50x tick-to-window): the window is
    0.5s against a 0.01s tick, so a single delayed iteration cannot be
    mistaken for a stall under a loaded event loop; the total timeout (2s)
    outlasts the window 4x so the deadline path is what actually fires.
    """
    from modulo.core.runtime_provider import ExecProcess

    process = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]
    killed: list[str] = []

    async def _kill() -> None:
        killed.append("killed")

    process._kill = _kill  # type: ignore[attr-defined]

    async def _chunks():
        # Never completes: only a bound below can end the wait.
        guard_deadline = time.monotonic() + 3600.0
        while time.monotonic() < guard_deadline:
            await asyncio.sleep(3600)
            yield ""  # pragma: no cover

    process.chunks = _chunks()

    clock: list[float] = [0.0]
    stall = _detector_with_clock(enable_heartbeat=True, clock=clock, sandbox_mode="llm")

    async def _probe_tick() -> None:
        # Each tick: get_info succeeded (the drain probe), the log did not
        # grow. The detector's fake clock follows wall time so the touch
        # timestamps stay comparable to the loop's real monotonic reads.
        clock[0] = time.monotonic()
        stall.touch("connection")

    with pytest.raises(TimeoutError, match="total timeout"):
        await _wait_command_with_exec_process(
            process,
            total_timeout=2.0,
            idle_timeout=0.5,  # a silence window the node outlives only via connection liveness
            last_activity=stall.last_activity,
            on_tick=_probe_tick,
            tick_interval=0.01,
        )

    assert killed == [], "the node must die at the deadline, never at the silence window"
    assert stall._output_seen is False
