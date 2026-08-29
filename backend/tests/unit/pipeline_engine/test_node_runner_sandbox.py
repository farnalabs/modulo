"""Unit tests for make_sandbox_agent_fn command resolution.

A sandbox_agent node MUST provide agent_command (or agent_commands);
there is no default command, and a missing command is a hard error.
"""

import asyncio
import logging
import os
import time
import urllib.request
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.event_broker import get_registry
from modulo.core.pipeline_engine.node_runner import (
    _E2B_SANDBOX_USD_PER_HOUR,
    _MAX_ARTIFACT_LOG,
    SandboxNodeFailedError,
    _compute_sandbox_cost,
    _delta_ratio,
    _fetch_sandbox_log_tail,
    _StallDetector,
    _wait_command_with_idle_watchdog,
    make_sandbox_agent_fn,
    resolve_env_var_refs,
)

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"


def _read_router(output_json: str, log_content: str = ""):
    """Route sandbox.files.read by path: output.json vs the redirected agent log.

    The FAR-97 pipe-buffer fix redirects the agent command's stdout/stderr to a
    sandbox log file, so sandbox.files.read is called for BOTH the log file
    (drain probe) and /home/user/output.json (final result). Routing by path
    keeps the two distinct.
    """

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return output_json
        return log_content

    return _read


def _make_sandbox_mock(*, log_content: str = "", output_json: str = '{"summary": "done"}'):
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read_router(output_json, log_content))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=len(log_content)))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


def test_missing_agent_command_raises_value_error():
    """A sandbox_agent node without agent_command/agent_commands is a hard error."""
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
    }
    with pytest.raises(ValueError, match="missing required 'agent_command'"):
        make_sandbox_agent_fn(node_def)


def test_missing_agent_prompt_raises_value_error():
    """An empty/missing agent_prompt is a hard error — it would dispatch the agent with no instructions."""
    node_def = {
        "id": "n1",
        "agent_command": _AGENT_COMMAND,
    }
    with pytest.raises(ValueError, match="missing required 'agent_prompt'"):
        make_sandbox_agent_fn(node_def)


def test_whitespace_only_agent_prompt_raises_value_error():
    """A whitespace-only agent_prompt is treated as missing."""
    node_def = {
        "id": "n1",
        "agent_prompt": "   ",
        "agent_command": _AGENT_COMMAND,
    }
    with pytest.raises(ValueError, match="missing required 'agent_prompt'"):
        make_sandbox_agent_fn(node_def)


def test_missing_agent_commands_only_raises_value_error():
    """agent_commands with an empty list is the same as missing."""
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_commands": [],
    }
    with pytest.raises(ValueError, match="missing required 'agent_command'"):
        make_sandbox_agent_fn(node_def)


def test_with_agent_command_returns_callable():
    """A node_def with agent_command resolves without raising and returns a callable."""
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": "opencode run --auto --format json < /home/user/prompt.md",
    }
    fn = make_sandbox_agent_fn(node_def)
    assert callable(fn)


def test_with_agent_commands_returns_callable():
    """agent_commands list is joined and resolved without raising."""
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_commands": ["echo start", "opencode run"],
    }
    fn = make_sandbox_agent_fn(node_def)
    assert callable(fn)


# ---------------------------------------------------------------------------
# FAR-212 PR A: the sandbox_agent builder forwards its node_def to the
# conformance gate so the mechanically-derived sandbox capability surface
# (egress; write/git-credential surfaces resolve unknown) reaches the live
# manifest.
# ---------------------------------------------------------------------------


async def test_sandbox_agent_passes_node_def_to_conformance_gate(monkeypatch):
    """The builder's node function calls the conformance gate with the node's
    full definition dict — the live-manifest reader needs the ACTUAL config
    (egress_policy) to certify egress impossibility. write_files / git_credentials
    are not certified from node keys (no enforced surface yet — FAR-212 PR B).
    Without the forwarding the sandbox surface would stay absent entirely and
    even the egress guarantee could not be certified."""
    import modulo.core.pipeline_engine.node_runner as nr

    node_def = _base_node_def(egress_policy="deny_all", read_only=True)
    fn = make_sandbox_agent_fn(node_def)

    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(nr, "_run_conformance_gate", gate)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    gate.assert_awaited_once()
    kwargs = gate.await_args.kwargs
    assert kwargs["node_def"] is node_def
    assert kwargs["node_id"] == "n1"


# ---------------------------------------------------------------------------
# Per-run agent runtime cost
# ---------------------------------------------------------------------------


def test_compute_sandbox_cost_hour_at_configured_rate():
    """3600s of sandbox uptime at the configured rate equals the rate itself.

    Default rate is 0.13 USD/hr, so one full hour of uptime estimates 0.13 USD.
    """
    expected = round(_E2B_SANDBOX_USD_PER_HOUR, 6)
    assert _compute_sandbox_cost(3600.0, None) == expected
    assert isinstance(_compute_sandbox_cost(3600.0, None), float)
    assert _compute_sandbox_cost(0.0, None) == 0.0


def test_compute_sandbox_cost_merges_agent_reported():
    """The agent's self-reported cost_estimate_usd is merged with the sandbox estimate."""
    # No sandbox uptime (elapsed 0) but agent reported 0.25 → total 0.25.
    assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": 0.25}) == 0.25
    # String numerics are accepted (JSON output can carry them).
    assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": "0.25"}) == 0.25
    # Non-numeric / missing agent-reported values are ignored (contribute 0).
    assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": "n/a"}) == 0.0
    assert _compute_sandbox_cost(0.0, {"summary": "no cost field"}) == 0.0
    assert _compute_sandbox_cost(0.0, None) == 0.0
    # Non-finite values (NaN/inf) must not corrupt the estimate.
    assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": "nan"}) == 0.0
    assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": "inf"}) == 0.0
    assert _compute_sandbox_cost(3600.0, {"cost_estimate_usd": float("inf")}) == round(_E2B_SANDBOX_USD_PER_HOUR, 6)


async def test_sandbox_agent_success_output_includes_cost_estimate_usd():
    """The success path attaches a numeric cost_estimate_usd to the node output.

    cost_estimate_usd = sandbox uptime x rate (tiny for a mocked instant run)
    + the agent's self-reported 0.001 from /home/user/output.json.
    """
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": "opencode run --auto --format json < /home/user/prompt.md",
    }
    fn = make_sandbox_agent_fn(node_def)

    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read_router('{"summary": "done", "cost_estimate_usd": 0.001}'))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(
            {
                "run_context": {"input": {"task": "x"}},
                "_run_id": "run-1",
                "_pipeline_id": "pipe-1",
                "_org_id": "org-1",
            }
        )

    assert result["output"]["status"] == "completed"
    assert isinstance(result["output"]["cost_estimate_usd"], float)
    # sandbox uptime cost >= 0 plus the agent-reported 0.001.
    assert result["output"]["cost_estimate_usd"] >= 0.001
    # Artifact output mirrors the node output cost.
    assert result["artifacts"][0]["output"]["cost_estimate_usd"] == result["output"]["cost_estimate_usd"]
    assert isinstance(result["artifacts"][0]["output"]["cost_estimate_usd"], float)


# ---------------------------------------------------------------------------
# {{ secrets.KEY }} env var resolution (org vault; unresolved refs are omitted,
# never empty-string clobbers — FAR-480)
# ---------------------------------------------------------------------------


def _base_node_def(**overrides) -> dict:
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": _AGENT_COMMAND,
    }
    node_def.update(overrides)
    return node_def


def _run_state() -> dict:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


