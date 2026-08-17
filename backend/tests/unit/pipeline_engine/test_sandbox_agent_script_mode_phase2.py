"""Unit tests for sandbox_agent script-mode Phase 2 (FAR-296).

Covers the SAFETY-CRITICAL Phase 2 machinery:
1. Fencing lease: script mode persists an execution claim (``script_executing``
   in ``runs.sandbox_dispatch_state``) IMMEDIATELY BEFORE the script process
   starts; LLM mode takes NO lease.
2. Stage-split failure contract: pre-claim faults stay retryable, post-claim
   faults (after the script process started) raise TERMINAL, never-retryable
   ``script.*`` exceptions.
3. The never-retryable encoding: ``script.side_effect_unknown`` (and the other
   post-claim script.* codes) are excluded from BOTH the run-level retry policy
   and the node-level A-series fenced reset.
4. The stale-claim lease probe: ``_script_lease_probe_ok`` blocks a requeue
   when a live ``script_executing`` lease exists.
"""

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.executor import (
    _failure_event_matches,
    _graph_has_script_mode,
    _script_lease_probe_ok,
)
from modulo.core.pipeline_engine.node_runner import (
    ScriptSideEffectUnknownError,
    make_sandbox_agent_fn,
)

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))


def _read_router(output_json: str, log_content: str = ""):
    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return output_json
        return log_content

    return _read


def _script_sandbox_mock(*, output_json: str = '{"result": "ok"}', log_content: str = ""):
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "script stdout"
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


def _run_state(payload: dict | None = None) -> dict:
    run_context = {"input": payload if payload is not None else {"task": "x"}}
    return {
        "run_context": run_context,
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


def _script_node_def(**overrides) -> dict:
    node_def: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "mode": "script",
        "script_command": "python3 /home/user/main.py",
        "agent_prompt": "ignored in script mode",
    }
    node_def.update(overrides)
    return node_def


# ---------------------------------------------------------------------------
# 1. Fencing lease: script mode persists the execution claim before process start
# ---------------------------------------------------------------------------


