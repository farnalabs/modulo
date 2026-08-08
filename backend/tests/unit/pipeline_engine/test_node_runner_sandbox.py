"""Unit tests for make_sandbox_agent_fn command resolution.

A sandbox_agent node MUST provide agent_command (or agent_commands);
there is no default command, and a missing command is a hard error.
"""

import asyncio
import logging
import os
import urllib.request
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.event_broker import get_registry
from modulo.core.pipeline_engine.node_runner import (
    _E2B_SANDBOX_USD_PER_HOUR,
    _MAX_ARTIFACT_LOG,
    _compute_sandbox_cost,
    _fetch_sandbox_log_tail,
    make_sandbox_agent_fn,
    resolve_env_var_refs,
)

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"


def _make_sandbox_mock():
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.files.read = AsyncMock(return_value='{"summary": "done"}')
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


async def test_with_agent_command_returns_callable():
    """A node_def with agent_command resolves without raising and returns a callable."""
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": "opencode run --auto --format json < /home/user/prompt.md",
    }
    fn = make_sandbox_agent_fn(node_def)
    assert callable(fn)


async def test_with_agent_commands_returns_callable():
    """agent_commands list is joined and resolved without raising."""
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_commands": ["echo start", "opencode run"],
    }
    fn = make_sandbox_agent_fn(node_def)
    assert callable(fn)


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
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.files.read = AsyncMock(return_value='{"summary": "done", "cost_estimate_usd": 0.001}')
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
# {{ secrets.KEY }} env var resolution (org vault -> host env fallback)
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


async def test_env_var_secret_ref_missing_resolves_to_empty_string(caplog):
    """No session_factory and no host env value -> '' plus a warning (legacy)."""
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
    assert envs["FOO"] == ""
    assert any("env_var.secret_ref_not_found" in m for m in caplog.messages)


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
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["FOO"] == "vault-secret"
    backend.get_secret.assert_awaited_once_with("FOO")


async def test_env_var_secret_ref_falls_back_to_host_env():
    """Vault raises KeyError but the host env has FOO -> host value wins."""
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
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=backend),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="test-key")),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch.dict(os.environ, {"FOO": "host-value"}),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["FOO"] == "host-value"


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
    assert resolved == {"A": "a-secret", "B": "plain", "C": ""}


async def test_sandbox_agent_command_timeout_surfaces_clear_summary():
    """A timed-out command (cmd_result None, exit_code -1, EMPTY stdout/stderr)
    must surface a clear explanation in the summary — not a silent empty-summary
    failure (the Branch Fixer empty-agent-output hang of 2026-08-05)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=TimeoutError("command timed out"))
    sandbox.files.read = AsyncMock(side_effect=TimeoutError("no output.json"))
    sandbox.kill = AsyncMock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    output = result["output"]
    artifact = result["artifacts"][0]["output"]
    assert output["status"] == "failed"
    assert artifact["exit_code"] == -1
    assert output["agent_stdout"] == ""
    assert output["agent_stderr"] == ""
    assert "no output" in output["summary"]
    assert "30s" in output["summary"]
    assert artifact["summary"] == output["summary"]


# ---------------------------------------------------------------------------
# FAR-97: E2B idle watchdog + kill-before-output-read
# ---------------------------------------------------------------------------


async def test_idle_watchdog_kills_stalled_command_and_fails():
    """A command that goes silent for _SANDBOX_IDLE_TIMEOUT is killed and the
    node fails fast — it does not block for the full sandbox_timeout (FAR-97)."""
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
    ):
        result = await fn(_run_state())

    output = result["output"]
    artifact = result["artifacts"][0]["output"]
    assert output["status"] == "failed"
    assert artifact["exit_code"] == -1
    assert "no output" in output["summary"]
    # The stalled command itself was killed...
    handle.kill.assert_awaited()
    # ...and the still-running sandbox was killed before output.json could be
    # read — the interrupted process must not fabricate a completion.
    sandbox.kill.assert_awaited()
    sandbox.files.read.assert_not_called()


async def test_timed_out_command_does_not_read_output_json():
    """On a timed-out command the sandbox is killed and output.json is NOT read —
    an interrupted-but-alive agent could otherwise fabricate a completion (FAR-97)."""
    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=TimeoutError("command timed out"))
    sandbox.files.read = AsyncMock(return_value='{"summary": "fabricated"}')
    sandbox.kill = AsyncMock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    output = result["output"]
    artifact = result["artifacts"][0]["output"]
    assert output["status"] == "failed"
    assert artifact["exit_code"] == -1
    assert "no output" in output["summary"]
    assert "30s" in output["summary"]
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
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.files.read = AsyncMock(return_value='{"summary": "done"}')
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
    ):
        result = await fn(_run_state())

    output = result["output"]
    artifact = result["artifacts"][0]["output"]
    assert output["status"] == "failed"
    assert output["sandbox_id"] == "sbx-dead"
    assert output["sandbox_log_tail"] == "sample log line"
    assert artifact["sandbox_id"] == "sbx-dead"
    assert artifact["sandbox_log_tail"] == "sample log line"
    # The tail fetch precedes the kill so the still-live sandbox serves its logs.
    assert events[0] == "fetch"
    assert events.index("fetch") < events.index("kill")
    sandbox.kill.assert_awaited()


async def test_fetch_sandbox_log_tail_returns_empty_without_api_key(monkeypatch):
    """No E2B key configured -> helper returns '' without attempting a fetch."""
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    with patch("urllib.request.urlopen") as _urlopen:
        assert await _fetch_sandbox_log_tail("sbx-nokey") == ""
    _urlopen.assert_not_called()


async def test_fetch_sandbox_log_tail_never_raises_on_network_failure(monkeypatch):
    """A failing urlopen (no network / non-2xx / garbage) is swallowed -> ''."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert await _fetch_sandbox_log_tail("sbx-netfail") == ""
    with patch("urllib.request.urlopen", side_effect=urllib.request.HTTPError("url", 401, "unauthorized", None, None)):
        assert await _fetch_sandbox_log_tail("sbx-netfail") == ""


async def test_fetch_sandbox_log_tail_returns_empty_for_invalid_id(monkeypatch):
    """A non-string/None sandbox id never triggers a network call."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-key")
    with patch("urllib.request.urlopen") as _urlopen:
        assert await _fetch_sandbox_log_tail(None) == ""
    _urlopen.assert_not_called()