async def test_env_var_secret_ref_missing_omits_key_and_warns(caplog):
    """No session_factory -> unresolved ref is OMITTED (not '') plus a warning
    naming the env var and secret key (FAR-480)."""
    node_def = _base_node_def(env_vars={"FOO": "{{ secrets.FOO }}"})
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with (
        caplog.at_level(logging.WARNING),
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch.dict(os.environ, {}, clear=True),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert "FOO" not in envs
    warnings = [m for m in caplog.messages if "env_var.secret_ref_not_found" in m]
    assert warnings, "expected an env_var.secret_ref_not_found warning"
    assert "FOO" in warnings[0]
    assert any("env_var.secret_ref_no_db_context" in m for m in caplog.messages)


async def test_env_var_secret_ref_resolves_from_vault():
    """With session_factory, {{ secrets.FOO }} resolves from the org vault."""
    node_def = _base_node_def(env_vars={"FOO": "{{ secrets.FOO }}"})
    session = MagicMock()
    session.__aenter__.return_value = session
    session.begin.return_value = AsyncMock()
    backend = MagicMock()
    backend.get_secret = AsyncMock(return_value="vault-secret")

    def _fake_session_factory():
        return session

    fn = make_sandbox_agent_fn(node_def, session_factory=_fake_session_factory)
    sandbox = _make_sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="test-key")),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch("modulo.db.rls.set_rls_execution_context", new=AsyncMock()),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["FOO"] == "vault-secret"
    backend.get_secret.assert_awaited_once_with("FOO")


async def test_env_var_secret_ref_does_not_fall_back_to_host_env(caplog):
    """Vault raises KeyError but the host env has FOO -> no fallback (security)."""
    node_def = _base_node_def(env_vars={"FOO": "{{ secrets.FOO }}"})
    session = MagicMock()
    session.__aenter__.return_value = session
    session.begin.return_value = AsyncMock()
    backend = MagicMock()
    backend.get_secret = AsyncMock(side_effect=KeyError("FOO"))

    def _fake_session_factory():
        return session

    fn = make_sandbox_agent_fn(node_def, session_factory=_fake_session_factory)
    sandbox = _make_sandbox_mock()

    with (
        caplog.at_level(logging.WARNING),
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="test-key")),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch("modulo.db.rls.set_rls_execution_context", new=AsyncMock()),
        patch.dict(os.environ, {"FOO": "host-value"}),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert "FOO" not in envs
    assert any("env_var.secret_ref_not_found" in m for m in caplog.messages)


async def test_env_var_plain_value_passes_through():
    """Non-reference env var values are passed through unchanged."""
    node_def = _base_node_def(env_vars={"FOO": "plain-value"})
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["FOO"] == "plain-value"


async def test_resolve_env_var_refs_calls_resolver_per_ref():
    """resolve_env_var_refs calls the async resolver only for secret refs."""
    calls: list[str] = []

    async def _resolver(secret_key: str) -> str | None:
        calls.append(secret_key)
        return {"A": "a-secret"}.get(secret_key)

    resolved = await resolve_env_var_refs({"A": "{{ secrets.A }}", "B": "plain", "C": "{{ secrets.C }}"}, _resolver)
    assert calls == ["A", "C"]
    # C is unresolved -> omitted entirely (FAR-480), never an empty string.
    assert resolved == {"A": "a-secret", "B": "plain"}


async def test_resolve_env_var_refs_unresolved_ref_warns_with_key_names(caplog):
    """An unresolved {{ secrets.X }} ref logs a WARNING naming the env var key
    and the secret key (FAR-480) and is omitted from the result."""
    calls: list[str] = []

    async def _resolver(secret_key: str) -> str | None:
        calls.append(secret_key)
        return None

    with caplog.at_level(logging.WARNING):
        resolved = await resolve_env_var_refs({"GITHUB_TOKEN": "{{ secrets.GITHUB_TOKEN }}", "PLAIN": "x"}, _resolver)

    assert calls == ["GITHUB_TOKEN"]
    assert resolved == {"PLAIN": "x"}
    warnings = [m for m in caplog.messages if "env_var.secret_ref_not_found" in m]
    assert warnings, "expected an env_var.secret_ref_not_found warning"
    assert "GITHUB_TOKEN" in warnings[0]


async def test_unresolved_secret_ref_does_not_clobber_system_default(caplog):
    """FAR-480 regression: an unresolved {{ secrets.GITHUB_TOKEN }} env ref must
    NOT emit an empty GITHUB_TOKEN that clobbers the system-injected default
    (host GITHUB_DOGFOOD_PAT_* / GITHUB_TOKEN) — the system token survives."""
    node_def = _base_node_def(env_vars={"GITHUB_TOKEN": "{{ secrets.GITHUB_TOKEN }}"})
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with (
        caplog.at_level(logging.WARNING),
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch.dict(os.environ, {"GITHUB_TOKEN": "system-default-pat"}, clear=False),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["GITHUB_TOKEN"] == "system-default-pat"
    assert any("env_var.secret_ref_not_found" in m for m in caplog.messages)


async def test_sandbox_resolve_secret_ref_warns_on_missing_db_context(caplog):
    """_sandbox_resolve_secret_ref with no session_factory returns None and
    logs a warning naming the secret key (FAR-480 — was silent)."""
    from modulo.core.pipeline_engine.node_runner import _sandbox_resolve_secret_ref

    with caplog.at_level(logging.WARNING):
        result = await _sandbox_resolve_secret_ref("GITHUB_TOKEN", session_factory=None, org_id=_ORG_ID)

    assert result is None
    assert any("env_var.secret_ref_no_db_context" in m and "GITHUB_TOKEN" in m for m in caplog.messages)


async def test_sandbox_resolve_secret_ref_warns_on_invalid_org_id(caplog):
    """_sandbox_resolve_secret_ref with an unparseable org_id returns None and
    logs a warning naming the secret key (FAR-480 — was silent)."""
    from modulo.core.pipeline_engine.node_runner import _sandbox_resolve_secret_ref

    session = MagicMock()

    def _fake_session_factory():
        return session

    with caplog.at_level(logging.WARNING):
        result = await _sandbox_resolve_secret_ref(
            "GITHUB_TOKEN", session_factory=_fake_session_factory, org_id="not-a-uuid"
        )

    assert result is None
    assert any("env_var.secret_ref_no_org_context" in m and "GITHUB_TOKEN" in m for m in caplog.messages)


async def test_sandbox_agent_command_timeout_raises_retryable_failure():
    """A timed-out command (cmd_result None, exit_code -1, EMPTY stdout/stderr)
    RAISES SandboxNodeFailedError with a clear explanation in the message —
    never a silent empty-summary failure and never a wrong-success completion
    (dist/runtime-core A6)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=TimeoutError("command timed out"))
    sandbox.files.read = AsyncMock(side_effect=TimeoutError("no output.json"))
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    assert "no output" in str(excinfo.value)
    assert "30s" in str(excinfo.value)
    sandbox.kill.assert_awaited()


# ---------------------------------------------------------------------------
# FAR-97: E2B idle watchdog + kill-before-output-read
# ---------------------------------------------------------------------------


async def test_idle_watchdog_kills_stalled_command_and_raises():
    """A command whose sandbox connection dies (drain probe fails for
    _SANDBOX_IDLE_TIMEOUT) is killed and the node fails fast with a retryable
    SandboxNodeFailedError — it does not block for the full sandbox_timeout
    (FAR-97 / dist/runtime-core A6)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    handle.kill = AsyncMock()

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.files.read = AsyncMock(return_value='{"summary": "fabricated"}')
    # The sandbox connection is dead: the drain probe fails on every tick, so the
    # idle watchdog's liveness signal goes stale and the watchdog fires.
    sandbox.files.get_info = AsyncMock(side_effect=OSError("sandbox connection dead"))
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_IDLE_TIMEOUT", 1.0),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_TAIL_INTERVAL", 0.01),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    # The stalled command itself was killed...
    handle.kill.assert_awaited()
    # ...and the still-running sandbox was killed before output.json could be
    # read — the interrupted process must not fabricate a completion.
    sandbox.kill.assert_awaited()
    sandbox.files.read.assert_not_called()
    assert "no output" in str(excinfo.value)


