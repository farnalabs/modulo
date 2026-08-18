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

import jinja2

_SANDBOX_MODES = frozenset({"llm", "script"})
_SANDBOX_EGRESS_POLICIES = frozenset({"default", "deny_all"})
_SANDBOX_RESOURCE_LIMIT_KEYS = frozenset(
    {"cpu_count", "memory_mb", "disk_mb", "max_processes", "max_fds", "max_sockets"}
)


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


def validate_sandbox_agent_command_jinja(node_def: dict[str, Any]) -> str | None:
    """Validate that an llm-mode sandbox_agent's ``agent_command`` is Jinja-renderable.

    FAR-226: catch a broken ``agent_command`` template at save time instead of
    letting it surface as an opaque instant-fail for every run of the pipeline.

    Returns an error message when the command has invalid Jinja syntax
    (``TemplateSyntaxError``), otherwise ``None``. Only llm mode is checked —
    script mode runs ``script_command`` VERBATIM with no Jinja render. The
    scalar ``agent_command`` and the joined ``agent_commands`` list are both
    validated (the same way node_runner resolves the command).

    Uses the same ``SandboxedEnvironment`` as node_runner so save-time and
    run-time rendering agree. Undefined variables are lenient (render to empty
    under the sandbox's default ``Undefined``), so missing ``{{ input.* }}``
    references are NOT flagged here — only genuinely broken template syntax,
    which the runtime would otherwise only discover (and fall back verbatim on)
    at run time.
    """
    node_id = node_def.get("id")
    if node_def.get("mode", "llm") != "llm":
        return None
    command = node_def.get("agent_command")
    if not command or not str(command).strip():
        agent_commands = node_def.get("agent_commands")
        if not agent_commands:
            return None
        command = node_def.get("commands_concatenation_string", " && ").join(str(c) for c in agent_commands)
    from jinja2.sandbox import SandboxedEnvironment

    try:
        SandboxedEnvironment().from_string(str(command))
    except jinja2.TemplateSyntaxError as exc:
        return f"sandbox_agent node '{node_id}' agent_command is not valid Jinja2: {exc}"
    return None


def _validate_sandbox_egress_config(node_def: dict[str, Any]) -> None:
    """Validate a sandbox_agent node's ``egress_policy`` (FAR-296 Phase 3).

    Allowed values: ``None`` (default), ``"default"``, ``"deny_all"``. Any
    other value raises ``ValueError`` — this is the single shared gate so
    save-time (Pydantic, GraphValidator, MCP) and run-time (node runner) agree
    on what an egress policy means.
    """
    node_id = node_def.get("id")
    egress_policy = node_def.get("egress_policy")
    if egress_policy is None:
        return
    if not isinstance(egress_policy, str) or egress_policy not in _SANDBOX_EGRESS_POLICIES:
        raise ValueError(
            f"sandbox_agent node '{node_id}' has invalid egress_policy {egress_policy!r} "
            "— expected None, 'default' or 'deny_all'"
        )


def _validate_sandbox_resource_limits_config(node_def: dict[str, Any]) -> None:
    """Validate a sandbox_agent node's ``resource_limits`` (FAR-296 Phase 3).

    Fail-closed: if present, ``resource_limits`` must be a dict whose keys are
    a known subset and whose values are positive numbers. Unknown keys raise
    ``ValueError`` (never silently dropped); non-positive values raise too.
    This is the single shared gate so save-time and run-time agree.
    """
    node_id = node_def.get("id")
    resource_limits = node_def.get("resource_limits")
    if resource_limits is None:
        return
    if not isinstance(resource_limits, dict):
        raise ValueError(
            f"sandbox_agent node '{node_id}' has invalid resource_limits {resource_limits!r} — expected an object"
        )
    unknown = set(resource_limits) - _SANDBOX_RESOURCE_LIMIT_KEYS
    if unknown:
        raise ValueError(
            f"sandbox_agent node '{node_id}' resource_limits contains unknown keys "
            f"{sorted(unknown)} — allowed keys are {sorted(_SANDBOX_RESOURCE_LIMIT_KEYS)}"
        )
    for key, value in resource_limits.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(
                f"sandbox_agent node '{node_id}' resource_limits['{key}'] must be a positive number, got {value!r}"
            )
