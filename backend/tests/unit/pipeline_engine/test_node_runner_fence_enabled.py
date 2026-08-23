"""Unit tests for the DB-atomic sandbox dispatch marker (dist/runtime-core A4).

The sibling ``test_node_runner_sandbox.py`` exercises the sandbox_agent node
with NO ``session_factory`` — the marker's ``_acquire_dispatch_marker`` fails
OPEN (returns True without writing) so the E2B idempotency fence never runs.
THIS file passes a fake session so the fence is ACTIVE:

* A pre-seeded marker for a DIFFERENT claim token (or a non-running row) makes
  ``_acquire_dispatch_marker`` return rowcount 0 — the node raises
  :class:`SupersededNodeError` and ``AsyncSandbox.create`` is NEVER awaited
  (a superseded original must not provision a second sandbox).
* A dispatch failure (create raises) still clears the marker in ``finally`` so
  a successor can acquire the dispatch slot.
* A successful dispatch stores the real sandbox id on the marker and clears it
  at teardown.

The marker SQL runs against an in-memory fake "runs row" (a mutable dict)
shared across the acquire / store / clear UPDATEs within one node invocation.
"""

from __future__ import annotations

import json
import uuid
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import SupersededNodeError, make_sandbox_agent_fn

_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))


def _read_router(output_json: str, log_content: str = ""):
    """Route sandbox.files.read by path: output.json vs the redirected agent log."""

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return output_json
        return log_content

    return _read


def _make_sandbox_mock(*, sandbox_id: str = "sbx-1", output_json: str = '{"summary": "done"}'):
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.sandbox_id = sandbox_id
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read_router(output_json))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


class _MarkerResult:
    def __init__(self, row: object | None = None) -> None:
        self._row = row

    def fetchone(self) -> object | None:
        return self._row


class _MarkerSession:
    """Fake async session that applies the three marker UPDATEs to an in-memory
    ``runs`` row (a mutable dict shared across the node invocation)."""

    def __init__(self, row: dict, executed: list[str]) -> None:
        self._row = row
        self._executed = executed

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: object, params: dict | None = None) -> _MarkerResult:
        sql = str(stmt)
        self._executed.append(sql)
        params = params or {}
        if "claim_count FROM runs" in sql:
            # D5 attempt-key read: fenced on token + status, returns the row's
            # claim_count so the acquire builds run:..:node:..:{claim_count}.
            if params.get("tok") == self._row.get("claim_token") and self._row.get("status") == "running":
                return _MarkerResult((self._row.get("claim_count", 0),))
            return _MarkerResult(None)
        if "RETURNING id" in sql:
            # Acquire: UPDATE ... AND claim_token=:tok AND status='running' RETURNING id.
            if params.get("tok") == self._row.get("claim_token") and self._row.get("status") == "running":
                self._row["sandbox_dispatch_state"] = params.get("marker")
                self._row["sandbox_id"] = None
                return _MarkerResult(("id",))
            return _MarkerResult(None)
        if "sandbox_id=NULL" in sql:
            # Clear: fenced on claim token only (no status guard, no RETURNING).
            if params.get("tok") == self._row.get("claim_token"):
                self._row["sandbox_dispatch_state"] = None
                self._row["sandbox_id"] = None
            return _MarkerResult(None)
        # Store the real sandbox id after a successful create.
        if params.get("tok") == self._row.get("claim_token") and self._row.get("status") == "running":
            self._row["sandbox_dispatch_state"] = params.get("marker")
            self._row["sandbox_id"] = params.get("sid")
        return _MarkerResult(None)


