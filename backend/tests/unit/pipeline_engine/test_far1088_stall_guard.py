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
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import modulo.core.pipeline_engine.node_runner as nr
from modulo.core.pipeline_engine.node_runner import (
    _configure_stall_detector,
    _SandboxWatchdog,
    _StallDetector,
    _WatchdogWallClock,
)


def _detector_with_clock(*, enable_heartbeat: bool, clock: list[float]) -> _StallDetector:
    """Build the REAL default/strict detector bound to a fake monotonic clock.

    ``_configure_stall_detector`` constructs its ``_StallDetector`` with
    ``time.monotonic`` captured at construction time, so patching the module
    attribute for the duration of the call wires the fake clock in — the
    enabled-channel set comes from production code, never from a re-declared
    test copy.
    """
    with patch("modulo.core.pipeline_engine.node_runner.time.monotonic", new=lambda: clock[0]):
        return _configure_stall_detector(
            enable_heartbeat=enable_heartbeat,
            watch_log_path=None,
            stdout_percentage_delta=None,
            watch_globs=[],
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
