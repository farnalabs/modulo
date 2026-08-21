"""Unit tests for agent status/outcome propagation (agent-failure UX, phase 1).

The sandbox-agent node output contract surfaces the agent's RAW verdict from
``/home/user/output.json`` VERBATIM as ``agent_status`` / ``agent_outcome`` on
both the artifact output dict and the top-level run output dict. It must never
be derived from the exit code — a legacy ``{"status": "failed"}`` written by a
driver that exited 0 must still surface as ``agent_status=failed`` (that is the
A1 elevation compatibility path).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError,
    _is_sandbox_session_lost_echo,
    make_sandbox_agent_fn,
)

_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"


def _read_router(output_json: str, log_content: str = ""):
    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return output_json
        return log_content

    return _read


def _make_sandbox_mock(*, exit_code: int = 0, output_json: str = '{"summary": "done"}'):
    cmd_result = MagicMock()
    cmd_result.exit_code = exit_code
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read_router(output_json))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


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
        "_org_id": "org-1",
    }


async def _run_node(output_json: str, *, exit_code: int = 0) -> dict:
    fn = make_sandbox_agent_fn(_base_node_def())
    with patch(
        "e2b.AsyncSandbox.create",
        new=AsyncMock(return_value=_make_sandbox_mock(exit_code=exit_code, output_json=output_json)),
    ):
        return await fn(_run_state())


async def test_agent_status_and_outcome_surface_verbatim_in_both_outputs():
    """The agent's raw status/outcome appear on BOTH output dicts, unchanged."""
    result = await _run_node('{"status": "completed", "outcome": "success", "summary": "groomed"}')

    node_output = result["output"]
    artifact_output = result["artifacts"][0]["output"]

    assert node_output["agent_status"] == "completed"
    assert node_output["agent_outcome"] == "success"
    assert artifact_output["agent_status"] == "completed"
    assert artifact_output["agent_outcome"] == "success"


async def test_legacy_status_failed_surfaces_as_agent_status_failed():
    """Legacy drivers write {"status": "failed"} with exit 0 — the raw verdict
    must surface verbatim as agent_status=failed (the A1 compatibility path),
    while the exit-code-derived node status stays 'completed'."""
    result = await _run_node('{"status": "failed", "summary": "all sub-calls timed out"}', exit_code=0)

    node_output = result["output"]
    artifact_output = result["artifacts"][0]["output"]

    assert node_output["agent_status"] == "failed"
    assert artifact_output["agent_status"] == "failed"
    # Node status remains harness truth from the exit code — NOT overwritten.
    assert node_output["status"] == "completed"
    assert artifact_output["status"] == "completed"


async def test_exit_code_derivation_unchanged():
    """A non-zero exit still marks the node failed even when the agent
    self-reported completed — the exit code is harness truth for node status."""
    result = await _run_node('{"status": "completed", "outcome": "success", "summary": "ok"}', exit_code=1)

    node_output = result["output"]
    artifact_output = result["artifacts"][0]["output"]

    assert node_output["status"] == "failed"
    assert artifact_output["status"] == "failed"
    # The agent's self-report is still surfaced verbatim.
    assert node_output["agent_status"] == "completed"


async def test_non_string_agent_verdicts_do_not_surface():
    """Non-string status/outcome values (booleans, numbers) degrade to None —
    a garbage verdict must never be read as a false failure or success."""
    result = await _run_node('{"status": true, "outcome": 0, "summary": "ok"}')

    node_output = result["output"]
    artifact_output = result["artifacts"][0]["output"]

    assert node_output["agent_status"] is None
    assert node_output["agent_outcome"] is None
    assert artifact_output["agent_status"] is None
    assert artifact_output["agent_outcome"] is None


async def test_missing_output_json_fails_open_with_no_silent_agent_status():
    """A missing output.json is a retryable sandbox failure BEFORE the output
    path — the node never returns, so no agent_status is ever fabricated (a
    missing verdict can never look like a false complete)."""
    fn = make_sandbox_agent_fn(_base_node_def())
    with (
        patch(
            "e2b.AsyncSandbox.create",
            new=AsyncMock(return_value=_make_sandbox_mock(exit_code=0, output_json="")),
        ),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())


async def test_outcome_failed_surfaces_verbatim():
    """An explicit outcome=failed is surfaced — the A1 predicate also fires on
    outcome alone."""
    result = await _run_node('{"status": "completed", "outcome": "failed", "summary": "partial"}')

    assert result["output"]["agent_status"] == "completed"
    assert result["output"]["agent_outcome"] == "failed"
    assert result["artifacts"][0]["output"]["agent_outcome"] == "failed"


async def test_summary_and_other_fields_still_extracted():
    """The phase-1 change must not disturb the existing summary/changed_files/pr_url extraction."""
    result = await _run_node(
        '{"status": "completed", "summary": "done", "changed_files": ["a.py"], "pr_url": "https://example.com/p"}'
    )

    node_output = result["output"]
    artifact_output = result["artifacts"][0]["output"]
    assert node_output["summary"] == "done"
    assert artifact_output["summary"] == "done"
    assert artifact_output["changed_files"] == ["a.py"]
    assert artifact_output["pr_url"] == "https://example.com/p"


async def test_unparseable_output_json_raises_before_agent_status():
    """An unparseable output.json fails the node before the output path — no
    agent_status is surfaced (a missing verdict can never be a false complete)."""
    output_json = "not json"
    fn = make_sandbox_agent_fn(_base_node_def())
    with (
        patch(
            "e2b.AsyncSandbox.create",
            new=AsyncMock(return_value=_make_sandbox_mock(exit_code=0, output_json=output_json)),
        ),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())


