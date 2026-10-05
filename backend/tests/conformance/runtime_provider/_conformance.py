"""Runtime-provider conformance checks: shared registry + suite runner (FAR-1053).

Purpose (why this gate exists)
------------------------------
The Kubernetes tier must not die the way the previous Helm configs did -
removed because nothing ever exercised them. This suite is the exercise: the ADR 040
``RuntimeProvider`` ABC contract run against a REAL Kubernetes cluster, plus
a negative suite proving the gate has teeth (a deliberately broken adapter
must FAIL it; a kill-before-collect must be detected, never reported as a
synthetic success).

Shape (ADR 040: "a new tier conforms by ... the shared conformance suite")
--------------------------------------------------------------------------
Follows the connectors-conformance pattern
(``backend/tests/connectors/_conformance.py``): a module-level registry of
named checks plus a runner that executes every registered check against one
provider and collects failures. The connectors suite's fixture-name registry
is deliberately NOT copied: one substrate (Kubernetes) is registered today
and ``test_k8s_conformance.py`` binds its fixture directly, so a second,
unconsumed registry would be unwired machinery.

Vocabulary: this is the ``conformance`` suite (pytest marker
``runtime_provider_conformance``, deselected by default via ``addopts`` in
``backend/pyproject.toml``). It is deliberately NOT a third "contract"
vocabulary: ``tests/integration/test_runtime_conformance.py`` owns run-fencing
invariants against Postgres, and
``tests/architecture/test_runtime_provider_contract.py`` is a static ABC
surface scanner. Neither of those needs a cluster; this suite does.

Running
-------
Selected explicitly, never implicitly - a normal unit/integration run must
DESELECT these tests (never skip-and-pass them green):

    uv run pytest tests/conformance/ -m runtime_provider_conformance ...

``.github/workflows/k8s-conformance.yml`` runs them against ``kind`` per-PR
and against one rotating managed cloud (EKS/AKS/GKE) weekly.

Naming: every check is ``assert_*`` so a thin test wrapper reads as a
verifying test body (the test-style scanner recognises assert-named calls)
and so a failure names the contract clause that broke.

Capability notes: the ``stream_*`` checks exercise ``exec_command_stream``,
which is an optional ABC method (ADR 040 streaming parity). A future tier
that does not override it must be registered with those checks excluded -
today the only registered tier (Kubernetes) overrides it, so no capability
flag machinery exists yet (deliberately unwired until a second tier lands).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from modulo.core.runtime_provider import RuntimeProvider, WorkspaceSpec

_log = logging.getLogger(__name__)

# Provision bound for every conformance workspace: create_workspace waits for
# the pod to reach Running within spec.timeout_seconds, and a cold cluster may
# still be pulling the workspace image - 240s bounds that wait without letting
# a stuck cluster hang the suite.
_PROVISION_BOUND_S = 240
# Collect-then-return exec bound for every conformance command.
_EXEC_TIMEOUT_S = 120
# Streaming bounds: live-marker wait, then post-kill drain.
_STREAM_LIVE_WAIT_S = 60
_STREAM_DONE_WAIT_S = 30
# Status-poll bound + interval for destroy -> terminated.
_STATUS_POLL_BOUND_S = 90.0
_STATUS_POLL_INTERVAL_S = 1.0
# read_log_tail bound under test.
_LOG_TAIL_MAX_BYTES = 2048
# The workspace agent-log file the dispatcher redirects the agent command's
# stdout/stderr to (``node_runner._SANDBOX_LOG_PATH``). The log-tail check
# writes its marker here - mirroring that redirect - so the read must return
# real content; an always-empty tail (e.g. reading the wrong surface) fails.
_WORKSPACE_LOG_PATH = "/home/user/agent.log"
_LOG_TAIL_MARKER = "conformance-log-tail"

_CONFORMANCE_CHECKS: dict[str, Callable[[RuntimeProvider], Awaitable[None]]] = {}
"""Registry: check name -> async check callable. Populated at module import."""


def register_conformance_check(name: str, check: Callable[[RuntimeProvider], Awaitable[None]]) -> None:
    """Register *check* under *name*.

    Must be called at module level in a conformance module (mirrors
    ``register_conformance_connector`` in the connectors suite), so the
    registry is populated before any test or the suite runner reads it.
    """
    if name in _CONFORMANCE_CHECKS:
        raise ValueError(f"conformance check {name!r} is already registered")
    _CONFORMANCE_CHECKS[name] = check


def get_registered_checks() -> list[str]:
    """Names of every registered conformance check, sorted."""
    return sorted(_CONFORMANCE_CHECKS)


def conformance_workspace_spec() -> WorkspaceSpec:
    """A throwaway spec for one conformance workspace.

    ``timeout_seconds`` bounds the provider's provision wait (pod ->
    Running); on a cold cluster the first pull of the default workspace image
    sits inside this bound. Organisation/profile ids are fresh per call: the
    Kubernetes tier reads them only as metadata, and a fresh pair keeps each
    check's workspace independent.
    """
    return WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        timeout_seconds=_PROVISION_BOUND_S,
    )


async def await_workspace_status(
    provider: RuntimeProvider,
    provider_ref: str,
    expected: str,
    *,
    bound_s: float = _STATUS_POLL_BOUND_S,
) -> str:
    """Poll ``get_workspace_status`` until it reports *expected* or *bound_s* elapses.

    Returns the last observed status either way; callers assert it, so a
    timeout surfaces as a readable contract failure instead of a hang.
    """
    deadline = time.monotonic() + bound_s
    status = ""
    while True:
        status = await provider.get_workspace_status(provider_ref)
        if status == expected:
            return status
        if time.monotonic() >= deadline:
            return status
        await asyncio.sleep(_STATUS_POLL_INTERVAL_S)


async def destroy_workspace_quietly(provider: RuntimeProvider, provider_ref: str) -> None:
    """Best-effort teardown for a check's workspace.

    A check's REAL verdict must not be masked by its own cleanup, so a
    teardown failure is logged and swallowed (the check has already
    passed/failed by the time this runs); a leaked pod is still reclaimed by
    the session fixture's namespace deletion.
    """
    try:
        await provider.destroy_workspace_by_ref(provider_ref)
    except Exception:
        _log.info("conformance teardown destroy failed for %s", provider_ref, exc_info=True)


async def _create_workspace(provider: RuntimeProvider) -> str:
    return await provider.create_workspace(conformance_workspace_spec())


# ── Registered checks ──────────────────────────────────────────────────────


async def assert_workspace_created_and_running(provider: RuntimeProvider) -> None:
    """``create_workspace`` returns a ref whose ``get_workspace_status`` reads ``running``.

    The pod-Ready condition is a Kubernetes-substrate assertion and lives in
    ``test_k8s_conformance.py``; at the ABC level the observable contract is
    the status string the provider waits for before returning the ref.
    """
    ref = await _create_workspace(provider)
    try:
        status = await provider.get_workspace_status(ref)
        assert status == "running", (
            f"create_workspace returned {ref!r} but get_workspace_status reports {status!r} "
            "(expected 'running' - the provider must wait for the pod before returning)"
        )
    finally:
        await destroy_workspace_quietly(provider, ref)


async def assert_exec_exit_zero(provider: RuntimeProvider) -> None:
    """An exit-0 command reports exit_code 0 with its stdout delivered."""
    ref = await _create_workspace(provider)
    try:
        result = await provider.exec_command(ref, ["sh", "-c", "echo conformance-zero-ok"], cmd_timeout=_EXEC_TIMEOUT_S)
        assert result.exit_code == 0, (
            f"exit-0 command returned exit_code={result.exit_code} (stderr={result.stderr!r}) - expected 0"
        )
        assert "conformance-zero-ok" in result.stdout, f"stdout lost the echoed marker: {result.stdout!r}"
    finally:
        await destroy_workspace_quietly(provider, ref)


async def assert_exec_nonzero_exit_code_mapped(provider: RuntimeProvider) -> None:
    """A real non-zero exit maps to ITS OWN code - never fabricated as 0.

    This is the check a success-fabricating adapter fails (the negative
    suite's teeth): ``exit_code=0`` for a command that exited 7 is a
    synthetic successful completion.
    """
    ref = await _create_workspace(provider)
    try:
        result = await provider.exec_command(ref, ["sh", "-c", "exit 7"], cmd_timeout=_EXEC_TIMEOUT_S)
        assert result.exit_code == 7, (
            f"a command that exits 7 must report exit_code=7 (fabricated success or an "
            f"opaque -1 both break the contract); got {result.exit_code} "
            f"(stderr={result.stderr!r})"
        )
    finally:
        await destroy_workspace_quietly(provider, ref)


async def assert_stream_ordered_with_clean_exit(provider: RuntimeProvider) -> None:
    """Streaming delivers stdout and stderr chunks IN ORDER and a clean exit.

    Ordering is asserted per stream (within stdout, within stderr): the
    Kubernetes exec multiplexer pumps the two pipes in separate goroutines,
    so cross-stream interleaving is not part of any provider's contract.
    """
    ref = await _create_workspace(provider)
    try:
        process = await provider.exec_command_stream(
            ref,
            ["sh", "-c", "echo cf-out-1; echo cf-err-1 1>&2; echo cf-out-2; echo cf-err-2 1>&2"],
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        async for chunk in process.chunks:
            if chunk.stream == "stdout":
                stdout_parts.append(chunk.data)
            else:
                stderr_parts.append(chunk.data)
        await asyncio.wait_for(process.done.wait(), timeout=_STREAM_DONE_WAIT_S)

        stdout_text = "".join(stdout_parts)
        stderr_text = "".join(stderr_parts)
        assert "cf-out-1" in stdout_text, f"stdout marker 1 missing: {stdout_text!r}"
        assert "cf-out-2" in stdout_text, f"stdout marker 2 missing: {stdout_text!r}"
        assert stdout_text.index("cf-out-1") < stdout_text.index("cf-out-2"), (
            f"stdout chunks arrived out of order: {stdout_text!r}"
        )
        assert "cf-err-1" in stderr_text, f"stderr marker 1 missing: {stderr_text!r}"
        assert "cf-err-2" in stderr_text, f"stderr marker 2 missing: {stderr_text!r}"
        assert stderr_text.index("cf-err-1") < stderr_text.index("cf-err-2"), (
            f"stderr chunks arrived out of order: {stderr_text!r}"
        )
        assert process.error is None, f"a healthy stream must end without an error: {process.error}"
        assert process.exit_code == 0, f"a healthy stream must end with exit_code=0, got {process.exit_code!r}"
    finally:
        await destroy_workspace_quietly(provider, ref)


async def assert_stream_kill_before_collect_detected(provider: RuntimeProvider) -> None:
    """Kill-before-collect is DETECTED: a stream error, never a synthetic success.

    The exec is killed mid-run (before any exit-status message could exist).
    The contract (ExecProcess, D4): ``exit_code`` stays ``None`` until the END
    of a HEALTHY stream, and the kill surfaces as ``error``. A consumer
    finishing on this must treat the process as failed - a fabricated
    ``exit_code == 0`` would be a silent wrong-success completion.
    """
    ref = await _create_workspace(provider)
    try:
        process = await provider.exec_command_stream(ref, ["sh", "-c", "echo cf-kill-live; sleep 600"])
        marker_seen: list[str] = []

        async def _consume_until_live() -> None:
            async for chunk in process.chunks:
                marker_seen.append(chunk.data)
                if "cf-kill-live" in "".join(marker_seen):
                    return

        # Bounded: a stream that never goes live is a hang, not a pass.
        await asyncio.wait_for(_consume_until_live(), timeout=_STREAM_LIVE_WAIT_S)
        # The stream must actually have gone live BEFORE the kill. Without
        # this, a stream that dies instantly (broken exec path) ends the
        # consume loop early and the kill assertions below hold vacuously -
        # the suite would then "pass" kill detection while never observing a
        # live stream at all (observed exactly that during local validation).
        seen_before_kill = "".join(marker_seen)
        assert "cf-kill-live" in seen_before_kill, (
            "the stream never delivered the live marker before the kill, so "
            "kill-before-collect cannot be tested on it (broken exec path?): "
            f"received {seen_before_kill!r}"
        )

        await process.kill()
        # Drain so the chunk generator finalises and ``done`` fires.
        async for _chunk in process.chunks:
            pass
        await asyncio.wait_for(process.done.wait(), timeout=_STREAM_DONE_WAIT_S)

        assert process.exit_code is None, (
            f"kill-before-collect fabricated exit_code={process.exit_code!r} - a stream killed "
            "before its exit status arrived must never report a completed (successful) process"
        )
        assert process.error is not None, (
            "kill-before-collect must surface a stream error (detected), not a silent healthy-looking end"
        )
    finally:
        await destroy_workspace_quietly(provider, ref)


async def assert_log_tail_after_process_end(provider: RuntimeProvider) -> None:
    """``read_log_tail`` works after an exec'd process ends: bounded raw bytes WITH content.

    The probe mirrors the dispatcher's own log redirect
    (``node_runner._wrap_sandbox_command_with_log_redirect``): the marker is
    written to the workspace agent-log file, so the read only passes if the
    returned tail actually CONTAINS it. Without the content assertion an
    always-empty tail - e.g. a provider reading the wrong surface - passed the
    check vacuously.
    """
    ref = await _create_workspace(provider)
    try:
        result = await provider.exec_command(
            ref,
            ["sh", "-c", f"( echo {_LOG_TAIL_MARKER} ) > {_WORKSPACE_LOG_PATH} 2>&1"],
            cmd_timeout=_EXEC_TIMEOUT_S,
        )
        assert result.exit_code == 0, (
            f"probing command failed (exit_code={result.exit_code}) - the log-tail read below would not prove anything"
        )
        tail = await provider.read_log_tail(ref, max_bytes=_LOG_TAIL_MAX_BYTES)
        assert isinstance(tail, bytes), f"read_log_tail must return raw bytes, got {type(tail).__name__}"
        assert len(tail) <= _LOG_TAIL_MAX_BYTES, (
            f"read_log_tail ignored max_bytes={_LOG_TAIL_MAX_BYTES}: returned {len(tail)} bytes"
        )
        assert _LOG_TAIL_MARKER.encode("utf-8") in tail, (
            "read_log_tail returned no workspace content after the process ended: "
            f"expected {_LOG_TAIL_MARKER!r} in the tail, got {tail!r}"
        )
    finally:
        await destroy_workspace_quietly(provider, ref)


async def assert_destroy_by_ref_idempotent(provider: RuntimeProvider) -> None:
    """``destroy_workspace_by_ref``: first destroy confirms, second is a clean no-op.

    Also covers the ABC's idempotency clause for a foreign/unknown ref (a
    ref this provider never created) - no-op success, never an error.
    """
    ref = await _create_workspace(provider)

    first = await provider.destroy_workspace_by_ref(ref)
    assert first is True, f"first destroy of {ref!r} must confirm removal, got {first!r}"

    status = await await_workspace_status(provider, ref, "terminated")
    assert status == "terminated", (
        f"status must read 'terminated' once the pod is gone, got {status!r} (polled {_STATUS_POLL_BOUND_S:.0f}s)"
    )

    second = await provider.destroy_workspace_by_ref(ref)
    assert second is True, (
        f"second destroy of the already-gone ref {ref!r} must be an idempotent no-op success, got {second!r}"
    )

    # A realistic-looking ref this run never created: the idempotent
    # no-op-success clause must cover it (the pod is already gone), never an
    # error.
    foreign_ref = f"modulo-ws-{uuid.uuid4().hex[:16]}"
    foreign = await provider.destroy_workspace_by_ref(foreign_ref)
    assert foreign is True, f"destroy of a foreign/unknown ref must be an idempotent no-op success, got {foreign!r}"


register_conformance_check("create_and_status", assert_workspace_created_and_running)
register_conformance_check("exec_exit_zero", assert_exec_exit_zero)
register_conformance_check("exec_nonzero_exit_mapping", assert_exec_nonzero_exit_code_mapped)
register_conformance_check("stream_ordered_clean_exit", assert_stream_ordered_with_clean_exit)
register_conformance_check("stream_kill_before_collect", assert_stream_kill_before_collect_detected)
register_conformance_check("log_tail_after_process_end", assert_log_tail_after_process_end)
register_conformance_check("destroy_by_ref_idempotent", assert_destroy_by_ref_idempotent)


async def run_conformance_checks(provider: RuntimeProvider) -> list[str]:
    """Run every registered check against *provider*; return failure descriptions.

    The negative-suite runner: it collects instead of raising so one call can
    show WHICH checks a deliberately broken adapter fails (and a conforming
    provider must produce an empty list). An unexpected exception inside a
    check becomes a failure entry rather than aborting the remaining checks.
    """
    failures: list[str] = []
    for name in sorted(_CONFORMANCE_CHECKS):
        check = _CONFORMANCE_CHECKS[name]
        try:
            await check(provider)
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    return failures