def _make_fence_env(
    *,
    row: dict,
    sandbox_factory=AsyncMock,
    sandbox: MagicMock | None = None,
    create_side_effect: Exception | None = None,
):
    """Return (node_fn, shared_row, executed, sandbox_mock).

    ``sandbox_factory`` is an AsyncMock callable standing in for
    ``AsyncSandbox.create``; ``create_side_effect`` makes it raise.
    """
    executed: list[str] = []
    state_row: dict = dict(row)

    def _factory() -> _MarkerSession:
        return _MarkerSession(state_row, executed)

    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": _AGENT_COMMAND,
        "timeout_seconds": 30,
    }
    node_fn = make_sandbox_agent_fn(node_def, session_factory=_factory)

    create_mock = sandbox_factory()
    if sandbox is not None:
        create_mock = sandbox_factory(return_value=sandbox)
    if create_side_effect is not None:
        create_mock = sandbox_factory(side_effect=create_side_effect)
    return node_fn, state_row, executed, create_mock


def _run_state(*, claim_lease: str) -> dict:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
        "_claim_lease": claim_lease,
    }


def _assert_marker_acquire_executed(executed: list[str]) -> None:
    # The acquire is the fenced claim_count read (D5) — it runs on EVERY DB path,
    # including the denied case where the UPDATE (only fired on a matching row)
    # is never reached.
    assert any("claim_count FROM runs" in s and "claim_token=:tok" in s for s in executed)


async def _run_fence(node_fn, create_mock) -> dict:
    """Run a fence-enabled node under the standard patch context."""
    with (
        patch("e2b.AsyncSandbox.create", new=create_mock),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch("modulo.db.rls.set_rls_execution_context", new=AsyncMock()),
    ):
        return await node_fn(_run_state(claim_lease="tok-owner"))


def _row(*, claim_token: str = "tok-owner", status: str = "running", claim_count: int = 0) -> dict:
    return {
        "claim_token": claim_token,
        "status": status,
        "sandbox_dispatch_state": None,
        "sandbox_id": None,
        "claim_count": claim_count,
    }


async def test_pre_seeded_marker_for_different_claim_denies_and_never_creates():
    """A dispatch marker pre-seeded by a successor (different claim token) makes
    the acquire UPDATE match zero rows -> SupersededNodeError; AsyncSandbox.create
    is NEVER awaited (no second sandbox for a superseded original)."""
    row = _row(claim_token="tok-successor")
    node_fn, _state, executed, create_mock = _make_fence_env(row=row)

    with (
        patch("e2b.AsyncSandbox.create", new=create_mock),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch("modulo.db.rls.set_rls_execution_context", new=AsyncMock()),
        pytest.raises(SupersededNodeError) as excinfo,
    ):
        await node_fn(_run_state(claim_lease="tok-owner"))

    assert "dispatch marker denied" in str(excinfo.value)
    create_mock.assert_not_awaited()
    _assert_marker_acquire_executed(executed)


async def test_same_claim_transient_denial_is_also_denied():
    """A same-token acquire still fails when the row is NOT running (e.g. the
    run was already terminalised between claim and dispatch) -> denied, no
    second sandbox."""
    row = _row(status="complete")
    node_fn, _state, executed, create_mock = _make_fence_env(row=row)

    with (
        patch("e2b.AsyncSandbox.create", new=create_mock),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch("modulo.db.rls.set_rls_execution_context", new=AsyncMock()),
        pytest.raises(SupersededNodeError) as excinfo,
    ):
        await node_fn(_run_state(claim_lease="tok-owner"))

    assert "dispatch marker denied" in str(excinfo.value)
    create_mock.assert_not_awaited()
    _assert_marker_acquire_executed(executed)


async def test_dispatch_failure_clears_marker_so_successor_can_acquire():
    """When AsyncSandbox.create raises, the marker acquired earlier MUST be
    cleared in the finally block — otherwise the run is left ``dispatching``
    forever and a successor can never acquire the dispatch slot."""
    row = _row()
    node_fn, state, executed, create_mock = _make_fence_env(row=row, create_side_effect=RuntimeError("e2b down"))

    result = await _run_fence(node_fn, create_mock)

    assert result["output"]["status"] == "failed"
    # Teardown cleared the marker for the owning token.
    assert state["sandbox_dispatch_state"] is None
    assert state["sandbox_id"] is None

    # A successor (same token still owns the row) can now acquire the slot.
    session = _MarkerSession(state, executed)
    acquire = await session.execute(
        "UPDATE runs SET sandbox_dispatch_state='dispatching', sandbox_id=:sid "
        "WHERE id=:rid AND organisation_id=:oid AND claim_token=:tok AND status='running' RETURNING id",
        {"tok": "tok-owner", "sid": None},
    )
    assert acquire.fetchone() == ("id",)


