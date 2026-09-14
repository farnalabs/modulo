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

import posixpath
import re as _re
from typing import Any

import jinja2

_SANDBOX_MODES = frozenset({"llm", "script"})
_SANDBOX_DEFAULT_MODE = "llm"
_SANDBOX_EGRESS_POLICIES = frozenset({"default", "deny_all", "selected"})
_SANDBOX_EGRESS_ALLOWLIST_KEYS = frozenset({"host", "port"})
# FAR-212 PR B: git-credential scope surface. ``scoped`` = the provisioned git
# credential is limited to a single allowlisted host (github.com — see
# ``_SANDBOX_GIT_CREDENTIAL_ALLOWED_HOST``) via a credential helper that refuses
# every other host; ``unscoped`` = full-access credential (the default); ``none``
# = no git credentials are provisioned at all. These are VALIDATED
# (``_validate_sandbox_git_credentials_config``) and ENFORCED (node_runner's
# sandbox policy step), so the capability derivation can mechanically certify
# scoped credentials.
_SANDBOX_GIT_CREDENTIAL_SCOPES = frozenset({"scoped", "unscoped", "none"})
_SANDBOX_GIT_CREDENTIAL_ALLOWED_HOST = "github.com"
_SANDBOX_RESOURCE_LIMIT_KEYS = frozenset(
    {
        # Informational / metadata-only (the e2b SDK exposes no enforcement
        # point): the request CPU core count, max processes/fds/sockets.
        "cpu_count",
        "max_processes",
        "max_fds",
        "max_sockets",
        # Enforceable platform-side caps (see node_runner's resource-cap killer):
        # cpu_usage_pct is a 0-100 PERCENTAGE (distinct from the cpu_count CORE
        # COUNT above); memory_mb / disk_mb are MiB caps.
        "cpu_usage_pct",
        "memory_mb",
        "disk_mb",
    }
)

# ---------------------------------------------------------------------------
# Sandbox capability vocabulary (FAR-212 PR A)
# ---------------------------------------------------------------------------

# Capability names for the sandbox write/egress surface. These are the names
# the conformance hard-block certifies against: a block-action guardrail whose
# ``required_capabilities`` carries one of these certifies the corresponding
# risk is IMPOSSIBLE (see ``conformance._add_sandbox_surface`` for the
# polarity inversion between the raw derivation and the conformance manifest).
SANDBOX_CAPABILITY_WRITE_FILES = "sandbox.write_files"
SANDBOX_CAPABILITY_EGRESS = "sandbox.egress"
SANDBOX_CAPABILITY_GIT_CREDENTIALS = "sandbox.git_credentials"
# FAR-802: managed workspace inputs. Boolean capability — when True, the
# organisation has at least one managed workspace input configured on the
# environment profile, and the sandbox runtime provisions the checked-out
# workspace instead of the default empty home directory. False/None when
# absent or explicitly disabled.
SANDBOX_CAPABILITY_WORKSPACE_INPUTS = "sandbox.workspace_inputs"
# FAR-798: boolean capability — True when >1 distinct host is granted via
# the multi-host git credential helper. Derived mechanically from the
# ``allowed_hosts`` field on the node definition (a dict with >1 key means
# multi-host). The capability stays boolean; a set-valued capability would
# break the ``dict[str, bool | None]`` conformance contract (ADR 033).
SANDBOX_CAPABILITY_MULTI_HOST = "sandbox.git_credentials.multi_host"

# FAR-212 PR B: ``sandbox.write_files`` and ``sandbox.git_credentials`` are now
# MECHANICALLY DERIVABLE from validated + enforced node config:
#   - ``read_only`` is a real ``PipelineGraphNode`` field (PR B), validated by
#     ``_validate_sandbox_read_only_config`` and ENFORCED at runtime (node_runner
#     applies a sandbox-policy step that chmods the workspace read-only), so
#     ``write_files=False`` certifies writes are genuinely impossible.
#   - ``git_credentials`` is a real ``PipelineGraphNode`` field (PR B), validated
#     by ``_validate_sandbox_git_credentials_config`` and ENFORCED at runtime
#     (node_runner provisions a scoped credential helper that refuses any host
#     but the allowlisted one, or omits git credentials entirely for "none"), so
#     ``git_credentials=True`` (scoped) certifies scoped credentials genuinely
#     limited to the target host.
# The derivation reads the VALIDATED node config (from PipelineGraphNode), never
# raw unvalidated dict keys, so a block guardrail never certifies a
# deny-guarantee nothing enforces.


