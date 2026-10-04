"""FAR-1470: sandbox agent commands dispatch under ``bash``, not ``sh``.

The PR Reviewer outage (2026-10-03): ``agent_commands`` beginning with
``set -euo pipefail`` aborted instantly because ``node_runner`` handed the
command to the runtime provider as ``["sh", "-c", ...]`` and ``sh`` on the
sandbox image is dash, which has no ``pipefail`` (``sh: 2: set: Illegal
option -o pipefail``). The dispatch now runs the command under ``bash`` — a
superset of sh for these scripts.

Regression coverage (each assertion FAILS on the pre-fix code):

1. the agent-command stream dispatch hands the provider
   ``["bash", "-c", <log-redirect wrap>]``, wrap payload unchanged;
2. the mediated ``commands.run`` helper path hands the provider
   ``["bash", "-c", ...]`` too — both ``node_runner`` dispatch sites stay
   consistent.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import _ProviderCommands, make_sandbox_agent_fn
from tests.unit.pipeline_engine.conftest import FakeDispatchProvider, FakeFileIOProvider, install_fake_dispatch

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
# The FAR-651 newline-safe log-redirect wrap the dispatch applies (byte-for-byte
# today's wrap — the FAR-1470 fix must change ONLY the shell, never the payload).
_EXPECTED_WRAPPED = f"(\n{_AGENT_COMMAND}\n) > /home/user/agent.log 2>&1"
_COMPLETED_OUTPUT = b'{"status": "completed", "summary": "done"}'


def _node_def() -> dict[str, Any]:
    return {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_commands": [_AGENT_COMMAND],
        "timeout_seconds": 30,
    }


def _run_state() -> dict[str, Any]:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-far1470",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


async def test_agent_command_dispatched_under_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent command reaches the provider as ``["bash", "-c", <wrap>]``.

    Full ``make_sandbox_agent_fn`` dispatch through a fake provider (the same
    harness ``test_e2b_dispatch_rewire_r4`` uses), so the assertion observes
    the REAL argv built at the ``exec_command_stream`` call site — not a
    re-statement of it.
    """
    # Log-tail seam: keep the dispatch off the real urllib tail probe.
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_log_tail_provider",
        AsyncMock(return_value=MagicMock(read_log_tail=AsyncMock(return_value=b"far1470-tail"))),
    )
    # File-I/O seam: serve output.json so the run completes cleanly.
    file_io = FakeFileIOProvider(files={"/home/user/output.json": _COMPLETED_OUTPUT})
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_file_io_provider",
        AsyncMock(return_value=file_io),
    )
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-far1470", exit_code=0)

    fn = make_sandbox_agent_fn(_node_def())
    with patch(
        "e2b.AsyncSandbox.create",
        new=AsyncMock(side_effect=AssertionError("the legacy AsyncSandbox.create must not run")),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    assert dispatch.last_command is not None
    # FAR-1470: bash, not sh (dash has no `pipefail`).
    assert dispatch.last_command[:2] == ["bash", "-c"], dispatch.last_command
    # One-token change only: the wrapped payload is byte-for-byte today's wrap.
    assert dispatch.last_command[2] == _EXPECTED_WRAPPED


async def test_provider_commands_helper_dispatched_under_bash() -> None:
    """The mediated ``commands.run`` helper path also dispatches under bash.

    ``_ProviderCommands.run`` is the ``sandbox.commands.run`` stand-in the
    workspace-input / drift-probe helpers call; FAR-1470 keeps BOTH
    ``node_runner`` dispatch sites on the same shell. bash is a superset of sh
    for the POSIX helper scripts, so behaviour is unchanged.
    """
    provider = FakeDispatchProvider(exit_code=0)
    commands = _ProviderCommands(provider, "sbx-far1470")

    result = await commands.run("set -euo pipefail; echo ok")

    assert provider.last_command == ["bash", "-c", "set -euo pipefail; echo ok"]
    assert result.exit_code == 0