@pytest.mark.parametrize(
    "output_json",
    [
        '{"summary": 42}',
        "[1, 2, 3]",
    ],
)
async def test_non_string_or_non_dict_output_never_sets_agent_status(output_json):
    """A JSON value that is not a dict, or a dict without string status/outcome,
    never fabricates an agent_status."""
    result = await _run_node(output_json, exit_code=0)
    assert result["output"]["agent_status"] is None
    assert result["artifacts"][0]["output"]["agent_status"] is None


async def test_fallback_echo_surfaces_session_lost_marker_not_agent_failed():
    """FAR-227: the E2B wrapper's fallback echo (a dead opencode session —
    ``{"status":"failed","summary":"No output from agent - session
    interrupted"}``) is a PLACEHOLDER, not an agent verdict. It must NOT surface
    as ``agent_status="failed"`` (which A1-elevates to the non-retryable
    ``agent.failed``); the envelope stamps the distinct ``sandbox_session_lost``
    marker instead so the executor routes to retryable ``sandbox.no_output_json``.
    The summary is preserved so the failure detail stays visible."""
    echo = '{"status": "failed", "summary": "No output from agent - session interrupted"}'
    result = await _run_node(echo, exit_code=0)

    node_output = result["output"]
    artifact_output = result["artifacts"][0]["output"]

    assert node_output["agent_status"] is None
    assert artifact_output["agent_status"] is None
    assert node_output["sandbox_session_lost"] is True
    assert artifact_output["sandbox_session_lost"] is True
    # The failure detail survives the marker.
    assert node_output["summary"] == "No output from agent - session interrupted"
    assert artifact_output["summary"] == "No output from agent - session interrupted"
    # A dead session is never a completed node, even when the wrapper exits 0.
    assert node_output["status"] == "failed"
    assert artifact_output["status"] == "failed"


async def test_fallback_echo_nonzero_exit_also_stamped():
    """The marker fires regardless of the command's exit code — the wrapper may
    exit 0 or non-zero after writing the echo."""
    echo = '{"status": "failed", "summary": "No output from agent - session interrupted"}'
    result = await _run_node(echo, exit_code=1)

    assert result["output"]["sandbox_session_lost"] is True
    assert result["output"]["agent_status"] is None
    assert result["output"]["status"] == "failed"


async def test_genuine_verdict_no_marker():
    """A real agent-written verdict with status=failed keeps the verbatim
    ``agent_status="failed"`` AND carries no session-lost marker — the control
    proving the fallback-echo detection is surgical, not over-broad."""
    result = await _run_node('{"status": "failed", "summary": "the task is impossible because X"}', exit_code=0)

    assert result["output"]["agent_status"] == "failed"
    assert "sandbox_session_lost" not in result["output"]
    assert result["artifacts"][0]["output"]["agent_status"] == "failed"


# ---------------------------------------------------------------------------
# FAR-227 — detection variants ("after 3 attempts", error field)
# ---------------------------------------------------------------------------


def test_session_lost_detection_matches_summary_variants():
    """FAR-227 spec point 5: the detector matches BOTH the base summary and the
    '...after 3 attempts' variant — the match is a substring, so a suffix on the
    base string still fires."""
    assert (
        _is_sandbox_session_lost_echo({"status": "failed", "summary": "No output from agent - session interrupted"})
        is True
    )
    assert (
        _is_sandbox_session_lost_echo(
            {"status": "failed", "summary": "No output from agent - session interrupted after 3 attempts"}
        )
        is True
    )


def test_session_lost_detection_matches_error_field_and_rejects_ordinary_output():
    """The detector also reads the ``error`` field (a wrapper may echo the
    placeholder there), and never misreads ordinary summaries/errors as a dead
    session."""
    assert (
        _is_sandbox_session_lost_echo({"status": "failed", "error": "No output from agent - session interrupted"})
        is True
    )
    assert _is_sandbox_session_lost_echo({"status": "failed", "summary": "the task is impossible because X"}) is False
    assert _is_sandbox_session_lost_echo({"status": "completed", "summary": "all good"}) is False
    assert _is_sandbox_session_lost_echo({"status": "failed", "summary": 42}) is False
    assert _is_sandbox_session_lost_echo("not-a-dict") is False
    assert _is_sandbox_session_lost_echo(None) is False


async def test_fallback_echo_after_3_attempts_variant_stamped():
    """The '...after 3 attempts' echo variant stamps the same session-lost
    marker and suppresses the fabricated agent verdict."""
    echo = '{"status": "failed", "summary": "No output from agent - session interrupted after 3 attempts"}'
    result = await _run_node(echo, exit_code=0)

    assert result["output"]["sandbox_session_lost"] is True
    assert result["output"]["agent_status"] is None
    assert result["output"]["status"] == "failed"
    assert result["artifacts"][0]["output"]["sandbox_session_lost"] is True


async def test_fallback_echo_in_error_field_stamped():
    """When the wrapper echoes the placeholder into the ``error`` field (no
    summary), the marker still fires and no agent verdict is fabricated — the
    failure detail falls back to the command error for display."""
    echo = '{"status": "failed", "error": "No output from agent - session interrupted"}'
    result = await _run_node(echo, exit_code=0)

    assert result["output"]["sandbox_session_lost"] is True
    assert result["output"]["agent_status"] is None
    assert result["output"]["status"] == "failed"
    assert result["output"]["summary"]