async def test_timed_out_command_does_not_read_output_json():
    """On a timed-out command the sandbox is killed and output.json is NOT read —
    an interrupted-but-alive agent could otherwise fabricate a completion (FAR-97).
    The node raises a retryable SandboxNodeFailedError."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=TimeoutError("command timed out"))
    sandbox.files.read = AsyncMock(return_value='{"summary": "fabricated"}')
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    assert "no output" in str(excinfo.value)
    assert "30s" in str(excinfo.value)
    sandbox.files.read.assert_not_called()
    sandbox.kill.assert_awaited()


async def test_background_command_success_still_completes():
    """A successful background command (handle path) still completes normally
    and reads the agent's output.json (FAR-97 regression guard)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert sandbox.commands.run.call_args.kwargs["background"] is True
    assert result["output"]["status"] == "completed"
    assert result["output"]["summary"] == "done"
    assert result["output"]["agent_stdout"] == "agent stdout"


# ---------------------------------------------------------------------------
# FAR-97 pipe-buffer fix: stdout redirected to a sandbox log file + drain probe
# ---------------------------------------------------------------------------


async def test_sandbox_command_stdout_redirected_to_log_file():
    """The agent command is wrapped so stdout/stderr are redirected to a sandbox
    log file — the process's stdout is a regular file, never a pipe that can fill
    and block a long session (FAR-97)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert "> /home/user/agent.log 2>&1" in wrapped
    assert "( opencode run --auto --format json < /home/user/prompt.md )" in wrapped


async def test_drain_probe_keeps_silent_live_agent_alive():
    """A live agent that stops producing NEW log output is NOT killed by the idle
    watchdog — liveness comes from the drain probe (get_info success on every
    tick), so a silent-but-connected agent gets the full timeout budget to finish
    (FAR-97)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = ""
    cmd_result.stderr = ""

    wait_calls = {"n": 0}

    async def _wait():
        wait_calls["n"] += 1
        if wait_calls["n"] < 3:
            raise TimeoutError
        return cmd_result

    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=_wait)
    handle.kill = AsyncMock()

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    # The log file exists and is responsive but only grew once — the agent then
    # fell into a long silent phase (e.g. an LLM turn) with no new output.
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=64))
    sandbox.files.read = AsyncMock(side_effect=_read_router('{"summary": "done"}', log_content="x" * 64))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_IDLE_TIMEOUT", 1.0),
        # Tick must stay above the Windows monotonic-clock quantum (15.6ms): at
        # 0.01 the per-slice shield timeout fires immediately and loses the
        # mock's result, so the command appears to time out at the full 30s.
        # 0.05 keeps the test fast (<2s) while staying above the quantum.
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_TAIL_INTERVAL", 0.05),
    ):
        result = await fn(_run_state())

    output = result["output"]
    assert output["status"] == "completed"
    # The idle watchdog must NOT have killed the live-but-silent process.
    handle.kill.assert_not_awaited()
    # The drain probe ran and refreshed liveness on every tick.
    sandbox.files.get_info.assert_awaited()
    # The drained log content is the artifact's stdout.
    assert output["agent_stdout"] == "x" * 64
    assert output["stdout_length"] == 64


async def test_drain_captures_pipe_buffer_size_output():
    """Output larger than a typical 64KB pipe buffer is drained from the sandbox
    log file and captured in full — the process never blocks on a full stdout
    pipe and the artifact carries the complete output (FAR-97)."""
    big = "y" * (65536 + 1234)
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock(log_content=big)

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    output = result["output"]
    assert output["status"] == "completed"
    assert output["agent_stdout"] == big
    assert output["stdout_length"] == len(big)


async def test_drain_no_double_emit_when_log_grows_between_probe_and_read():
    """The agent appending between get_info and the read must not double-emit.

    get_info reports the file size at probe time; the read returns the FULL
    file, which can be longer if the agent appended in between. The drain
    offset must advance to len(text) (not the stale get_info size) so the next
    tick starts after the bytes that were actually emitted — otherwise bytes
    [size, len(text)) are re-emitted on the following tick (D3 no-double-emit
    invariant).

    Scenario: tick 1 reads 50 bytes; on tick 2 the probe reports 100 but the
    read returns 150 (50 bytes appended after the probe). The correct final
    artifact is exactly 150 bytes; a stale offset re-emits the last 50.
    """
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    probe_sizes = iter([50, 100, 150, 150])
    read_contents = iter(["x" * 50, "x" * 150, "x" * 150, "x" * 150])

    def _get_info(path, **kwargs):
        return MagicMock(size=next(probe_sizes))

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return '{"summary": "done"}'
        return next(read_contents)

    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = ""
    cmd_result.stderr = ""

    wait_calls = {"n": 0}

    async def _wait():
        wait_calls["n"] += 1
        if wait_calls["n"] < 3:
            raise TimeoutError
        return cmd_result

    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=_wait)
    handle.kill = AsyncMock()

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.get_info = AsyncMock(side_effect=_get_info)
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_TAIL_INTERVAL", 0.05),
    ):
        result = await fn(_run_state())

    output = result["output"]
    assert output["status"] == "completed"
    assert output["agent_stdout"] == "x" * 150
    assert output["stdout_length"] == 150


async def test_drain_window_lossless_when_truncation_and_probe_lag_race():
    """D3 trailing-window truncation must not drop bytes when the probe lags the read.

    The drain window retains only the last _MAX_DRAIN_WINDOW bytes of the log,
    but the retained slice's absolute start was derived from the STALE get_info
    probe ``size`` instead of the content length the read actually returned.
    When the log exceeds the window AND the agent appends between the probe and
    the read, the true window spans [len(content)-WINDOW, len(content)) but the
    slice started at [size-WINDOW, ...) — shifted left — so the emitted chunk
    skipped the first (len - size) bytes of new in-window content, which were
    marked drained and permanently lost (live stream and node artifact).

    Scenario (WINDOW patched to 100): tick 1 probe 50 / read 50 emits [0,50).
    On tick 2 the probe reports 100 but the read returns 150 bytes; truncation
    keeps the last 100 = absolute bytes [50,150). The correct emitted chunk is
    the FULL window [50,150); the buggy slice emitted [100,150) and dropped
    [50,100), then a third tick (probe 150) re-emitted [100,150) — a double-emit.
    The final artifact must be byte-for-byte the in-window content [50,150) =
    "n"*50+"p"*50 with NO lost and NO doubled bytes.
    """
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    tick2_content = ("m" * 50) + ("n" * 50) + ("p" * 50)  # absolute bytes [0,150)
    probe_sizes = iter([50, 100, 150, 150])
    read_contents = iter(["m" * 50, tick2_content, tick2_content, tick2_content])

    def _get_info(path, **kwargs):
        return MagicMock(size=next(probe_sizes))

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return '{"summary": "done"}'
        return next(read_contents)

    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = ""
    cmd_result.stderr = ""

    wait_calls = {"n": 0}

    async def _wait():
        wait_calls["n"] += 1
        if wait_calls["n"] < 3:
            raise TimeoutError
        return cmd_result

    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=_wait)
    handle.kill = AsyncMock()

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.get_info = AsyncMock(side_effect=_get_info)
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._MAX_DRAIN_WINDOW", 100),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_TAIL_INTERVAL", 0.05),
    ):
        result = await fn(_run_state())

    output = result["output"]
    assert output["status"] == "completed"
    assert output["agent_stdout"] == ("n" * 50) + ("p" * 50)
    assert output["stdout_length"] == 100


# ---------------------------------------------------------------------------
# FAR-98: first-class stall detection — stall_timeout_seconds + stall_reason
# ---------------------------------------------------------------------------