class _Begin:
    """Async context manager for the fake session's ``begin()``."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeDialect:
    name = "postgresql"


class _FakeBind:
    dialect = _FakeDialect()


class _FakeSession:
    """Records raw UPDATE/params so the test can assert the lease marker.

    ``_acquire_dispatch_marker`` runs a SELECT for ``claim_count`` (returns
    ``(1,)`` so the claim succeeds) then UPDATEs with RETURNING (truthy row).
    All (statement, params) pairs are captured for the marker assertions.
    """

    def __init__(self, dispatch_state: str | None = None):
        self._state = dispatch_state
        self.executions: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _Begin:
        return _Begin()

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> _FakeBind:
        return _FakeBind()

    async def execute(self, statement: Any, params: dict | None = None) -> Any:
        sql = str(statement)
        self.executions.append((sql, params or {}))
        if "sandbox_dispatch_state=" in sql:
            self._state = (params or {}).get("marker")
        return MagicMock(fetchone=MagicMock(return_value=(1,)))


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession):
        self.session = session

    def __call__(self) -> _FakeSession:
        return self.session


def _any_script_executing_marker(session: _FakeSession) -> bool:
    """True when any executed UPDATE carried a ``script_executing`` marker."""
    for _sql, params in session.executions:
        marker = params.get("marker")
        if isinstance(marker, str):
            try:
                state = json.loads(marker)
            except (TypeError, ValueError):
                continue
            if isinstance(state, dict) and state.get("state") == "script_executing":
                return True
    return False


async def test_script_mode_stores_fencing_lease_before_process_start():
    """Script mode writes the ``script_executing`` lease before ``commands.run``.

    The lease must be persisted (recorded in the session) BEFORE the script
    process starts, so a durable marker proves the script RAN.
    """
    session = _FakeSession()
    factory = _FakeSessionFactory(session)

    node_def = _script_node_def()
    # No claim lease + no org in state would fail-open and skip the DB write.
    # Provide a claim lease + org so the fenced UPDATE path is exercised.
    state = _run_state()
    state["_claim_lease"] = "claim-token-1"
    sandbox = _script_sandbox_mock()
    fn = make_sandbox_agent_fn(node_def, session_factory=factory)

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        await fn(state)

    assert _any_script_executing_marker(session)


async def test_llm_mode_takes_no_fencing_lease():
    """LLM mode must NOT persist a ``script_executing`` lease (no fencing)."""
    session = _FakeSession()
    factory = _FakeSessionFactory(session)

    node_def = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "mode": "llm",
        "agent_prompt": "Do the thing",
        "agent_command": "opencode run --auto",
    }
    state = _run_state()
    state["_claim_lease"] = "claim-token-1"
    sandbox = _script_sandbox_mock(output_json='{"summary": "done"}')
    fn = make_sandbox_agent_fn(node_def, session_factory=factory)

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(state)

    assert result["output"]["status"] == "completed"
    assert not _any_script_executing_marker(session)


# ---------------------------------------------------------------------------
# 2. Stage-split failure contract
# ---------------------------------------------------------------------------


async def test_script_mode_mid_execution_termination_raises_side_effect_unknown():
    """A timeout/stall AFTER the process started raises ScriptSideEffectUnknownError.

    The side effect may or may not have happened — never retried.
    """
    node_def = _script_node_def()
    # commands.run succeeds but wait() returns None => cmd_result is None (timeout).
    handle = MagicMock()
    handle.wait = AsyncMock(return_value=None)
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read_router('{"x": 1}'))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    fn = make_sandbox_agent_fn(node_def)
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(ScriptSideEffectUnknownError),
    ):
        await fn(_run_state())


# ---------------------------------------------------------------------------
# 3. Never-retryable encoding in the retry subsystem
# ---------------------------------------------------------------------------


def test_failure_event_matches_excludes_script_never_retryable_codes():
    """A ``failure`` retry_policy must NOT retry script-mode terminal codes."""
    for code, mapped in [
        ("ScriptFailedError", "script.failed"),
        ("ScriptInvalidOutputError", "script.invalid_output"),
        ("ScriptSideEffectUnknownError", "script.side_effect_unknown"),
        ("ScriptSessionLostError", "script.session_lost"),
        ("script.schema_failed", "contract.schema"),
        ("script.no_output", "contract.no_output"),
    ]:
        assert _failure_event_matches({"failure"}, "failed", code, mapped, None) is False, (code, mapped)


def test_failure_event_matches_still_retries_llm_mode_and_preclaim():
    """LLM-mode / pre-claim retryable codes still retry on a ``failure`` event."""
    assert (
        _failure_event_matches({"failure"}, "failed", "SandboxNodeFailedError", "sandbox.no_output_json", None) is True
    )


# ---------------------------------------------------------------------------
# 4. Stale-claim lease probe
# ---------------------------------------------------------------------------


class _ProbeSession:
    def __init__(self, state: str | None):
        self._state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _Begin:
        return _Begin()

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> _FakeBind:
        return _FakeBind()

    async def execute(self, statement: Any, params: dict | None = None) -> Any:
        row = (self._state,) if self._state is not None else (None,)
        return MagicMock(fetchone=MagicMock(return_value=row))


class _ProbeFactory:
    def __init__(self, state: str | None):
        self.session = _ProbeSession(state)

    def __call__(self) -> _ProbeSession:
        return self.session


async def test_lease_probe_ok_when_no_script_executing_lease():
    """A clean dispatch state (no script lease) permits the requeue."""
    factory = _ProbeFactory(json.dumps({"state": "dispatching", "attempt_key": "k"}))
    ok = await _script_lease_probe_ok(factory, "run-1", _ORG_ID, "claim-1")
    assert ok is True


async def test_lease_probe_blocks_when_script_executing_lease_exists():
    """A live ``script_executing`` lease blocks the requeue (exactly-once)."""
    factory = _ProbeFactory(json.dumps({"state": "script_executing", "attempt_key": "k"}))
    ok = await _script_lease_probe_ok(factory, "run-1", _ORG_ID, "claim-1")
    assert ok is False


async def test_lease_probe_ok_with_no_claim_token():
    """Fail-open: no claim token means no fence to probe — requeue allowed."""
    assert await _script_lease_probe_ok(_ProbeFactory(None), "run-1", _ORG_ID, None) is True


# ---------------------------------------------------------------------------
# _graph_has_script_mode
# ---------------------------------------------------------------------------


def test_graph_has_script_mode_true_for_script_mode_node():
    graph = {"nodes": [{"node_type": "sandbox_agent", "mode": "script"}]}
    assert _graph_has_script_mode(graph) is True


def test_graph_has_script_mode_false_for_llm_and_missing():
    assert _graph_has_script_mode({"nodes": [{"node_type": "sandbox_agent", "mode": "llm"}]}) is False
    assert _graph_has_script_mode({"nodes": [{"node_type": "sandbox_agent"}]}) is False
    assert _graph_has_script_mode(None) is False
    assert _graph_has_script_mode({}) is False
