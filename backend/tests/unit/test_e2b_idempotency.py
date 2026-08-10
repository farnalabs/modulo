"""Unit tests for the DB-atomic E2B dispatch marker (dist/runtime-core A4).

The retired Redis SETNX E2B fence is replaced by a DB-atomic dispatch marker
on ``runs.sandbox_dispatch_state`` / ``runs.sandbox_id``:

  * ``_acquire_dispatch_marker`` runs ONE ``UPDATE ... WHERE claim_token=:tok
    AND status='running'`` IMMEDIATELY BEFORE ``AsyncSandbox.create`` — no
    read-then-create TOCTOU. A rowcount of 0 (superseded claim / run not
    running) raises :class:`SupersededNodeError` and NO sandbox is created.
  * After a successful create the real sandbox id is stored on the run row.
  * The marker is cleared in a finally, FENCED on the claim token (a superseded
    original cannot clear a successor's marker).

Fake-session based — no live Postgres required.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import SandboxNodeFailedError, SupersededNodeError, make_sandbox_agent_fn

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"


class _FakeResult:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _MarkerSession:
    """Fake session that records dispatch-marker UPDATEs in an event list."""

    def __init__(self, events: list[str], *, marker_row: tuple[object, ...] | None = ("id",)) -> None:
        self._events = events
        self._marker_row = marker_row

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> Any:
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        return bind

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _FakeResult:
        s = str(stmt)
        if "sandbox_dispatch_state" in s:
            if "sandbox_dispatch_state=NULL" in s:
                self._events.append("marker_clear")
            elif "sandbox_id=:sid" in s and params and params.get("sid") is None:
                self._events.append("marker_set")
            else:
                self._events.append("marker_store_id")
            return _FakeResult(self._marker_row)
        return _FakeResult(None)


def _make_factory(session: _MarkerSession):
    @asynccontextmanager
    async def _ctx():
        yield session

    return _ctx


def _base_node_def(**overrides: object) -> dict:
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
        "_claim_lease": "tok-executor-abc",
    }


def _sandbox_mock() -> MagicMock:
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""
    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)
    sandbox = MagicMock()
    sandbox.sandbox_id = "sbx-123"
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(return_value='{"summary": "done"}')
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


async def test_dispatch_marker_set_before_sandbox_create():
    """The DB-atomic marker UPDATE runs BEFORE AsyncSandbox.create (no TOCTOU)."""
    events: list[str] = []
    session = _MarkerSession(events)
    fn = make_sandbox_agent_fn(_base_node_def(), session_factory=_make_factory(session))
    sandbox = _sandbox_mock()

    def _record_create(*_a, **_kw):
        events.append("sandbox_create")
        return sandbox

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(side_effect=_record_create)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    assert events.index("marker_set") < events.index("sandbox_create")
    assert events.index("marker_store_id") > events.index("sandbox_create")
    # The marker is cleared in the finally.
    assert events[-1] == "marker_clear"


async def test_superseded_marker_deny_raises_before_create():
    """A marker UPDATE matching zero rows (superseded/not running) raises
    SupersededNodeError BEFORE any sandbox is created."""
    events: list[str] = []
    session = _MarkerSession(events, marker_row=None)
    fn = make_sandbox_agent_fn(_base_node_def(), session_factory=_make_factory(session))
    created: list[str] = []

    def _record_create(*_a, **_kw):
        created.append("created")
        return _sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(side_effect=_record_create)),
        pytest.raises(SupersededNodeError, match="marker denied"),
    ):
        await fn(_run_state())

    assert created == []
    assert "sandbox_create" not in events


async def test_marker_cleared_even_when_sandbox_create_fails():
    """A dispatch failure (create raises) still clears the marker in the finally."""
    events: list[str] = []
    session = _MarkerSession(events)
    fn = make_sandbox_agent_fn(_base_node_def(), session_factory=_make_factory(session))

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(side_effect=RuntimeError("sandbox provisioning failed"))):
        result = await fn(_run_state())

    # A create failure is a generic exception path — surfaces as a failed node
    # output (not a retryable/superseded class), and the marker is cleared.
    assert result["output"]["status"] == "failed"
    assert events[-1] == "marker_clear"


async def test_marker_skip_fail_open_without_session_factory():
    """No session factory -> the marker is skipped fail-open (no DB write, node
    proceeds) — matches the heartbeat claim fence as the primary guard."""
    fn = make_sandbox_agent_fn(_base_node_def(), session_factory=None)
    sandbox = _sandbox_mock()
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"


async def test_retryable_infra_failure_raises_sandbox_node_failed():
    """A command timeout is a retryable sandbox-infra failure — it RAISES
    SandboxNodeFailedError instead of returning a silent failed/wrong-success
    node output."""
    fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=None)
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.commands.run = AsyncMock(side_effect=TimeoutError("command timed out"))
    sandbox.files.read = AsyncMock(side_effect=TimeoutError("no output.json"))
    sandbox.kill = AsyncMock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError, match="no output"),
    ):
        await fn(_run_state())
    sandbox.kill.assert_awaited()