async def test_idle_watchdog_normal_completion_returns_none_reason():
    """_wait_command_with_idle_watchdog returns (cmd_result, None) on normal completion."""
    handle = MagicMock()
    cmd_result = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    result, stall_reason = await _wait_command_with_idle_watchdog(
        handle,
        total_timeout=30.0,
        idle_timeout=60.0,
        last_activity=lambda: time.monotonic(),
    )
    assert result is cmd_result
    assert stall_reason is None


async def test_idle_watchdog_stall_returns_reason_not_raise():
    """A silent agent returns (None, stall_reason) instead of raising — the
    caller can distinguish a STALL from a TOTAL-TIMEOUT (FAR-98)."""
    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    handle.kill = AsyncMock()

    def _stale_last_activity() -> float:
        return time.monotonic() - 120.0

    result, stall_reason = await _wait_command_with_idle_watchdog(
        handle,
        total_timeout=30.0,
        idle_timeout=60.0,
        last_activity=_stale_last_activity,
    )
    assert result is None
    assert stall_reason is not None
    assert "no output" in stall_reason
    assert "60s" in stall_reason
    handle.kill.assert_awaited()


async def test_stall_timeout_seconds_config_passed_to_watchdog():
    """node_def stall_timeout_seconds flows into the idle watchdog as idle_timeout."""
    node_def = _base_node_def(timeout_seconds=30, stall_timeout_seconds=60)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    cmd_result = sandbox.commands.run.return_value.wait.return_value
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.pipeline_engine.node_runner._wait_command_with_idle_watchdog",
            new=AsyncMock(return_value=(cmd_result, None)),
        ) as watchdog,
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    watchdog.assert_awaited_once()
    assert watchdog.await_args.kwargs["idle_timeout"] == 60


async def test_stalled_command_raises_with_stall_reason():
    """A stalled command raises SandboxNodeFailedError carrying the stall reason
    and still kills the sandbox before output.json can be read (FAR-98 /
    dist/runtime-core A6)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    handle.kill = AsyncMock()

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.files.read = AsyncMock(return_value='{"summary": "fabricated"}')
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_IDLE_TIMEOUT", 1.0),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    assert "no output" in str(excinfo.value)
    handle.kill.assert_awaited()
    sandbox.kill.assert_awaited()
    sandbox.files.read.assert_not_called()


# FAR-98: live stdout/stderr streaming via run event broker
# ---------------------------------------------------------------------------


def _with_registered_broker(broker) -> dict:
    """Register the broker in the process-local registry keyed by its run id and
    return a state dict whose _run_id matches, so the sandbox_agent node streams
    through the registry lookup instead of a _broker key carried in state (the
    broker is not msgpack-serializable, so it must not live in LangGraph state)."""
    get_registry()._brokers[broker.run_id] = broker
    return {**_run_state(), "_run_id": str(broker.run_id)}


async def test_on_stdout_buffers_and_flushes_joined_chunk():
    """Within the flush interval chunks are buffered; crossing the boundary
    flushes the joined buffer in a single node.stdout_chunk event (FAR-98)."""
    from modulo.core.pipeline_engine.event_broker import RunEventBroker

    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    broker = RunEventBroker(uuid.uuid4())

    try:
        with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
            result = await fn(_with_registered_broker(broker))

        assert result["output"]["status"] == "completed"
        on_stdout = sandbox.commands.run.call_args.kwargs["on_stdout"]
        await on_stdout("line one\n")  # first chunk publishes immediately
        await on_stdout("line two\n")  # within the 1s window -> buffered
        await asyncio.sleep(1.05)  # cross the flush boundary
        await on_stdout("line three\n")  # flushes joined buffer + this chunk

        chunk_events = [e for e in broker.replay_since(0) if e.event_type == "node.stdout_chunk"]
        assert len(chunk_events) == 2
        assert chunk_events[0].payload["chunk"] == "line one\n"
        assert chunk_events[1].payload["chunk"] == "line two\nline three\n"
        payload = chunk_events[1].payload
        assert payload["node_id"] == "n1"
        assert payload["seq"] == chunk_events[1].seq
        assert isinstance(payload["ts"], int)
    finally:
        get_registry().close(broker.run_id)


async def test_on_stdout_publishes_unthrottled_when_interval_elapsed():
    """With a zero flush interval, each stdout chunk publishes its own event."""
    from modulo.core.pipeline_engine.event_broker import RunEventBroker

    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    broker = RunEventBroker(uuid.uuid4())

    try:
        with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
            result = await fn(_with_registered_broker(broker))

        assert result["output"]["status"] == "completed"
        on_stdout = sandbox.commands.run.call_args.kwargs["on_stdout"]
        with patch("modulo.core.pipeline_engine.node_runner._STREAM_FLUSH_INTERVAL", 0.0):
            await on_stdout("a")
            await on_stdout("b")

        chunk_events = [e for e in broker.replay_since(0) if e.event_type == "node.stdout_chunk"]
        assert len(chunk_events) == 2
        assert chunk_events[0].payload["chunk"] == "a"
        assert chunk_events[1].payload["chunk"] == "b"
    finally:
        get_registry().close(broker.run_id)


async def test_on_stderr_publishes_stderr_chunk_event():
    """on_stderr publishes a node.stderr_chunk event with the chunk."""
    from modulo.core.pipeline_engine.event_broker import RunEventBroker

    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    broker = RunEventBroker(uuid.uuid4())

    try:
        with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
            result = await fn(_with_registered_broker(broker))

        assert result["output"]["status"] == "completed"
        on_stderr = sandbox.commands.run.call_args.kwargs["on_stderr"]
        await on_stderr("warn: something")

        stderr_events = [e for e in broker.replay_since(0) if e.event_type == "node.stderr_chunk"]
        assert len(stderr_events) == 1
        assert stderr_events[0].payload["chunk"] == "warn: something"
        assert stderr_events[0].payload["node_id"] == "n1"
    finally:
        get_registry().close(broker.run_id)


async def test_streaming_skipped_when_no_broker_registered_for_run():
    """Without a broker registered for the run id (a non-UUID run id or no
    registration), on_stdout/on_stderr skip silently — no error, no publish,
    node completes normally."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    on_stdout = sandbox.commands.run.call_args.kwargs["on_stdout"]
    on_stderr = sandbox.commands.run.call_args.kwargs["on_stderr"]
    await on_stdout("ignored")
    await on_stderr("ignored")
    assert result["output"]["status"] == "completed"
    assert result["output"]["summary"] == "done"


# FAR-306: live stdout/stderr streaming must redact injected credentials before
# publishing to the run broker (the persistence path already redacts raw output).
# ---------------------------------------------------------------------------


async def test_stream_redacts_credentials_from_live_stdout_chunk():
    """A credential-bearing stdout chunk is scrubbed before the broker payload
    is published, so live output in the Run UI never exposes injected tokens
    (tokenized git URLs, ghp_/Bearer tokens). (FAR-306)"""
    from modulo.core.pipeline_engine.event_broker import RunEventBroker

    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    broker = RunEventBroker(uuid.uuid4())

    try:
        with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
            result = await fn(_with_registered_broker(broker))

        assert result["output"]["status"] == "completed"
        on_stdout = sandbox.commands.run.call_args.kwargs["on_stdout"]
        with patch("modulo.core.pipeline_engine.node_runner._STREAM_FLUSH_INTERVAL", 0.0):
            await on_stdout("cloning https://x-access-token:ghp_ABC123@github.com/farnalabs/modulo.git\n")
            await on_stdout('auth "Bearer sk-proj-SECRETKEY" token=myapikey\n')

        chunk_events = [e for e in broker.replay_since(0) if e.event_type == "node.stdout_chunk"]
        assert chunk_events, "expected at least one streamed chunk event"
        joined = "".join(ev.payload["chunk"] for ev in chunk_events)

        assert "ghp_ABC123" not in joined
        assert "sk-proj-SECRETKEY" not in joined
        assert "myapikey" not in joined
        assert "https://<redacted>@github.com/farnalabs/modulo.git" in joined
        assert "Bearer <redacted>" in joined
        assert "token=<redacted>" in joined
    finally:
        get_registry().close(broker.run_id)