def derive_sandbox_capabilities(node_def: dict[str, Any]) -> dict[str, bool | None]:
    """Mechanically derive a sandbox_agent node's capability profile.

    Reads the node's ACTUAL configuration — the FAR-296 Phase 3 egress surface
    (``egress_policy``) and the FAR-212 PR B read-only-workspace + git-credential
    scope surfaces (``read_only`` / ``git_credentials``), all of which are real
    validated ``PipelineGraphNode`` fields and are ENFORCED at runtime
    (node_runner maps ``egress_policy`` to ``allow_internet_access`` and applies
    a sandbox-policy step for read-only chmod + git-credential scoping). Returns
    ``{capability: bool | None}`` with the RAW mechanical polarity:

      ``sandbox.egress``
          False when ``egress_policy`` is ``"deny_all"`` or ``"selected"`` —
          node_runner maps both to ``allow_internet_access=False``; True when
          the policy is absent (default) or ``"default"``; None when the
          declared value is unrecognised.
      ``sandbox.write_files``
          False when ``read_only`` is truthy (the read-only workspace is
          enforced — chmod makes writes impossible); True when ``read_only`` is
          False (the sandbox workspace is writable); None when the value is
          unrecognised or absent (unknown — fail-closed). Only a VALIDATED
          ``read_only`` boolean is read; any other value resolves None so a
          smuggled non-boolean key can never certify a deny-guarantee.
      ``sandbox.git_credentials``
          True when ``git_credentials`` is ``"scoped"`` (the enforced
          credential-helper scope genuinely limits the token to the allowlisted
          host); False when ``"unscoped"`` or ``"none"`` (full-access or absent
          credentials are not a scoped guarantee); None when the value is
          unrecognised or absent (unknown — fail-closed).

    The derivation is MECHANICAL — it reads the node's actual validated
    configuration, never a declared claim — so a conformance hard-block can
    CERTIFY what is genuinely enforced (egress, read-only workspace, scoped git
    credentials) and fails CLOSED (unknown) for anything not yet a real,
    validated, enforced surface. The polarity here is raw (True = present /
    risked for egress and write_files; True = the scoped guarantee holds for
    git_credentials); the conformance manifest reader inverts the deny-pair
    (``conformance._add_sandbox_surface``) because a block guardrail's
    ``required_capabilities`` on the write/egress surface is a deny/negative
    guarantee, while ``sandbox.git_credentials`` is a positive guarantee and is
    stamped as-is. Non-sandbox nodes contribute an empty profile.
    """
    node_type = node_def.get("node_type")
    if node_type is not None and node_type != "sandbox_agent":
        return {}

    caps: dict[str, bool | None] = {}

    egress_policy = node_def.get("egress_policy")
    if egress_policy is None:
        caps[SANDBOX_CAPABILITY_EGRESS] = True
    elif isinstance(egress_policy, str) and egress_policy in _SANDBOX_EGRESS_POLICIES:
        caps[SANDBOX_CAPABILITY_EGRESS] = egress_policy not in ("deny_all", "selected")
    else:
        caps[SANDBOX_CAPABILITY_EGRESS] = None

    # FAR-212 PR B: read_only is a REAL validated + enforced PipelineGraphNode
    # field. Only a genuine bool is read; any other value (smuggled non-bool,
    # absent) resolves None (unknown) so a block guardrail fails CLOSED and can
    # never certify a deny-guarantee from an unvalidated key.
    read_only = node_def.get("read_only")
    if isinstance(read_only, bool):
        caps[SANDBOX_CAPABILITY_WRITE_FILES] = not read_only
    else:
        caps[SANDBOX_CAPABILITY_WRITE_FILES] = None

    # FAR-212 PR B: git_credentials is a REAL validated + enforced
    # PipelineGraphNode field. Only the recognised scopes are read; anything
    # else (smuggled value, absent) resolves None (unknown) so a block
    # guardrail can never certify scoped credentials from an unvalidated key.
    git_credentials = node_def.get("git_credentials")
    if isinstance(git_credentials, str) and git_credentials in _SANDBOX_GIT_CREDENTIAL_SCOPES:
        caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] = git_credentials == "scoped"
    else:
        caps[SANDBOX_CAPABILITY_GIT_CREDENTIALS] = None

    # FAR-798: multi_host is a BOOLEAN capability — True when >1 distinct
    # host is granted via the ``allowed_hosts`` field on the node definition.
    # The capability stays boolean; a set-valued capability would break the
    # ``dict[str, bool | None]`` conformance contract (ADR 033).
    #
    # The derivation reads the node config FAIL-CLOSED like the other
    # capabilities: only a validated ``dict[str, str]`` (host -> env-var name)
    # is trusted. Any other shape (a smuggled dict whose values are not host/
    # env-var strings, or a non-dict) resolves ``None`` so a block guardrail
    # can never certify a deny-guarantee from an unvalidated key — a smuggled
    # dict with >1 keys must NOT silently certify True with no enforcement
    # behind it.
    allowed_hosts = node_def.get("allowed_hosts")
    if isinstance(allowed_hosts, dict):
        items = list(allowed_hosts.items())
        if not items:
            # Empty dict: no host granted -> multi_host is False (not unknown).
            caps[SANDBOX_CAPABILITY_MULTI_HOST] = False
        elif any(not isinstance(k, str) or not isinstance(v, str) for k, v in items):
            # Smuggled / non-string keys/values: fail CLOSED to None so a block
            # guardrail can never certify a deny-guarantee from an unvalidated
            # key (matches the read_only / git_credentials capabilities).
            caps[SANDBOX_CAPABILITY_MULTI_HOST] = None
        else:
            # Validated shape (str keys -> str env-var names): multi-host iff
            # more than one host is granted.
            caps[SANDBOX_CAPABILITY_MULTI_HOST] = len(items) > 1
    elif allowed_hosts is None:
        # Absent: no multi-host credential is granted (normal scoped single-host
        # default) — multi_host is False, not unknown.
        caps[SANDBOX_CAPABILITY_MULTI_HOST] = False
    else:
        # Non-dict shape is unvalidated input: fail CLOSED to None.
        caps[SANDBOX_CAPABILITY_MULTI_HOST] = None

    return caps


