"""Unit tests for sandbox_agent ``mode="script"`` (FAR-296 Phase 1).

Covers:
1. The 16-cell agreement matrix — mode x prompt-presence x command-presence
   x output-schema-set — asserting that EVERY gate (the shared mode-aware
   validator, the Pydantic ``PipelineGraphNode``, the GraphValidator, and
   ``make_sandbox_agent_fn``) agrees on valid/invalid for every cell.
2. Verbatim ``script_command`` execution (no Jinja render).
3. Full run input written to /home/user/input.json (no 10KB truncation, no
   prompt.md write).
4. The script-mode output contract: raw parsed output.json is the node output,
   no LLM envelope extraction, standard sandbox envelope shape preserved.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from modulo.api.routes.pipelines import PipelineGraphNode
from modulo.core.graph_validator import GraphValidator, ValidationResult
from modulo.core.pipeline_engine.node_runner import (
    ScriptFailedError,
    ScriptInvalidOutputError,
    make_sandbox_agent_fn,
)
from modulo.core.pipeline_engine.sandbox_mode import (
    _validate_sandbox_mode_config,
    validate_sandbox_agent_command_jinja,
)

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))


def _read_router(output_json: str, log_content: str = ""):
    """Route sandbox.files.read by path: output.json vs the redirected agent log."""

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return output_json
        return log_content

    return _read


def _script_sandbox_mock(*, output_json: str = '{"result": "ok"}', log_content: str = ""):
    """Sandbox mock with the writes a script-mode run issues captured."""
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


def _run_state(payload: Any = None) -> dict:
    run_context = {"input": {"task": "x"}} if payload is None else {"input": payload}
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
# 1. The 16-cell agreement matrix
# ---------------------------------------------------------------------------


def _matrix_cell(
    mode: str | None,
    prompt: bool,
    command: str,
    output_schema: bool,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
    }
    if mode is not None:
        node["mode"] = mode
    if prompt:
        node["agent_prompt"] = "Do the thing"
    if command == "agent_command":
        node["agent_command"] = "opencode run --auto"
    elif command == "script_command":
        node["script_command"] = "python3 main.py"
    if output_schema:
        node["output_schema_json"] = {"type": "object"}
    return node


def _expected_valid(mode: str | None, prompt: bool, command: str) -> bool:
    """The ground truth for the 16-cell matrix.

    llm mode requires agent_prompt AND agent_command; script mode requires
    script_command (agent_prompt is optional and ignored). The command field
    that does NOT match the mode makes the cell invalid.
    """
    effective_mode = mode or "llm"
    if effective_mode == "script":
        return command == "script_command"
    return prompt and command == "agent_command"


_MATRIX_CELLS: list[tuple[str, bool, str, bool]] = [
    (mode, prompt, command, output_schema)
    for mode in ("llm", "script")
    for prompt in (True, False)
    for command in ("agent_command", "script_command")
    for output_schema in (True, False)
]


def _graph_validator_valid(node: dict[str, Any]) -> bool:
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config({"nodes": [node], "edges": []}, result)
    return result.is_valid


@pytest.mark.parametrize(
    "mode,prompt,command,output_schema",
    _MATRIX_CELLS,
    ids=lambda v: str(v),
)
def test_16_cell_matrix_all_gates_agree(mode, prompt, command, output_schema):
    """Every gate agrees on the same valid/invalid classification per cell.

    Gates under test: the shared mode-aware validator (run-time), the Pydantic
    PipelineGraphNode (REST save-time), the GraphValidator (save + pre-run), and
    make_sandbox_agent_fn (node-runner construction). If any gate diverges,
    save-time and run-time validation would disagree — the exact regression the
    matrix exists to prevent.
    """
    node = _matrix_cell(mode, prompt, command, output_schema)
    expect_valid = _expected_valid(mode, prompt, command)

    # Gate 1: shared validator
    try:
        _validate_sandbox_mode_config(node)
        helper_valid = True
    except ValueError:
        helper_valid = False
    assert helper_valid == expect_valid

    # Gate 2: Pydantic model (REST save-time)
    try:
        PipelineGraphNode.model_validate(node)
        pydantic_valid = True
    except ValidationError:
        pydantic_valid = False
    assert pydantic_valid == expect_valid

    # Gate 3: GraphValidator
    assert _graph_validator_valid(node) == expect_valid

    # Gate 4: node-runner construction (run-time)
    try:
        make_sandbox_agent_fn(node)
        runner_valid = True
    except ValueError:
        runner_valid = False
    assert runner_valid == expect_valid


def test_legacy_no_mode_snapshot_reads_as_llm():
    """A node WITHOUT a mode key (legacy snapshot) reads as ``llm``."""
    legacy_valid = _matrix_cell(None, True, "agent_command", False)
    assert _validate_sandbox_mode_config(legacy_valid)[0] == "llm"

    legacy_missing_prompt = _matrix_cell(None, False, "agent_command", False)
    with pytest.raises(ValueError, match="missing required 'agent_prompt'"):
        _validate_sandbox_mode_config(legacy_missing_prompt)

    assert _expected_valid(None, True, "agent_command") is True
    assert PipelineGraphNode.model_validate(legacy_valid).mode == "llm"


def test_mode_validation_error_messages_are_distinct():
    """Each invalid combination surfaces a distinct, descriptive message."""
    base = {"id": "n1"}
    with pytest.raises(ValueError, match="BOTH agent_command"):
        _validate_sandbox_mode_config(
            {**base, "mode": "llm", "agent_prompt": "x", "agent_command": "a", "script_command": "b"}
        )
    with pytest.raises(ValueError, match="invalid mode"):
        _validate_sandbox_mode_config({**base, "mode": "docker", "agent_command": "a"})
    with pytest.raises(ValueError, match="mode='script' requires"):
        _validate_sandbox_mode_config({**base, "mode": "script", "agent_command": "a"})


# ---------------------------------------------------------------------------
# FAR-226: agent_command Jinja syntax validation
# ---------------------------------------------------------------------------


def test_jinja_helper_accepts_plain_command():
    """A plain agent_command (no Jinja syntax) validates clean."""
    assert validate_sandbox_agent_command_jinja({"id": "n1", "mode": "llm", "agent_command": "opencode run"}) is None


def test_jinja_helper_accepts_undefined_var_template():
    """A valid {{ }} template referencing a not-yet-known variable is NOT flagged —
    undefined vars are lenient (render to empty), matching run-time handling."""
    assert (
        validate_sandbox_agent_command_jinja(
            {"id": "n1", "mode": "llm", "agent_command": "opencode --model {{ input.model }} --auto"}
        )
        is None
    )


def test_jinja_helper_rejects_broken_template():
    """An invalid backslash inside {{ }} is a TemplateSyntaxError -> error message."""
    err = validate_sandbox_agent_command_jinja(
        {"id": "n1", "mode": "llm", "agent_command": "opencode --model {{ \\\\ }}"}
    )
    assert err is not None
    assert "agent_command" in err
    assert "n1" in err


def test_jinja_helper_skips_script_mode():
    """script mode runs script_command VERBATIM — no Jinja check applies."""
    assert (
        validate_sandbox_agent_command_jinja({"id": "n1", "mode": "script", "script_command": "python3 x.py"}) is None
    )


def test_jinja_helper_skips_empty_command():
    """An empty/missing agent_command is left to the mode validator, not the Jinja check."""
    assert validate_sandbox_agent_command_jinja({"id": "n1", "mode": "llm", "agent_command": ""}) is None


def test_jinja_helper_validates_agent_commands_list():
    """The joined agent_commands list form is validated against Jinja."""
    good = {"id": "n1", "mode": "llm", "agent_commands": ["opencode run", "--model {{ input.m }}"]}
    assert validate_sandbox_agent_command_jinja(good) is None

    bad = {"id": "n1", "mode": "llm", "agent_commands": ["opencode run", "--model {{ \\\\ }}"]}
    err = validate_sandbox_agent_command_jinja(bad)
    assert err is not None
    assert "agent_command" in err
    assert "n1" in err


# ---------------------------------------------------------------------------
# 2. Verbatim script_command execution (no Jinja render)
# ---------------------------------------------------------------------------


async def test_script_mode_runs_command_verbatim_no_jinja_render():
    """A script_command containing Jinja syntax runs LITERALLY — never rendered.

    If the command were Jinja-rendered, ``{{ input.task }}`` would resolve to
    the input value; a verbatim command keeps the literal template text.
    """
    node_def = _script_node_def(
        script_command="python3 main.py --arg {{ input.task }} ${{ not_a_template }}",
    )
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _script_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state({"task": "resolved-value"}))

    assert result["output"]["status"] == "completed"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert "{{ input.task }}" in wrapped
    assert "${{ not_a_template }}" in wrapped
    assert "resolved-value" not in wrapped


async def test_script_mode_does_not_write_prompt_md():
    """Script mode never writes /home/user/prompt.md and never requires a prompt."""
    node_def = _script_node_def()
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _script_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    written_paths = [call.args[0] for call in sandbox.files.write.call_args_list]
    assert "/home/user/prompt.md" not in written_paths
    assert "/home/user/input.json" in written_paths


# ---------------------------------------------------------------------------
# 3. Full run input at /home/user/input.json (no 10KB truncation)
# ---------------------------------------------------------------------------


async def test_script_mode_writes_full_input_json_no_truncation():
    """The FULL run input payload lands in /home/user/input.json — no 10KB cap.

    The llm path truncates MODULO_INPUT_PAYLOAD above 10KB to a stub; script
    mode must carry the complete payload through the file channel.
    """
    payload = {"big": "x" * 20000, "nested": {"deep": list(range(500))}}
    node_def = _script_node_def()
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _script_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state(payload))

    assert result["output"]["status"] == "completed"
    import json

    written = {
        call.args[0]: call.args[1]
        for call in sandbox.files.write.call_args_list
        if call.args[0] == "/home/user/input.json"
    }
    assert len(written) == 1
    parsed = json.loads(written["/home/user/input.json"])
    assert parsed == payload
    assert "_truncated" not in parsed


async def test_script_mode_env_payload_is_full_not_truncated():
    """MODULO_INPUT_PAYLOAD also carries the FULL payload in script mode."""
    payload = {"big": "y" * 20000}
    node_def = _script_node_def()
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _script_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        await fn(_run_state(payload))

    envs = sandbox.commands.run.call_args.kwargs["envs"]
    import json

    parsed = json.loads(envs["MODULO_INPUT_PAYLOAD"])
    assert parsed == payload
    assert "_truncated" not in parsed


async def test_script_mode_input_json_scalar_and_list():
    """Scalar and list run inputs round-trip through input.json."""
    import json

    for payload in ({"a": 1}, [1, 2, 3], "plain string", 42):
        node_def = _script_node_def()
        fn = make_sandbox_agent_fn(node_def)
        sandbox = _script_sandbox_mock()
        with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
            result = await fn(_run_state(payload))
        assert result["output"]["status"] == "completed"
        written = {
            call.args[0]: call.args[1]
            for call in sandbox.files.write.call_args_list
            if call.args[0] == "/home/user/input.json"
        }
        assert json.loads(written["/home/user/input.json"]) == payload


# ---------------------------------------------------------------------------
# 4. Output contract: raw parsed output.json is the node output
# ---------------------------------------------------------------------------


async def test_script_mode_output_is_raw_parsed_output():
    """output_json carries the raw parsed output; summary is auto-generated.

    No LLM envelope extraction: the script's own fields are NOT elevated into
    status/summary/changed_files/pr_url/agent_status/agent_outcome.
    """
    script_output = {"rows": [1, 2, 3], "meta": {"count": 3}}
    node_def = _script_node_def()
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _script_sandbox_mock(output_json='{"rows": [1, 2, 3], "meta": {"count": 3}}')

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    out = result["output"]
    art = result["artifacts"][0]["output"]
    assert out["status"] == "completed"
    assert art["output_json"] == script_output
    assert out["summary"] == "script mode: exit_code=0"
    assert not art["changed_files"]
    assert not art["pr_url"]
    assert out["agent_status"] is None
    assert out["agent_outcome"] is None
    assert art["exit_code"] == 0
    assert art["output_json"] == script_output


async def test_script_mode_does_not_elevate_llm_envelope_fields():
    """Even an output.json shaped like the LLM envelope is NOT elevated.

    A script that happens to emit summary/changed_files/status/pr_url fields
    must not trigger the LLM envelope path — the raw dict stays the node output
    and status/summary are derived from exit_code / auto-generation.
    """
    node_def = _script_node_def()
    fn = make_sandbox_agent_fn(node_def)
    envelope_like = (
        '{"summary": "llm-ish summary", "changed_files": ["a.py"], '
        '"pr_url": "https://github.com/x/y/pull/1", "status": "complete", "outcome": "yes"}'
    )
    sandbox = _script_sandbox_mock(output_json=envelope_like)

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    out = result["output"]
    art = result["artifacts"][0]["output"]
    assert out["status"] == "completed"  # from exit_code, NOT output_json["status"]
    assert out["summary"] == "script mode: exit_code=0"  # auto-generated, not the script's summary
    assert not art["changed_files"]
    assert not art["pr_url"]
    assert out["agent_status"] is None
    assert out["agent_outcome"] is None
    assert art["output_json"]["summary"] == "llm-ish summary"  # raw, untouched


async def test_script_mode_non_zero_exit_raises_terminal():
    """A non-zero exit in script mode is a POST-CLAIM fault — it RAISES the
    terminal (never-retryable) ``ScriptFailedError``, it does NOT proceed.

    FAR-296 Phase 2 stage-split: once the script process started (the fencing
    lease is claimed), a non-zero exit can never be retried — re-dispatching
    could double-execute the side-effecting script.
    """
    node_def = _script_node_def()
    cmd_result = MagicMock()
    cmd_result.exit_code = 3
    cmd_result.stdout = "script stdout"
    cmd_result.stderr = "boom"
    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read_router('{"partial": true}'))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    fn = make_sandbox_agent_fn(node_def)
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(ScriptFailedError, match="code 3"),
    ):
        await fn(_run_state())


async def test_script_mode_missing_output_json_raises_terminal():
    """Missing/unparseable output.json in script mode raises the TERMINAL
    ``ScriptInvalidOutputError`` (never retryable).

    FAR-296 Phase 2 stage-split: the script process started (lease claimed) but
    produced no parseable output.json — a POST-CLAIM fault, never retried.
    """
    node_def = _script_node_def()
    cmd_result = MagicMock(exit_code=0, stdout="", stderr="")
    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read_router("not json at all"))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    fn = make_sandbox_agent_fn(node_def)
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(ScriptInvalidOutputError),
    ):
        await fn(_run_state())


async def test_script_mode_list_output_carried_in_envelope():
    """A non-dict (list) output is carried verbatim in the envelope's output_json."""
    node_def = _script_node_def()
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _script_sandbox_mock(output_json="[1, 2, 3]")

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    out = result["output"]
    art = result["artifacts"][0]["output"]
    assert art["output_json"] == [1, 2, 3]
    assert out["summary"] == "script mode: exit_code=0"


async def test_script_mode_does_not_require_agent_prompt():
    """Script mode constructs and runs with NO agent_prompt present."""
    node_def = _script_node_def()
    node_def.pop("agent_prompt")
    fn = make_sandbox_agent_fn(node_def)  # must not raise
    sandbox = _script_sandbox_mock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