async def test_stream_redaction_is_idempotent_on_already_redacted_chunk():
    """Re-publishing an already-redacted chunk does not double-scrub it into
    mojibake — redacting an already-redacted chunk yields the same output
    (drained content can be re-streamed safely). (FAR-306)"""
    from modulo.core.pipeline_engine.event_broker import RunEventBroker

    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    broker = RunEventBroker(uuid.uuid4())

    try:
        with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
            result = await fn(_with_registered_broker(broker))

        assert result["output"]["status"] == "completed"
        on_stdout = sandbox.commands.run.call_args.kwargs["on_stdout"]
        with patch("modulo.core.pipeline_engine.node_runner._STREAM_FLUSH_INTERVAL", 0.0):
            await on_stdout("https://x-access-token:ghp_ABC123@github.com/farnalabs/modulo.git\n")
            await on_stdout("token=abc123\n")

        chunk_events = [e for e in broker.replay_since(0) if e.event_type == "node.stdout_chunk"]
        joined = "".join(ev.payload["chunk"] for ev in chunk_events)
        assert "ghp_ABC123" not in joined
        assert "abc123" not in joined

        # Draining the already-scrubbed buffer must be stable (no double scrub).
        on_stderr = sandbox.commands.run.call_args.kwargs["on_stderr"]
        with patch("modulo.core.pipeline_engine.node_runner._STREAM_FLUSH_INTERVAL", 0.0):
            await on_stderr(joined)

        stderr_events = [e for e in broker.replay_since(0) if e.event_type == "node.stderr_chunk"]
        redrained = "".join(ev.payload["chunk"] for ev in stderr_events)
        assert redrained == joined
    finally:
        get_registry().close(broker.run_id)


# ---------------------------------------------------------------------------
# FAR-97 observability: stdout_length/stderr_length + sandbox trace at death
# ---------------------------------------------------------------------------


async def test_success_output_carries_full_stdout_length_when_truncated():
    """When stdout exceeds _MAX_ARTIFACT_LOG the stored agent_stdout is truncated
    but stdout_length/stderr_length report the FULL pre-truncation lengths — so
    consumers can tell 'stored-truncated' from a genuine cut (FAR-97)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    long_stdout = "x" * (_MAX_ARTIFACT_LOG + 1234)
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = long_stdout
    cmd_result.stderr = "stderr line"

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read_router('{"summary": "done"}'))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    output = result["output"]
    artifact = result["artifacts"][0]["output"]
    assert output["status"] == "completed"
    assert output["stdout_length"] == len(long_stdout)
    assert output["stderr_length"] == len("stderr line")
    assert output["agent_stdout"] == long_stdout[:_MAX_ARTIFACT_LOG]
    assert output["agent_stderr"] == "stderr line"
    assert artifact["stdout_length"] == output["stdout_length"]
    assert artifact["stderr_length"] == output["stderr_length"]


async def test_success_output_omits_sandbox_log_tail():
    """Success outputs do NOT carry sandbox_id/sandbox_log_tail — keep them small."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    sandbox.sandbox_id = "sbx-success"

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    assert "sandbox_log_tail" not in result["output"]
    assert "sandbox_id" not in result["output"]
    assert "sandbox_log_tail" not in result["artifacts"][0]["output"]


async def test_timed_out_command_output_includes_sandbox_id_and_log_tail():
    """A timed-out command's failure output carries sandbox_id + sandbox_log_tail
    (the E2B kill reason) and the tail is fetched BEFORE the sandbox is killed —
    the logs endpoint only serves live sandboxes (FAR-97)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    sandbox = MagicMock()
    sandbox.sandbox_id = "sbx-dead"
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=TimeoutError("command timed out"))
    sandbox.files.read = AsyncMock(side_effect=TimeoutError("no output.json"))
    sandbox.kill = AsyncMock()

    events: list[str] = []

    async def _fake_tail(*_args, **_kwargs):
        events.append("fetch")
        return "sample log line"

    def _record_kill(*_args, **_kwargs):
        events.append("kill")

    sandbox.kill.side_effect = _record_kill

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail", new=_fake_tail),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    # The timed-out command raises a retryable SandboxNodeFailedError (A6) — the
    # sandbox trace is no longer a node output but the fetch-before-kill
    # ordering guarantee is preserved (FAR-97).
    assert "no output" in str(excinfo.value)
    # The tail fetch precedes the kill so the still-live sandbox serves its logs.
    assert events[0] == "fetch"
    assert events.index("fetch") < events.index("kill")
    sandbox.kill.assert_awaited()


# ---------------------------------------------------------------------------
# FAR-197: no-output.json failure surfaces the agent stdout/stderr tail
# ---------------------------------------------------------------------------


async def _completed_sandbox(exit_code: int, *, stdout: str = "", stderr: str = "", output_json: str = ""):
    """Build a sandbox mock whose command COMPLETED (cmd_result not None) but
    whose output.json read returns *output_json* (default empty = missing)."""
    cmd_result = MagicMock()
    cmd_result.exit_code = exit_code
    cmd_result.stdout = stdout
    cmd_result.stderr = stderr

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    # get_info reports size 0 so the drain probe never transfers log content —
    # agent_stdout_raw then falls back to cmd_result.stdout deterministically.
    sandbox.files.read = AsyncMock(side_effect=_read_router(output_json))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


async def test_no_output_json_message_includes_stdout_stderr_tail_and_sandbox_id():
    """When the command completed but output.json is missing, the raised
    SandboxNodeFailedError explains WHY (stdout/stderr tail + sandbox id) instead
    of the opaque 'no parseable output.json' string (FAR-197)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = await _completed_sandbox(
        1,
        stdout="opencode: command not found\n",
        stderr="bash: opencode: command not found",
    )
    sandbox.sandbox_id = "sbx-far197"

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail",
            new=AsyncMock(return_value="sample log line"),
        ),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    message = str(excinfo.value)
    assert "no parseable output.json" in message
    assert "exit code 1" in message
    assert "opencode: command not found" in message
    assert "bash: opencode: command not found" in message
    assert "sbx-far197" in message
    assert "sample log line" in message


async def test_invalid_output_json_includes_what_was_read_bounded():
    """Invalid output.json is treated the same as missing — the message includes
    what was read (bounded) so the failure is diagnosable (FAR-197)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = await _completed_sandbox(0, stdout="booting agent", output_json="<<< not json >>>")
    sandbox.sandbox_id = "sbx-invalid"

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail",
            new=AsyncMock(return_value=""),
        ),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    message = str(excinfo.value)
    assert "exit code 0" in message
    assert "<<< not json >>>" in message  # what was read is surfaced, bounded


async def test_exit_code_zero_with_no_output_json_still_raises():
    """Exit code 0 with no parseable output.json is still a retryable
    SandboxNodeFailedError — a node with zero usable work must never complete
    silently (A6 / FAR-197)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = await _completed_sandbox(0, stdout="agent ran")
    sandbox.sandbox_id = "sbx-zero"

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail",
            new=AsyncMock(return_value=""),
        ),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    message = str(excinfo.value)
    assert "no parseable output.json" in message
    assert "exit code 0" in message


