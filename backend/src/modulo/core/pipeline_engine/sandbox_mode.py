"""Shared mode-aware validation for ``sandbox_agent`` nodes (FAR-296).

A ``sandbox_agent`` node runs either an LLM agent (``mode="llm"``, the default
and legacy behaviour) or a verbatim script (``mode="script"``). Every gate that
validates sandbox_agent nodes MUST route through
:func:`_validate_sandbox_mode_config` so save-time (Pydantic model, GraphValidator,
MCP ``update_pipeline_graph``, config linter) and run-time (node runner) validation
agree.

This module is intentionally dependency-free (no LangGraph, no DB) so it can be
imported by the API and validator layers without dragging LangGraph into them —
the import-linter ``api-does-not-import-langgraph-directly`` contract depends on
this being a lightweight module.
"""

from __future__ import annotations

from typing import Any

_SANDBOX_MODES = frozenset({"llm", "script"})


def _validate_sandbox_mode_config(node_def: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Validate a sandbox_agent node's mode-scoped command configuration.

    FAR-296: a sandbox_agent node runs either an LLM agent (``mode="llm"``, the
    default and legacy behaviour) or a verbatim script (``mode="script"``). The
    two modes are mutually exclusive: ``agent_command`` / ``agent_commands``
    belong to llm mode, ``script_command`` to script mode. An absent ``mode``
    key (legacy no-mode snapshots) reads as ``"llm"``.

    Returns ``(mode, command, config)``:
      - ``mode``: ``"llm"`` | ``"script"``
      - ``command``: the command to execute — the joined agent_command /
        agent_commands for llm mode; the VERBATIM script_command for script
        mode (never Jinja-rendered).
      - ``config``: mode-scoped extras — ``{"agent_prompt": <str>}`` for llm
        mode (required non-empty); ``{}`` for script mode (prompt not required
        and never written to the sandbox).

    Raises ``ValueError`` with a descriptive message on invalid combinations:
      - unknown ``mode`` value
      - BOTH agent_command (or agent_commands) AND script_command present
      - ``mode="llm"``: missing/empty agent_prompt or agent_command
      - ``mode="script"``: missing/empty script_command
    """
    node_id = node_def.get("id")
    mode = node_def.get("mode", "llm")
    if mode not in _SANDBOX_MODES:
        raise ValueError(f"sandbox_agent node '{node_id}' has invalid mode {mode!r} — expected 'llm' or 'script'")
    commands_concatenation_string: str = node_def.get("commands_concatenation_string", " && ")
    agent_commands_raw: list[str] | None = node_def.get("agent_commands")
    agent_command_raw: str | None = node_def.get("agent_command")
    script_command_raw: str | None = node_def.get("script_command")

    has_agent_command = bool(agent_commands_raw) or bool(agent_command_raw and str(agent_command_raw).strip())
    has_script_command = bool(script_command_raw and str(script_command_raw).strip())

    if has_agent_command and has_script_command:
        raise ValueError(
            f"sandbox_agent node '{node_id}' has BOTH agent_command (or agent_commands) "
            "and script_command — the two modes are mutually exclusive"
        )

    if mode == "script":
        if not has_script_command:
            raise ValueError(f"sandbox_agent node '{node_id}' mode='script' requires a non-empty 'script_command'")
        return mode, str(script_command_raw), {}

    agent_prompt = node_def.get("agent_prompt")
    if not agent_prompt or not str(agent_prompt).strip():
        raise ValueError(
            f"sandbox_agent node '{node_id}' is missing required 'agent_prompt' "
            "— an empty prompt would dispatch the agent with no instructions"
        )
    if agent_commands_raw:
        agent_command = commands_concatenation_string.join(agent_commands_raw)
    elif agent_command_raw and str(agent_command_raw).strip():
        agent_command = agent_command_raw
    else:
        raise ValueError(
            f"sandbox_agent node '{node_id}' is missing required 'agent_command' "
            "(or 'agent_commands') — a sandbox agent cannot run without an explicit command"
        )
    return mode, agent_command, {"agent_prompt": str(agent_prompt)}
