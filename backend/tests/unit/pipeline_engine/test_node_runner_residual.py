"""Unit tests for the FAR-664 newline-safe loop-intercept bridge handoff.

When the loop-intercept bridge is active, the rendered agent command must be
handed to the bridge client via a FILE (/home/user/.modulo_bridge_cmd.sh) and
the wrapped command must invoke ``bash <file>`` after ``--`` — never the
command inline (the outer bash word-splits a multi-line inline command, so
only its first line would reach the bridge argv and post-heredoc statements
would escape guardrail interception).
"""

import logging
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.pipeline_engine.node_runner import (
    _SANDBOX_LOG_PATH,
    make_sandbox_agent_fn,
)

_BRIDGE_CMD_PATH = "/home/user/.modulo_bridge_cmd.sh"
_BRIDGE_HANDOFF_COMMAND = "python3 /home/user/modulo_bridge.py --wrap -- bash /home/user/.modulo_bridge_cmd.sh"
_SINGLE_LINE_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
_MULTI_LINE_COMMAND = (
    "cat > /home/user/agent_prompt.md <<'FAR664'\n"
    "heredoc line one\n"
    "heredoc line two\n"
    "FAR664\n"
    "opencode run --auto --format json < /home/user/agent_prompt.md"
)


def _run_state() -> dict[str, Any]:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": "org-1",
    }


def _sandbox_node_def(**overrides: Any) -> dict[str, Any]:
    node_def: dict[str, Any] = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": _SINGLE_LINE_COMMAND,
    }
    node_def.update(overrides)
    return node_def


def _make_sandbox_mock():
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    def _read(path: Any, format: str = "text", **kwargs: Any) -> Any:
        if str(path).endswith("output.json"):
            return '{"summary": "done"}'
        return ""

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


def _bridge_server_mock() -> MagicMock:
    server = MagicMock()
    server.start = AsyncMock(return_value=47591)
    server.close = AsyncMock()
    return server


def _bridge_patches(stack: ExitStack, sandbox: MagicMock, server: MagicMock) -> None:
    stack.enter_context(patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)))
    stack.enter_context(
        patch(
            "modulo.core.guardrails.loop_intercept.load_loop_intercept_guardrails",
            new=AsyncMock(return_value=[MagicMock()]),
        )
    )
    stack.enter_context(patch("modulo.core.guardrails.loop_intercept.LoopInterceptCallbackServer", return_value=server))


async def test_bridge_handoff_command_file_writes_single_line_command():
    """With the bridge active the agent command is handed off via the command
    file and the dispatched command is exactly the bash-file handoff form."""
    fn = make_sandbox_agent_fn(_sandbox_node_def(loop_intercept={"enabled": True, "latency_budget_ms": 100}))
    sandbox = _make_sandbox_mock()
    server = _bridge_server_mock()
    with ExitStack() as stack:
        _bridge_patches(stack, sandbox, server)
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    written = {call.args[0]: call.args[1] for call in sandbox.files.write.call_args_list}
    assert written[_BRIDGE_CMD_PATH] == _SINGLE_LINE_COMMAND
    expected_wrapped = f"(\n{_BRIDGE_HANDOFF_COMMAND}\n) > {_SANDBOX_LOG_PATH} 2>&1"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert wrapped == expected_wrapped
    server.close.assert_awaited_once()


async def test_bridge_handoff_command_file_holds_multiline_command_verbatim():
    """A multi-line agent command (heredoc, no trailing newline) is written to
    the command file byte-for-byte and never appears inline in the dispatched
    command — only ``bash <file>`` does."""
    node_def = _sandbox_node_def(
        agent_command=_MULTI_LINE_COMMAND,
        loop_intercept={"enabled": True, "latency_budget_ms": 100},
    )
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    server = _bridge_server_mock()
    with ExitStack() as stack:
        _bridge_patches(stack, sandbox, server)
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    written = {call.args[0]: call.args[1] for call in sandbox.files.write.call_args_list}
    assert written[_BRIDGE_CMD_PATH] == _MULTI_LINE_COMMAND
    expected_wrapped = f"(\n{_BRIDGE_HANDOFF_COMMAND}\n) > {_SANDBOX_LOG_PATH} 2>&1"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert wrapped == expected_wrapped
    assert _MULTI_LINE_COMMAND not in wrapped


async def test_bridge_cmd_file_write_failure_falls_back_to_plain_command(caplog):
    """A command-file write failure fails open: the bridge is disabled for the
    node, the failure is logged, and the plain agent command still dispatches."""
    fn = make_sandbox_agent_fn(_sandbox_node_def(loop_intercept={"enabled": True, "latency_budget_ms": 100}))
    sandbox = _make_sandbox_mock()

    async def _write(path: Any, content: Any, **kwargs: Any) -> None:
        if str(path) == _BRIDGE_CMD_PATH:
            raise RuntimeError("bridge cmd file upload failed")

    sandbox.files.write = AsyncMock(side_effect=_write)
    server = _bridge_server_mock()
    with ExitStack() as stack:
        _bridge_patches(stack, sandbox, server)
        stack.enter_context(caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"))
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    assert any("loop_intercept_setup_failed" in m for m in caplog.messages)
    expected_wrapped = f"(\n{_SINGLE_LINE_COMMAND}\n) > {_SANDBOX_LOG_PATH} 2>&1"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert wrapped == expected_wrapped