async def test_no_output_json_message_is_bounded_for_huge_stdout():
    """Huge agent stdout/stderr still produces a bounded message — the raised
    error never carries an unbounded string (FAR-197)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    huge = "z" * 200_000
    sandbox = await _completed_sandbox(1, stdout=huge, stderr=huge)
    sandbox.sandbox_id = "sbx-huge"

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail",
            new=AsyncMock(return_value="x" * 50_000),
        ),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    message = str(excinfo.value)
    # log tail (1024) + stderr (1536) + stdout (1024) caps + headers/markers.
    # The WHOLE message must stay under 5000 so it survives BOTH the
    # sanitizer's hard cap and the executor's terminal-fail write surface
    # (`_sanitize_detail("...: " + msg, limit=5000)`) + the String(5000)
    # runs.error_detail column — the diagnostic is never truncated away.
    assert len(message) < 5_000
    assert "...[truncated" in message
    # The kill-reason log tail leads the sections, so at ANY truncation the
    # highest-value diagnostic (the only place the kill reason lives) survives.
    assert message.index("--- sandbox log tail ---") < message.index("--- stderr tail ---")
    assert message.index("--- stderr tail ---") < message.index("--- stdout tail ---")


async def test_no_output_json_message_bounded_with_huge_raw_readback():
    """Worst case: huge stdout/stderr AND a huge invalid output.json readback
    still yields one bounded message < 5000 chars with no duplicated sections
    (FAR-197 merge-conflict regression guard).

    The read snippet documents itself LAST (per the section ordering), and each
    section header must appear exactly once — a duplicated stdout block (from a
    botched conflict resolution) inflated the message past the 5000-char
    sanitizer/column cap and silently truncated the diagnostic tail.
    """
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    huge = "z" * 200_000
    # Huge stdout/stderr AND a huge invalid output.json readback — the exact
    # invalid-JSON worst case that must stay bounded.
    sandbox = await _completed_sandbox(
        1,
        stdout=huge,
        stderr=huge,
        output_json="<<< not json >>>" + "y" * 200_000,
    )
    sandbox.sandbox_id = "sbx-huge-raw"

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail",
            new=AsyncMock(return_value="x" * 50_000),
        ),
        pytest.raises(SandboxNodeFailedError) as excinfo,
    ):
        await fn(_run_state())

    message = str(excinfo.value)
    assert len(message) < 5_000
    # Every section header appears exactly once — no duplicated blocks.
    assert message.count("--- stdout tail ---") == 1
    assert message.count("--- stderr tail ---") == 1
    assert message.count("--- sandbox log tail ---") == 1
    assert message.count("--- output.json read") == 1
    # Documented ordering: log tail (kill reason) -> stderr -> stdout -> raw readback last.
    assert message.index("--- sandbox log tail ---") < message.index("--- stderr tail ---")
    assert message.index("--- stderr tail ---") < message.index("--- stdout tail ---")
    assert message.index("--- stdout tail ---") < message.index("--- output.json read")


async def test_no_output_log_tail_fetched_before_kill():
    """On the no-output.json path the E2B log tail is fetched BEFORE the
    finally-block sandbox kill — the logs endpoint only serves live sandboxes
    (FAR-197)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = await _completed_sandbox(1)
    sandbox.sandbox_id = "sbx-order"

    events: list[str] = []

    async def _fake_tail(*_args, **_kwargs):
        events.append("fetch")
        return "sample log line"

    def _record_kill(*_args, **_kwargs):
        events.append("kill")

    sandbox.kill.side_effect = _record_kill

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail", new=_fake_tail),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert events[0] == "fetch"
    assert events.index("fetch") < events.index("kill")
    sandbox.kill.assert_awaited()


