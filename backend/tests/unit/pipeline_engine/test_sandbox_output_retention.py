"""Regression tests for FAR-188: raw sandbox output retention on parse failure.

When a sandbox_agent node's ``/home/user/output.json`` is missing, empty, or
malformed, the node raises ``SandboxNodeFailedError`` (retryable, A6) — but the
run record must STILL retain the RAW output (the file content, or the captured
stdout that carried it) so a ``pr_url`` the agent created inside the sandbox is
never lost when the JSON fails to parse (classification FAR-189 depends on it).

The invariant under test: a run that created a PR must never lose the evidence
of that PR when output.json fails to parse.
"""

import asyncio
import uuid
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError, make_sandbox_agent_fn

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"


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


def _make_sandbox_mock(*, output_json: str = '{"summary": "done"}', log_content: str = ""):
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return output_json
        return log_content

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=len(log_content)))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


class _FakeRunRow:
    """In-memory ``runs`` row capturing the persisted output columns.

    Mirrors the fence-test fake: the ORM ``select(Run)`` in the persist helper
    resolves against this object, and its ``outputs_json`` /
    ``node_telemetry_json`` attributes are the lockstep pair written by the
    node's raw-output retention path.
    """

    def __init__(self) -> None:
        self.outputs_json: dict | None = None
        self.node_telemetry_json: dict | None = None


class _RetentionResult:
    def __init__(self, row: _FakeRunRow | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> _FakeRunRow | None:
        return self._row


class _RetentionSession:
    """Fake async session serving the ORM ``select(Run)`` and ORM writes."""

    def __init__(self, row: _FakeRunRow) -> None:
        self._row = row

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> "_RetentionSession":
        return self

    async def execute(self, stmt: object, params: dict | None = None) -> _RetentionResult:
        if "FROM runs" in str(stmt):
            return _RetentionResult(self._row)
        return _RetentionResult(None)

    async def flush(self) -> None:
        return None


def _retention_env(output_json: str):
    """Return (node_fn, fake_row, sandbox) wired to an in-memory run row."""
    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def, session_factory=_factory)
    sandbox = _make_sandbox_mock(output_json=output_json)
    return fn, row, sandbox


async def test_malformed_output_json_retains_raw_output_and_pr_url():
    """A malformed output.json (read succeeds, json.loads fails) still leaves the
    RAW content — including an embedded pr_url — on the run record."""
    malformed = '{"summary": "PR created", "pr_url": "https://github.com/farnalabs/modulo/pull/123", '
    fn, row, sandbox = _retention_env(output_json=malformed)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert row.outputs_json is not None, "raw output must be retained on the run record"
    marker = row.outputs_json["n1"]
    assert marker["status"] == "failed"
    assert marker["parse_error"]
    assert "JSONDecodeError" in marker["parse_error"]
    assert "https://github.com/farnalabs/modulo/pull/123" in marker["raw_output"]
    assert marker["pr_url"] == "https://github.com/farnalabs/modulo/pull/123"
    # Lockstep: the telemetry column carries the same marker so the finalize
    # merge treats the row as already-pure and passes it through verbatim.
    assert row.node_telemetry_json is not None
    assert row.node_telemetry_json["n1"]["raw_output"] == marker["raw_output"]


async def test_bytes_output_json_that_fails_json_loads_retains_raw():
    """A read that returns bytes which fail json.loads is decoded and retained."""
    raw_bytes = b'{"pr_url": "https://github.com/farnalabs/modulo/pull/456" '
    fn, row, sandbox = _retention_env(output_json=raw_bytes)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    marker = row.outputs_json["n1"]
    assert isinstance(marker["raw_output"], str)
    assert "https://github.com/farnalabs/modulo/pull/456" in marker["raw_output"]
    assert marker["pr_url"] == "https://github.com/farnalabs/modulo/pull/456"


async def test_missing_output_json_falls_back_to_captured_stdout():
    """When the output.json read itself fails (file missing / unreadable), the
    captured stdout that carried the agent's result is retained instead."""
    sandbox = _make_sandbox_mock()

    # The final read of /home/user/output.json raises; the drain-log reads of
    # /home/user/agent.log still succeed so the watchdog stays alive.
    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            raise OSError("output.json missing")
        return ""

    sandbox.files.read = AsyncMock(side_effect=_read)
    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=_factory)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert row.outputs_json is not None
    marker = row.outputs_json["n1"]
    assert marker["status"] == "failed"
    assert marker["parse_error"]
    assert "OSError" in marker["parse_error"]
    # The stdout carried the raw content (here the fallback source).
    assert "agent stdout" in marker["raw_output"]


async def test_persist_failure_never_blocks_the_raise():
    """A failing DB write during retention must NOT block the SandboxNodeFailedError
    raise — the parse-failure path is best-effort and must not block terminalization."""
    _fn, row, sandbox = _retention_env(output_json='{"summary": "broken", ')

    class _BoomSession(_RetentionSession):
        async def execute(self, stmt: object, params: dict | None = None) -> _RetentionResult:
            raise RuntimeError("db down")

    def _boom_factory() -> _BoomSession:
        return _BoomSession(row)

    boom_fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=_boom_factory)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        pytest.raises(SandboxNodeFailedError, match=r"no parseable output\.json"),
    ):
        await boom_fn(_run_state())


async def test_stalled_command_retains_captured_stdout():
    """A command that stalls/times out (cmd_result None) never reads output.json,
    but the captured stdout is retained as the raw evidence — a pr_url echoed
    before the stall must survive (FAR-188 invariant)."""
    cmd_result = MagicMock()
    cmd_result.exit_code = -1

    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=asyncio.TimeoutError)

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            raise OSError("no output.json")
        return ""

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.get_info = AsyncMock(side_effect=OSError("sandbox connection dead"))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=_factory)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_IDLE_TIMEOUT", 1.0),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_TAIL_INTERVAL", 0.01),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    assert row.outputs_json is not None
    marker = row.outputs_json["n1"]
    assert marker["status"] == "failed"
    assert marker["parse_error"]


async def test_successful_parse_does_not_write_retention_marker():
    """A clean output.json parse does NOT write a raw-output marker — retention is
    only for the parse-failure path."""
    fn, row, sandbox = _retention_env(output_json='{"summary": "done", "pr_url": ""}')

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    assert row.outputs_json is None