def _validate_sandbox_mode_config(node_def: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Validate a sandbox_agent node's mode-scoped command configuration.

    FAR-296: a sandbox_agent node runs either an LLM agent (``mode="llm"``, the
    default and legacy behaviour) or a verbatim script (``mode="script"``). The
    two modes are mutually exclusive: ``agent_commands`` belongs to llm mode,
    ``script_command`` to script mode. An absent ``mode`` key (legacy no-mode
    snapshots) reads as ``"llm"``.

    Returns ``(mode, command, config)``:
      - ``mode``: ``"llm"`` | ``"script"``
      - ``command``: the command to execute — the joined agent_commands for
        llm mode; the VERBATIM script_command for script mode (never
        Jinja-rendered).
      - ``config``: mode-scoped extras — ``{"agent_prompt": <str>}`` for llm
        mode (required non-empty); ``{}`` for script mode (prompt not required
        and never written to the sandbox).

    Raises ``ValueError`` with a descriptive message on invalid combinations:
      - unknown ``mode`` value
      - BOTH agent_commands AND script_command present
      - ``mode="llm"``: missing/empty agent_prompt or agent_commands
      - ``mode="script"``: missing/empty script_command
    """
    node_id = node_def.get("id")
    mode = node_def.get("mode", _SANDBOX_DEFAULT_MODE)
    if mode not in _SANDBOX_MODES:
        raise ValueError(f"sandbox_agent node '{node_id}' has invalid mode {mode!r} — expected 'llm' or 'script'")
    commands_concatenation_string: str = node_def.get("commands_concatenation_string", " && ")
    agent_commands_raw: list[str] | None = node_def.get("agent_commands")
    script_command_raw: str | None = node_def.get("script_command")

    has_agent_command = bool(agent_commands_raw)
    has_script_command = bool(script_command_raw and str(script_command_raw).strip())

    if has_agent_command and has_script_command:
        raise ValueError(
            f"sandbox_agent node '{node_id}' has BOTH agent_commands "
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
        agent_command = commands_concatenation_string.join(str(c) for c in agent_commands_raw)
        if not agent_command.strip():
            agent_command = ""
    if not agent_commands_raw or not agent_command:
        raise ValueError(
            f"sandbox_agent node '{node_id}' is missing required 'agent_commands' "
            "— a sandbox agent cannot run without an explicit command"
        )
    return mode, agent_command, {"agent_prompt": str(agent_prompt)}


def validate_sandbox_agent_command_jinja(node_def: dict[str, Any]) -> str | None:
    """Validate that an llm-mode sandbox_agent's ``agent_commands`` list is Jinja-renderable.

    FAR-226: catch a broken ``agent_commands`` template at save time instead of
    letting it surface as an opaque instant-fail for every run of the pipeline.

    Returns an error message when any command has invalid Jinja syntax
    (``TemplateSyntaxError``), otherwise ``None``. Only llm mode is checked —
    script mode runs ``script_command`` VERBATIM with no Jinja render.

    Uses the same ``SandboxedEnvironment`` as node_runner so save-time and
    run-time rendering agree. Undefined variables are lenient (render to empty
    under the sandbox's default ``Undefined``), so missing ``{{ input.* }}``
    references are NOT flagged here — only genuinely broken template syntax,
    which the runtime would otherwise only discover (and fall back verbatim on)
    at run time.
    """
    node_id = node_def.get("id")
    if node_def.get("mode", _SANDBOX_DEFAULT_MODE) != _SANDBOX_DEFAULT_MODE:
        return None
    agent_commands = node_def.get("agent_commands")
    if not agent_commands:
        return None
    command = node_def.get("commands_concatenation_string", " && ").join(str(c) for c in agent_commands)
    from jinja2.sandbox import SandboxedEnvironment

    try:
        SandboxedEnvironment().from_string(str(command))
    except jinja2.TemplateSyntaxError as exc:
        return f"sandbox_agent node '{node_id}' agent_commands is not valid Jinja2: {exc}"
    return None


def _validate_sandbox_egress_config(node_def: dict[str, Any]) -> None:
    """Validate a sandbox_agent node's ``egress_policy`` (FAR-296 Phase 3).

    Allowed values: ``None`` (default), ``"default"``, ``"deny_all"``,
    ``"selected"`` (FAR-296 Phase 3b-3). Any other value raises
    ``ValueError`` — this is the single shared gate so save-time (Pydantic,
    GraphValidator, MCP) and run-time (node runner) agree on what an egress
    policy means.

    IMPORTANT (FAR-296 Phase 3b-3 limitation): ``selected`` currently DENIES
    ALL egress at runtime (``allow_internet_access=False``) and only carries
    the host:port allowlist as sandbox metadata — the allowlist is NOT yet
    honored by any enforcement point (the e2b SDK has no native allowlist
    control; a template-side mechanism does not exist yet). Until that point
    lands, ``selected`` is functionally equivalent to ``deny_all`` and must
    not be advertised as opening specific hosts.
    """
    node_id = node_def.get("id")
    egress_policy = node_def.get("egress_policy")
    if egress_policy is None:
        return
    if not isinstance(egress_policy, str) or egress_policy not in _SANDBOX_EGRESS_POLICIES:
        raise ValueError(
            f"sandbox_agent node '{node_id}' has invalid egress_policy {egress_policy!r} "
            "— expected None, 'default', 'deny_all' or 'selected'"
        )


def _validate_sandbox_egress_allowlist_config(egress_policy: str | None, egress_allowlist: Any, node_id: str) -> None:
    """Validate the host:port egress allowlist (FAR-296 Phase 3b-3).

    Fail-closed: ``selected`` REQUIRES a non-empty allowlist; any other
    policy must NOT carry an allowlist (a control that would silently
    no-op is rejected at save-time).

    Each entry must be a dict with exactly ``host`` (non-empty str) and
    ``port`` (int, 1-65535); unknown keys raise ``ValueError``.

    LIMITATION: the allowlist is metadata-only today. ``selected`` denies all
    egress at runtime (``allow_internet_access=False``) and the allowlist is
    carried as sandbox metadata awaiting a FUTURE template-side enforcement
    point — it is NOT honored at runtime yet (FAR-296 Phase 3b-3).
    """
    if egress_policy != "selected":
        if egress_allowlist is not None:
            raise ValueError(
                f"sandbox_agent node '{node_id}' egress_allowlist is only valid with "
                "egress_policy='selected' — a non-selected policy has no allowlist "
                "enforcement point and would silently no-op"
            )
        return
    _require_non_empty_egress_allowlist(egress_allowlist, node_id)
    for index, entry in enumerate(egress_allowlist):
        _validate_egress_allowlist_entry(entry, index, node_id)


def _require_non_empty_egress_allowlist(egress_allowlist: Any, node_id: str) -> None:
    """Fail-closed: ``selected`` requires a non-empty list of allowlist entries."""
    if not isinstance(egress_allowlist, list) or not egress_allowlist:
        raise ValueError(
            f"sandbox_agent node '{node_id}' egress_policy='selected' requires a "
            "non-empty 'egress_allowlist' of {{'host', 'port'}} entries"
        )


def _validate_egress_allowlist_entry(entry: Any, index: int, node_id: str) -> None:
    """Validate a single egress allowlist entry (host/port dict, known keys)."""
    if not isinstance(entry, dict):
        raise ValueError(
            f"sandbox_agent node '{node_id}' egress_allowlist[{index}] must be an "
            f"object with 'host' and 'port', got {entry!r}"
        )
    unknown = set(entry) - _SANDBOX_EGRESS_ALLOWLIST_KEYS
    if unknown:
        raise ValueError(
            f"sandbox_agent node '{node_id}' egress_allowlist[{index}] contains "
            f"unknown keys {sorted(unknown)} — allowed keys are "
            f"{sorted(_SANDBOX_EGRESS_ALLOWLIST_KEYS)}"
        )
    _validate_allowlist_host(entry.get("host"), index, node_id)
    _validate_allowlist_port(entry.get("port"), index, node_id)


def _validate_allowlist_host(host: Any, index: int, node_id: str) -> None:
    """Validate the ``host`` field of an egress allowlist entry."""
    if not isinstance(host, str) or not host.strip():
        raise ValueError(
            f"sandbox_agent node '{node_id}' egress_allowlist[{index}] 'host' must be a non-empty string, got {host!r}"
        )


def _validate_allowlist_port(port: Any, index: int, node_id: str) -> None:
    """Validate the ``port`` field of an egress allowlist entry."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(
            f"sandbox_agent node '{node_id}' egress_allowlist[{index}] 'port' must "
            f"be an int in [1, 65535], got {port!r}"
        )


def _validate_sandbox_read_only_config(node_def: dict[str, Any]) -> None:
    """Validate a sandbox_agent node's ``read_only`` flag (FAR-212 PR B).

    ``read_only`` is a boolean: when True the sandbox workspace is mounted /
    chmodded read-only at runtime (node_runner applies the enforcement policy),
    so ``sandbox.write_files`` derives False. Only a genuine boolean is
    accepted — a non-bool (e.g. a smuggled string) raises ``ValueError`` so the
    fail-closed derivation never reads an unvalidated key. Absent (None) is
    valid (writable sandbox default).
    """
    node_id = node_def.get("id")
    read_only = node_def.get("read_only")
    if read_only is None:
        return
    if not isinstance(read_only, bool):
        raise ValueError(f"sandbox_agent node '{node_id}' read_only must be a boolean, got {read_only!r}")


def _validate_sandbox_git_credentials_config(node_def: dict[str, Any]) -> None:
    """Validate a sandbox_agent node's ``git_credentials`` scope (FAR-212 PR B).

    Allowed values: ``None`` (default → full-access credential, equivalent to
    ``"unscoped"``), ``"scoped"`` (credential limited to the allowlisted
    ``github.com`` host via an enforced helper), ``"unscoped"`` (full access),
    ``"none"`` (no git credentials provisioned). Any other value raises
    ``ValueError`` — this is the single shared gate so save-time (Pydantic,
    GraphValidator, MCP) and run-time (node runner) agree on what a git-credential
    scope means, and the capability derivation only reads a recognised scope.
    """
    node_id = node_def.get("id")
    git_credentials = node_def.get("git_credentials")
    if git_credentials is None:
        return
    if not isinstance(git_credentials, str) or git_credentials not in _SANDBOX_GIT_CREDENTIAL_SCOPES:
        raise ValueError(
            f"sandbox_agent node '{node_id}' has invalid git_credentials {git_credentials!r} "
            "— expected None, 'scoped', 'unscoped' or 'none'"
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


def _validate_sandbox_wallclock_budget_config(
    wallclock_budget_seconds: Any, _sandbox_timeout_seconds: Any, node_id: str
) -> None:
    """Validate the wall-clock spend budget (FAR-296 Phase 4a).

    Fail-closed: the budget must be a positive int (or None). A budget that
    cannot be compared to the wall clock is rejected at save-time.
    """
    if wallclock_budget_seconds is None:
        return
    if isinstance(wallclock_budget_seconds, bool) or not isinstance(wallclock_budget_seconds, int):
        raise ValueError(
            f"sandbox_agent node '{node_id}' wallclock_budget_seconds must be a positive int (seconds), "
            f"got {wallclock_budget_seconds!r}"
        )
    if wallclock_budget_seconds < 1:
        raise ValueError(
            f"sandbox_agent node '{node_id}' wallclock_budget_seconds must be a positive int (>= 1), "
            f"got {wallclock_budget_seconds!r}"
        )
    # Cross-check (informational): a budget that is tighter than the node timeout
    # is the intended use — the budget is the spend cap, the timeout the backstop.
    # No error here; a budget >= the timeout is equally valid (the timeout fires
    # first). The comparison is only validated for TYPE: ``sandbox_timeout_seconds``
    # is a positive int when present, so an incomparable budget was already rejected
    # above. Kept as a named parameter so the signature documents the relationship.


# ---------------------------------------------------------------------------
# FAR-802: managed workspace inputs — save-time validation
# ---------------------------------------------------------------------------

# Jinja block pattern: ``{{ ... }}`` or ``{% ... %}`` (non-greedy, any content).
_JINJA_BLOCK_RE = _re.compile(r"\{\{.*?\}\}|\{%.*?%\}", _re.DOTALL)

# Destination paths that must never be written by managed inputs.
_DEST_DENYLIST = frozenset(
    {
        ".git",
        "agent.log",
        "output.json",
        ".gitconfig",
        ".git-policy",
        ".ssh",
    }
)

# Supported URL schemes for managed-input refs.
_SUPPORTED_URL_SCHEMES = frozenset({"https", "ssh", "git@"})

# Valid ref kinds.
_VALID_REF_KINDS = frozenset({"branch", "tag", "sha"})

# Maximum dest depth below /home/user/ (segments after /home/user/).
_MAX_DEST_DEPTH = 8


def _strip_jinja_blocks(text: str) -> str:
    """Normalise ``{{ ... }}`` / ``{% ... %}`` blocks in *text* for literal checks.

    Jinja block delimiters are removed but their INNER expression text is kept
    (``{{ 'git clone' }}`` becomes ``'git clone'``, ``git {{ clone_cmd }}``
    becomes ``git clone_cmd``).  This keeps a ``git``/``clone`` pair detectable
    when it is split across a Jinja boundary — the previous behaviour (dropping
    the block contents entirely) let a clone command smuggled inside a Jinja
    block bypass the literal ``git clone`` check, because blocks were stripped
    before substring matching.  Whitespace is then collapsed so ``git {{ x }}
    clone`` still reads as ``git clone``.
    """
    # Keep the inner expression text; drop only the surrounding delimiters.
    normalized = _JINJA_BLOCK_RE.sub(lambda m: m.group(0)[2:-2], text)
    return _re.sub(r"\s+", " ", normalized)


def _validate_sandbox_managed_inputs_config(node_def: dict[str, Any]) -> None:
    """Validate a sandbox_agent node's managed workspace inputs configuration.

    FAR-802: managed workspace inputs allow an organisation to provision
    checked-out repositories inside the sandbox workspace.  This validator
    enforces safety invariants at save time (GraphValidator / MCP
    ``update_pipeline_graph`` / Pydantic model) so invalid configurations are
    rejected before a run is ever dispatched.

    Rules enforced:

    * ``agent_commands`` must NOT contain a literal ``git clone`` — managed inputs
      handle checkout; a user-provided ``git clone`` would race with or bypass
      the managed checkout.  The check is Jinja-aware: a ``git``/``clone`` pair
      split across a Jinja block (e.g. ``{{ 'git clone' }}`` or ``git
      {{ clone_cmd }}``) is still detected, while a bare ``{{ git_clone }}``
      interpolation is allowed.
    * Each input's ``dest`` (after canonicalisation to an absolute POSIX path)
      must:
      - be non-empty and not just ``"."`` or ``"/home/user"``
      - resolve to a path UNDER ``/home/user/`` (no traversal to ``/tmp/`` etc.)
      - NOT be on the denylist (``.git``, ``agent.log``, ``output.json``,
        ``.gitconfig``, ``.git-policy/``, ``.ssh/``)
    * Each input's ``ref.kind`` must be one of ``branch``, ``tag``, ``sha``.
    * Each input's ``url`` must use a supported scheme (``https``, ``ssh``,
      ``git@``).

    Connector-credential checks (hosted profile ``repo_url``/``repo_ref``) are
    DEFERRED — the profile data is not reachable from this validation layer.
    The runtime executor enforces that constraint; see the deferred work note
    in the slice-B report.

    Raises ``ValueError`` with a descriptive message on any violation.
    """
    node_id: str = str(node_def.get("id", ""))
    workspace_inputs: list[dict[str, Any]] | None = node_def.get("workspace_inputs")
    if not workspace_inputs:
        return

    # --- (a) literal git clone check (Jinja-aware) ---
    agent_commands = node_def.get("agent_commands")
    if isinstance(agent_commands, list):
        for agent_command in agent_commands:
            if not isinstance(agent_command, str) or not agent_command:
                continue
            # A ``git``/``clone`` pair may be split across a Jinja boundary, so check
            # two normalised views:
            #   1. inner-kept    — block delimiters dropped, inner expression kept
            #      (catches ``{{ 'git clone' }}`` and ``git {{ clone_cmd }}``);
            #   2. block-stripped — blocks replaced with a single space
            #      (catches ``git {{ x }} clone`` where the variable resolves empty).
            # Both collapse whitespace first so adjacency survives.  A bare
            # ``{{ git_clone }}`` interpolation is correctly allowed by neither.
            inner_kept = _strip_jinja_blocks(agent_command)
            block_stripped = _re.sub(r"\s+", " ", _JINJA_BLOCK_RE.sub(" ", agent_command))
            if "git clone" in inner_kept or "git clone" in block_stripped:
                raise ValueError(
                    f"sandbox_agent node '{node_id}' agent_commands item contains a literal "
                    "'git clone' after Jinja block removal — managed workspace inputs "
                    "handle checkout; a user-provided git clone would race with or "
                    "bypass the managed checkout"
                )

    # --- per-input validation ---
    for index, inp in enumerate(workspace_inputs):
        if not isinstance(inp, dict):
            raise ValueError(f"sandbox_agent node '{node_id}' workspace_inputs[{index}] must be an object, got {inp!r}")
        _validate_managed_input_dest(inp.get("dest"), index, node_id)
        _validate_managed_input_ref(inp.get("ref"), index, node_id)
        _validate_managed_input_url(inp.get("url"), index, node_id)


def _validate_managed_input_dest(dest: Any, index: int, node_id: str) -> None:
    """Validate the ``dest`` field of a managed workspace input.

    The destination is canonicalised to an absolute POSIX path and checked
    against traversal, denylist, and minimum-depth rules.
    """
    if not isinstance(dest, str) or not dest.strip():
        raise ValueError(f"sandbox_agent node '{node_id}' workspace_inputs[{index}] 'dest' must be a non-empty string")
    dest = dest.strip()

    # Canonicalise to absolute POSIX path.
    if not posixpath.isabs(dest):
        # Relative paths are resolved relative to /home/user/.
        dest = posixpath.join("/home/user", dest)
    dest = posixpath.normpath(dest)

    # Reject "." or "/" or bare "/home/user".
    if dest in (".", "/", "/home/user"):
        raise ValueError(
            f"sandbox_agent node '{node_id}' workspace_inputs[{index}] "
            f"'dest' {dest!r} is not a valid target — must be a specific "
            "subdirectory under /home/user/"
        )

    # Must be under /home/user/.
    home_prefix = "/home/user/"
    if not dest.startswith(home_prefix) and dest != "/home/user":
        raise ValueError(
            f"sandbox_agent node '{node_id}' workspace_inputs[{index}] "
            f"'dest' {dest!r} resolves outside /home/user/ — workspace "
            "inputs must not traverse to other filesystem locations"
        )

    # Depth check (too deep is suspicious).
    rel = dest[len("/home/user/") :] if dest.startswith(home_prefix) else ""
    depth = len([p for p in rel.split("/") if p])
    if depth > _MAX_DEST_DEPTH:
        raise ValueError(
            f"sandbox_agent node '{node_id}' workspace_inputs[{index}] "
            f"'dest' {dest!r} is {depth} levels deep (max {_MAX_DEST_DEPTH})"
        )

    # Denylist check — compare the leaf name and trailing path component.
    parts = [p for p in dest.split("/") if p]
    for part in parts:
        if part in _DEST_DENYLIST:
            raise ValueError(
                f"sandbox_agent node '{node_id}' workspace_inputs[{index}] "
                f"'dest' {dest!r} contains denied path component '{part}'"
            )


def _validate_managed_input_ref(ref: Any, index: int, node_id: str) -> None:
    """Validate the ``ref`` field of a managed workspace input.

    ``ref`` must be a dict with a ``kind`` key whose value is one of
    ``branch``, ``tag``, or ``sha``.
    """
    if ref is None:
        # No ref — default (HEAD/main) is fine.
        return
    if not isinstance(ref, dict):
        raise ValueError(
            f"sandbox_agent node '{node_id}' workspace_inputs[{index}] 'ref' must be an object, got {ref!r}"
        )
    kind = ref.get("kind")
    if not isinstance(kind, str) or kind not in _VALID_REF_KINDS:
        raise ValueError(
            f"sandbox_agent node '{node_id}' workspace_inputs[{index}] "
            f"'ref.kind' {kind!r} is not valid — expected one of "
            f"{sorted(_VALID_REF_KINDS)}"
        )


def _validate_managed_input_url(url: Any, index: int, node_id: str) -> None:
    """Validate the ``url`` field of a managed workspace input.

    The URL must use a supported scheme (``https``, ``ssh``, or ``git@``).
    """
    if url is None:
        # No URL — the input may reference a connector instead (deferred).
        return
    if not isinstance(url, str) or not url.strip():
        raise ValueError(
            f"sandbox_agent node '{node_id}' workspace_inputs[{index}] 'url' must be a non-empty string when provided"
        )
    url = url.strip()
    # Valid prefixes: ``https://``, ``ssh://`` (scheme + authority), and
    # ``git@`` (SCP-style: ``git@host:path``).  ``git@`` is a bare prefix —
    # no ``://`` separator — so it is handled separately from the ``://`` schemes.
    _colon_slash_schemes = frozenset(s for s in _SUPPORTED_URL_SCHEMES if s != "git@")
    _valid_prefixes = (*[s + "://" for s in _colon_slash_schemes], "git@")
    if not url.startswith(_valid_prefixes):
        raise ValueError(
            f"sandbox_agent node '{node_id}' workspace_inputs[{index}] "
            f"'url' {url!r} uses an unsupported scheme — supported schemes "
            f"are {sorted(_SUPPORTED_URL_SCHEMES)}"
        )