async def test_no_output_json_log_includes_lengths_and_sandbox_id(caplog):
    """The sandbox_agent.no_output_json INFO record carries stdout/stderr
    lengths + sandbox id for log-level diagnostics (FAR-197)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = await _completed_sandbox(1, stdout="abc", stderr="de")
    sandbox.sandbox_id = "sbx-log"

    with (
        caplog.at_level(logging.INFO, logger="modulo.core.pipeline_engine.node_runner"),
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.pipeline_engine.node_runner._fetch_sandbox_log_tail",
            new=AsyncMock(return_value=""),
        ),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    records = [r for r in caplog.records if r.getMessage() == "sandbox_agent.no_output_json"]
    assert records, "no_output_json INFO record not emitted"
    record = records[-1]
    assert record.exit_code == 1
    assert record.stdout_length == 3
    assert record.stderr_length == 2
    assert record.sandbox_id == "sbx-log"


def test_no_output_json_message_survives_executor_terminal_fail_surface():
    """The FAR-197 diagnostic is NOT truncated at the user-facing write surface.

    After retries are exhausted the executor writes the raised message to
    runs.error_detail via ``_sanitize_detail("Sandbox node failed (transient)
    after retries exhausted: " + str(exc), limit=5000)`` (executor.py) — the
    exact field the RunDetail view renders. Round-trip the WORST-CASE message
    (huge stdout/stderr + log tail) through that write and assert the whole
    diagnostic — including the kill-reason log tail, which is the first thing
    a 500-char cap would have cut — survives in full (FAR-197 review).
    """
    from modulo.core.pipeline_engine.executor import _sanitize_detail
    from modulo.core.pipeline_engine.node_runner import _build_no_output_message

    message = _build_no_output_message(
        exit_code=1,
        stdout_raw="z" * 200_000,
        stderr_raw="s" * 200_000,
        sandbox_id="sbx-roundtrip",
        read_raw="",
        log_tail="l" * 50_000 + " KILL_REASON_ONLY_HERE",
    )
    stored = _sanitize_detail("Sandbox node failed (transient) after retries exhausted: " + message, limit=5000)

    assert stored == "Sandbox node failed (transient) after retries exhausted: " + message
    assert "KILL_REASON_ONLY_HERE" in stored
    assert "--- sandbox log tail ---" in stored
    assert "--- stderr tail ---" in stored
    assert "--- stdout tail ---" in stored


async def test_fetch_sandbox_log_tail_returns_empty_without_api_key(monkeypatch):
    """No E2B key configured -> helper returns '' without attempting a fetch."""
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    with patch("urllib.request.urlopen") as _urlopen:
        assert not await _fetch_sandbox_log_tail("sbx-nokey")
    _urlopen.assert_not_called()


async def test_fetch_sandbox_log_tail_never_raises_on_network_failure(monkeypatch):
    """A failing urlopen (no network / non-2xx / garbage) is swallowed -> ''."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert not await _fetch_sandbox_log_tail("sbx-netfail")
    with patch("urllib.request.urlopen", side_effect=urllib.request.HTTPError("url", 401, "unauthorized", None, None)):
        assert not await _fetch_sandbox_log_tail("sbx-netfail")


async def test_fetch_sandbox_log_tail_returns_empty_for_invalid_id(monkeypatch):
    """A non-string/None sandbox id never triggers a network call."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-key")
    with patch("urllib.request.urlopen") as _urlopen:
        assert not await _fetch_sandbox_log_tail(None)
    _urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# agent_command Jinja rendering (LLM model as a per-run / per-parameter value)
# ---------------------------------------------------------------------------


def _model_node_def(command: str, **overrides) -> dict:
    node_def = _base_node_def(agent_command=command)
    node_def.update(overrides)
    return node_def


async def test_agent_command_renders_input_model():
    """`{{ input.model }}` in agent_command resolves from the run's input payload."""
    node_def = _model_node_def("opencode run --model {{ input.model }} --auto --format json < /home/user/prompt.md")
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(
            {
                "run_context": {"input": {"model": "opencode-go/mimo-v2.5"}},
                "_run_id": "run-1",
                "_pipeline_id": "pipe-1",
                "_org_id": _ORG_ID,
            }
        )

    assert result["output"]["status"] == "completed"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert "--model opencode-go/mimo-v2.5" in wrapped
    assert "{{" not in wrapped


async def test_agent_command_renders_parameter_model():
    """`{{ parameter.model }}` in agent_command resolves from the resolved parameter schema."""
    node_def = _model_node_def(
        "opencode run --model {{ parameter.model }} --auto --format json < /home/user/prompt.md",
        _resolved_parameters={"model": "opencode-go/mimo-v2.5"},
    )
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert "--model opencode-go/mimo-v2.5" in wrapped
    assert "{{" not in wrapped


async def test_agent_command_default_filter():
    """A missing input.model falls back to the `default()` filter value."""
    node_def = _model_node_def(
        'opencode run --model {{ input.model | default("opencode-go/deepseek-v4-flash") }} '
        "--auto --format json < /home/user/prompt.md"
    )
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert "--model opencode-go/deepseek-v4-flash" in wrapped
    assert "{{" not in wrapped


async def test_agent_command_without_template_unchanged():
    """A plain agent_command (no {{ }} templates) executes byte-for-byte unchanged."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert f"( {_AGENT_COMMAND} )" in wrapped
    assert _AGENT_COMMAND in wrapped


async def test_agent_command_undefined_error_skips():
    """A command template referencing a missing nested input field raises
    UndefinedError -> the node returns status 'skipped' and NO sandbox command
    is executed (mirrors the prompt's UndefinedError handling)."""
    node_def = _model_node_def(
        "opencode run --model {{ input.missing.deep.path }} --auto --format json < /home/user/prompt.md"
    )
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["status"] == "skipped"
    assert "agent_command" in result["summary"]
    sandbox.commands.run.assert_not_called()


async def test_agent_command_invalid_template_falls_back_verbatim():
    """A legacy command with Jinja-like syntax that is NOT a valid template
    (e.g. an empty ``{{ }}`` or an unclosed ``{{``) must NOT crash the run with
    TemplateSyntaxError — it executes verbatim, as it did before #1291."""
    node_def = _model_node_def(
        "opencode run --model deepseek-v4-flash {{ }} --auto --format json < /home/user/prompt.md"
    )
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert "{{ }}" in wrapped
    assert sandbox.commands.run.call_count == 1


async def test_agent_command_unclosed_template_falls_back_verbatim():
    """An unclosed ``{{`` in a legacy command is a TemplateSyntaxError — falls
    back to verbatim execution instead of crashing the run."""
    node_def = _model_node_def("opencode run --model {{ --auto --format json < /home/user/prompt.md")
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert "{{" in wrapped
    assert sandbox.commands.run.call_count == 1


async def test_agent_command_empty_after_render_fails():
    """A command that renders to empty (e.g. `{{ input.model }}` with no default
    and no value) fails with a clear ValueError and NO sandbox command executes."""
    node_def = _model_node_def("{{ input.model }}")
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(ValueError, match="rendered agent_command is empty"),
    ):
        await fn(_run_state())

    sandbox.commands.run.assert_not_called()


# ---------------------------------------------------------------------------
# FAR-306: opt-in stall detectors — _StallDetector + _delta_ratio unit tests
# ---------------------------------------------------------------------------


def test_stall_detector_last_activity_is_max_across_enabled_channels():
    """last_activity() returns the most recent activity across all ENABLED
    channels — the all-channels-silent rule the watchdog relies on."""
    d = _StallDetector()
    d.enable("heartbeat")
    d.enable("log_growth")
    d.touch("heartbeat")
    hb = d._activity["heartbeat"]
    assert d.last_activity() == hb
    d.touch("log_growth")
    assert d.last_activity() == d._activity["log_growth"]


def test_stall_detector_disabled_channel_touch_is_noop():
    """A disabled channel's touch() does not update last_activity — so an
    opt-out heartbeat cannot be resurrected by a stray probe."""
    d = _StallDetector()
    d.enable("heartbeat")
    d.touch("heartbeat")
    before = d.last_activity()
    d.touch("log_growth")
    assert d.last_activity() == before


def test_stall_detector_disable_removes_channel():
    """disable() removes a channel from the enabled set."""
    d = _StallDetector()
    d.enable("heartbeat")
    d.enable("stdout")
    d.disable("heartbeat")
    assert d.enabled == {"stdout"}


def test_stall_detector_no_channels_never_stalls():
    """With zero enabled channels last_activity() returns now, so the watchdog
    never fires (belt-and-braces against a fully-disabled node)."""
    d = _StallDetector()
    assert d.last_activity() >= time.monotonic() - 1.0


def test_delta_ratio_semantics():
    """_delta_ratio measures the fraction of new content that differs from prev
    (absolute-growth semantics for the stdout-delta detector)."""
    assert _delta_ratio("aaaa", "aaaa") == 0.0
    assert _delta_ratio("aaaa", "bbbb") == 1.0
    assert _delta_ratio("", "aaaa") == 1.0
    assert _delta_ratio("aaaa", "") == 0.0
    assert _delta_ratio("a", "ab") > 0.0
    assert _delta_ratio("a", "a") == 0.0
    assert 0.0 < _delta_ratio("hello", "hellox") < 1.0


# ---------------------------------------------------------------------------
# FAR-306: watch_log_path (log-growth) detector keeps a silent run alive
# ---------------------------------------------------------------------------


async def test_watch_log_growth_keeps_silent_run_alive():
    """When the agent's own log stays flat but a user-configured watch_log_path
    keeps growing, the run must NOT stall (the log-growth channel is active)."""
    node_def = _base_node_def(timeout_seconds=30, watch_log_path="/home/user/progress.log")
    fn = make_sandbox_agent_fn(node_def)

    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = ""
    cmd_result.stderr = ""

    watch_sizes = iter([10, 20, 30])
    wait_calls = {"n": 0}

    async def _wait():
        wait_calls["n"] += 1
        if wait_calls["n"] < 3:
            raise TimeoutError
        return cmd_result

    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=_wait)
    handle.kill = AsyncMock()

    def _get_info(path, **kwargs):
        if str(path).endswith("progress.log"):
            return MagicMock(size=next(watch_sizes))
        return MagicMock(size=0)

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return '{"summary": "done"}'
        return ""

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.get_info = AsyncMock(side_effect=_get_info)
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.list = AsyncMock(return_value=[])
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_TAIL_INTERVAL", 0.05),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    handle.kill.assert_not_awaited()


async def test_watch_log_growth_keeps_silent_strict_run_alive_end_to_end():
    """End-to-end proof that the log-growth detector wires up through
    make_sandbox_agent_fn: with enable_heartbeat=False (strict mode) and a fully
    silent agent (its own log never grows), a user-configured watch_log_path
    that keeps growing is the ONLY channel keeping the run alive.

    This is the test that genuinely fails without the detector: with the
    heartbeat disabled the drain probe's heartbeat touch is a no-op and the
    agent's ``output`` channel stays stale, so the idle watchdog fires and the
    run raises SandboxNodeFailedError unless ``_probe_log_growth`` refreshes
    the ``log_growth`` channel every tick.
    """
    node_def = _base_node_def(
        timeout_seconds=30,
        stall_timeout_seconds=1.0,
        enable_heartbeat=False,
        watch_log_path="/home/user/progress.log",
    )
    fn = make_sandbox_agent_fn(node_def)

    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = ""
    cmd_result.stderr = ""

    # The command stays "running" for 44 tick slices (each ~0.03s) before
    # completing on the 45th, so the run lasts ~1.35s in total. That EXCEEDS
    # the 1.0s stall window: without the log-growth detector the idle watchdog
    # fires mid-run and this test fails in every environment, not just a loaded
    # one. With the detector the log-growth touch on every tick keeps the run
    # alive to completion. The stall window (1.0s) is kept 20x wider than the
    # patched tick interval (0.05s) so a loaded event loop can never stretch a
    # single tick past it and trip the idle watchdog spuriously — the original
    # 0.1s window (only 2x the 0.05s tick) flaked ~50-70% under suite load
    # because the per-iteration gap is bounded by the wait_for timeout, so only
    # a generous window-to-tick ratio gives real safety (FAR-320). The
    # no-detector stall case is proven separately by
    # test_heartbeat_off_no_detector_stalls_end_to_end.
    wait_calls = {"n": 0}

    async def _wait():
        wait_calls["n"] += 1
        await asyncio.sleep(0.03)
        if wait_calls["n"] < 45:
            raise TimeoutError
        return cmd_result

    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=_wait)
    handle.kill = AsyncMock()

    watch_size = {"n": 0}

    def _get_info(path, **kwargs):
        if str(path).endswith("progress.log"):
            watch_size["n"] += 10
            return MagicMock(size=watch_size["n"])
        return MagicMock(size=0)

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return '{"summary": "done"}'
        return ""

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.get_info = AsyncMock(side_effect=_get_info)
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.list = AsyncMock(return_value=[])
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_TAIL_INTERVAL", 0.05),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    handle.kill.assert_not_awaited()