async def test_success_marker_holds_sandbox_id_then_cleared_at_teardown():
    """On success the marker carries the real sandbox id while the agent runs
    (so the heartbeat-lost path can kill it by id), and the finally block
    clears the marker when the node completes."""
    row = _row()
    sandbox = _make_sandbox_mock(sandbox_id="sbx-live")
    state_at_run: dict = {}
    handle = sandbox.commands.run.return_value

    async def _capture_state(*_args, **_kwargs):
        state_at_run.update(dict(state))
        return handle

    sandbox.commands.run.side_effect = _capture_state

    node_fn, state, _executed, create_mock = _make_fence_env(row=row, sandbox=sandbox)

    result = await _run_fence(node_fn, create_mock)

    assert result["output"]["status"] == "completed"
    # While the agent command ran the marker carried the real sandbox id AND the
    # structured attempt-key marker (D5).
    assert json.loads(state_at_run.get("sandbox_dispatch_state"))["state"] == "dispatching"
    assert state_at_run.get("sandbox_id") == "sbx-live"
    # Teardown cleared the marker.
    assert state["sandbox_dispatch_state"] is None
    assert state["sandbox_id"] is None
    sandbox.kill.assert_awaited()


async def test_reclaimed_run_produces_different_attempt_key():
    """A run re-claimed (claim_count rotates) re-runs node N with a DIFFERENT
    attempt key — the successor's re-run is distinguishable from the superseded
    original's attempt (D5 observability gap)."""
    keys: list[str] = []
    for claim_count in (1, 2):
        row = _row(claim_count=claim_count)
        node_fn, _state, _executed, create_mock = _make_fence_env(
            row=row, sandbox=_make_sandbox_mock(sandbox_id=f"sbx-{claim_count}")
        )
        result = await _run_fence(node_fn, create_mock)
        keys.append(result["output"]["attempt_key"])

    assert keys[0] == "run:run-1:node:n1:1"
    assert keys[1] == "run:run-1:node:n1:2"
    assert keys[0] != keys[1]


async def test_same_claim_attempt_key_stable_across_invocations():
    """Within one claim (same claim_count) the attempt key is STABLE — two
    dispatches of the same node under the same claim share one attempt key."""
    keys: list[str] = []
    for _ in range(2):
        row = _row(claim_count=4)
        node_fn, _state, _executed, create_mock = _make_fence_env(
            row=row, sandbox=_make_sandbox_mock(sandbox_id="sbx-x")
        )
        result = await _run_fence(node_fn, create_mock)
        keys.append(result["output"]["attempt_key"])

    assert keys[0] == "run:run-1:node:n1:4"
    assert keys[1] == "run:run-1:node:n1:4"


async def test_marker_carries_attempt_key():
    """The structured dispatch marker persisted on the run row carries the
    per-claim attempt key while the sandbox runs (D5)."""
    row = _row(claim_count=7)
    sandbox = _make_sandbox_mock(sandbox_id="sbx-7")
    state_at_run: dict = {}
    handle = sandbox.commands.run.return_value

    async def _capture_state(*_args, **_kwargs):
        state_at_run.update(dict(state))
        return handle

    sandbox.commands.run.side_effect = _capture_state

    node_fn, state, _executed, create_mock = _make_fence_env(row=row, sandbox=sandbox)

    result = await _run_fence(node_fn, create_mock)

    assert result["output"]["attempt_key"] == "run:run-1:node:n1:7"
    assert result["artifacts"][0]["output"]["attempt_key"] == "run:run-1:node:n1:7"
    marker = json.loads(state_at_run["sandbox_dispatch_state"])
    assert marker["state"] == "dispatching"
    assert marker["attempt_key"] == "run:run-1:node:n1:7"
    assert state_at_run["sandbox_id"] == "sbx-7"