async def test_heartbeat_off_no_detector_stalls_end_to_end():
    """END-TO-END strict-mode companion: with enable_heartbeat=False and NO
    opt-in detector, a connected-but-silent agent MUST stall through the real
    make_sandbox_agent_fn wiring (not just a hand-built _StallDetector). The
    drain probe's successful get_info proves the sandbox is responsive, but
    with heartbeat disabled that touch is a no-op and the flat agent log never
    refreshes the output channel — so log-growth is provably the ONLY lifeline
    a heartbeat-off silent run has.

    ``handle.wait`` sleeps for the full tick interval before each timeout so a
    real stall window elapses and the watchdog genuinely fires.
    """
    node_def = _base_node_def(timeout_seconds=30, enable_heartbeat=False)
    fn = make_sandbox_agent_fn(node_def)

    async def _wait():
        await asyncio.sleep(0.04)
        raise TimeoutError

    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=_wait)
    handle.kill = AsyncMock()

    def _get_info(path, **kwargs):
        return MagicMock(size=0)

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return '{"summary": "done"}'
        return ""

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.get_info = AsyncMock(side_effect=_get_info)
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.list = AsyncMock(return_value=[])
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_TAIL_INTERVAL", 0.05),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_IDLE_TIMEOUT", 0.1),
        pytest.raises(SandboxNodeFailedError, match="no output"),
    ):
        await fn(_run_state())

    handle.kill.assert_awaited()


# ---------------------------------------------------------------------------
# FAR-306: enable_heartbeat=False is strict — a connected-but-silent agent stalls
# ---------------------------------------------------------------------------


async def test_heartbeat_disabled_silent_connected_agent_stalls():
    """With enable_heartbeat=False and no opt-in detector, a connected-but-silent
    agent MUST stall (strict mode). This drives the watchdog directly with a
    ``_StallDetector`` that has ONLY the ``output`` channel enabled (the exact
    configuration the node builds when heartbeat is opted out) — a connected but
    silent agent leaves ``output`` stale, so the watchdog fires."""
    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    handle.kill = AsyncMock()

    detector = _StallDetector()
    detector.enable("output")  # heartbeat intentionally NOT enabled (strict mode)

    result, stall_reason = await _wait_command_with_idle_watchdog(
        handle,
        total_timeout=30.0,
        idle_timeout=0.05,
        last_activity=detector.last_activity,
        on_tick=None,
        tick_interval=0.01,
    )
    assert result is None
    assert stall_reason is not None
    assert "no output" in stall_reason
    handle.kill.assert_awaited()


def test_heartbeat_disabled_but_agent_output_keeps_run_alive():
    """Even with enable_heartbeat=False (strict), ACTUAL agent-log growth keeps
    the run alive via the always-on ``output`` channel — a busy-but-silent agent
    (one that writes progress to its own log) must NOT be false-killed.

    Asserted via the detector's liveness semantics: repeated ``output`` touches
    keep ``last_activity()`` fresh, so the watchdog's stale-check never fires.
    """
    detector = _StallDetector()
    detector.enable("output")
    assert "heartbeat" not in detector.enabled  # strict mode

    for _ in range(5):
        detector.touch("output")

    # The output channel is continually refreshed -> last_activity tracks now,
    # never going stale.
    assert time.monotonic() - detector.last_activity() < 1.0


def test_heartbeat_enabled_default_silent_connected_agent_does_not_stall():
    """With the default enable_heartbeat=True, a connected-but-silent agent does
    NOT stall — the drain probe's successful get_info keeps the heartbeat fresh
    (the safe default that never false-kills).

    Asserted via detector semantics: with heartbeat enabled, refreshing it on a
    successful get_info (without any output growth) keeps ``last_activity()``
    fresh, so the watchdog never treats the run as stalled.
    """
    detector = _StallDetector()
    detector.enable("output")
    detector.enable("heartbeat")
    assert "heartbeat" in detector.enabled  # default enabled

    # Simulate many silent drain ticks that only refresh the heartbeat (get_info
    # success, no output growth).
    for _ in range(50):
        detector.touch("heartbeat")
        # last_activity() is the max across enabled channels and stays fresh.
        assert time.monotonic() - detector.last_activity() < 1.0


# ---------------------------------------------------------------------------
# FAR-194: `|tojson` on an undefined variable raises TypeError (not
# UndefinedError) — the template render handlers must skip, not crash the run.
# ---------------------------------------------------------------------------


async def test_prompt_tojson_on_undefined_skips():
    """A prompt template using `{{ missing | tojson }}` on an undefined variable
    raises TypeError inside Jinja's tojson filter (json.dumps on Undefined), NOT
    UndefinedError. It must be treated like the UndefinedError case: the node
    returns status 'skipped' and NO sandbox command executes."""
    node_def = _base_node_def(agent_prompt="Context: {{ missing | tojson }}")
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["status"] == "skipped"
    assert "prompt template" in result["summary"]
    sandbox.commands.run.assert_not_called()


async def test_agent_command_tojson_on_undefined_skips():
    """An agent_command template using `{{ missing | tojson }}` on an undefined
    variable raises TypeError, not UndefinedError. It must return status
    'skipped' and execute NO sandbox command, mirroring the prompt handling."""
    node_def = _model_node_def(
        "opencode run --model {{ missing | tojson }} --auto --format json < /home/user/prompt.md"
    )
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["status"] == "skipped"
    assert "agent_command" in result["summary"]
    sandbox.commands.run.assert_not_called()


async def test_context_scope_gates_sandbox_agent_render_view():
    """FAR-418 MAJOR-3 fix for the sandbox_agent path: ``capability_scope.context_scope``
    must bind the sandbox agent's render view, not just the plain ``agent`` node path.

    A ``sandbox_agent`` node whose template reads a gated ``run_context`` key (via
    ``{{ run_context.<key> }}`` or ``{{ state.run_context.<key> }}``) must NOT have
    that key appear in the rendered prompt written to ``/home/user/prompt.md`` — the
    same bypass class the make_node_fn fix closed, still open on this node type.
    """
    node_def = _base_node_def(
        agent_prompt=(
            "tier={{ run_context.model_tier }}|"
            "leak-rc={{ run_context.secret }}|"
            "leak-state={{ state.run_context.secret }}"
        ),
        capability_scope={"context_scope": ["model_tier"]},
    )
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(
            {
                "run_context": {"model_tier": "tier-2", "secret": "X", "input": {}},
                "_run_id": "run-1",
                "_pipeline_id": "pipe-1",
                "_org_id": _ORG_ID,
            }
        )

    assert result["output"]["status"] == "completed"

    # Pull the rendered prompt back out of the /home/user/prompt.md write.
    prompt_writes = [
        c.args[1] for c in sandbox.files.write.call_args_list if c.args and c.args[0] == "/home/user/prompt.md"
    ]
    assert prompt_writes, "rendered prompt was not written to /home/user/prompt.md"
    rendered = prompt_writes[0]

    # The scoped key is visible; the gated key must NOT leak into the prompt
    # (neither via the run_context var nor the state.run_context view).
    assert "tier=tier-2" in rendered
    assert "leak-rc=X" not in rendered
    assert "leak-state=X" not in rendered
