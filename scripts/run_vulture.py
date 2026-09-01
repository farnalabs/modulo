#!/usr/bin/env python3
"""Cross-platform pre-commit + CI wrapper for the vulture dead-code gate.

Vulture is a pure-Python static analyser (no native core), so unlike
run_semgrep.py this wrapper runs identically on Windows, Linux and macOS and
is BLOCKING everywhere. It exists to give both invocation contexts — the
pre-commit hook (which runs from the repo root) and the CI backend-lint job
(which uses `working-directory: backend`) — a single, path-independent entry
point. The wrapper locates the repo root from its own location and passes
absolute paths to vulture, so the `.vulture_whitelist.py` at the repo root
resolves identically in both.

The whitelist is vulture's documented mechanism: a Python file passed as an
additional PATH whose `__all__ = [...]` list feeds vulture's used_names set.
There is no `--whitelist` CLI flag in vulture 2.16.

Gate semantics (FAR-252):

- ``--min-confidence 60``: vulture reports unused functions/methods/classes at
  60% confidence. The previous gate ran at 80, which only caught unused imports
  (already covered by ruff F401) and unreachable code — dead functions sailed
  through. At 60, unused functions/methods/classes block.
- ``--ignore-decorators``: framework-registration decorators whose callers
  vulture cannot see (FastAPI ``@router.*`` route handlers, ``@mcp.tool`` /
  ``@mcp.resource`` tools, Pydantic ``@field_validator`` / ``@model_validator``,
  SQLAlchemy ``@compiles`` / ``@event.listens_for``, Click ``@cli.command`` /
  ``@click.*``, the internal ``@_rls`` RLS wrapper). Empirically verified
  against the tree — this is the minimal set that clears framework noise.
- ``--ignore-names``: precise framework-interface names that vulture cannot
  resolve to a call site. Every entry is a load-bearing contract (LangChain
  ``on_*_*`` callback methods, the ``ConnectorType`` ABC interface
  ``trigger_run`` / ``get_run_status`` / ``get_run_logs``, the ticket-tracker
  ``update_ticket``, Authlib ``ClientMixin`` methods, the LangGraph
  ``BaseCheckpointSaver`` interface, the stub backend's ``BaseChatModel``
  interface, Alembic ``downgrade`` migration entrypoints, Starlette middleware
  ``dispatch``, ``StrEnum._missing_``, and the lazy-import ``__getattr__`` /
  ``__dir__`` module protocol). None of these can be genuinely-dead in this
  codebase, so the names are precise rather than broad.
- ``unused variable`` findings are NON-BLOCKING (reported to stderr, exit 0):
  at 60% confidence vulture reports every Pydantic model field, dataclass
  field, SQLAlchemy ``mapped_column`` attribute, class constant and Alembic
  migration module var as an "unused variable" because it cannot see the
  metaclass / ORM mapper / migration tooling that consumes them. The names are
  arbitrary (no glob pattern distinguishes ``id: str`` in a Pydantic model from
  a genuinely dead module constant), so suppressing them precisely would need
  hundreds of whitelist entries — exactly the anti-pattern FAR-252 forbids.
  ruff F841 already catches unused *local* variables and F401 unused imports;
  the gate's purpose is dead functions/methods/classes, which stay blocking.
- ``unused function / method / class / property / attribute`` findings are
  BLOCKING after the whitelist. The whitelist ``__all__`` is the ONLY
  sanctioned dead-code escape hatch; it exists solely for framework-contract
  symbols and test-referenced-but-production-dead code, each with a comment.
  New dead code is a blocking finding.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Git-state env vars inherited from a running `git commit` (e.g. the relative
# `GIT_INDEX_FILE=.git/index`) are harmless to vulture (unlike semgrep's
# baseline worktree scan), but strip them anyway for parity with run_semgrep.py
# so both wrappers present a clean environment.
_GIT_STATE_ENV = {
    "GIT_INDEX_FILE",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
}

# Framework-registration decorators whose registered callables vulture cannot
# see. Empirically derived: this set clears the decorator-based framework noise
# at --min-confidence 60 and no wider.
_IGNORE_DECORATORS = (
    "@router.*,@pipeline_triggers_router.*,@mcp.tool,@mcp.resource,"
    "@field_validator,@model_validator,@compiles,@event.listens_for,"
    "@cli.command,@click.*,@_rls"
)

# Precise framework-interface names (see module docstring). Glob patterns match
# the LangChain BaseCallbackHandler method family.
_IGNORE_NAMES = (
    "on_chain_*,on_llm_*,on_chat_model_*,on_tool_*,"
    "downgrade,dispatch,"
    "trigger_run,get_run_status,get_run_logs,update_ticket,"
    "get_client_id,get_default_redirect_uri,check_endpoint_auth_method,"
    "check_grant_type,check_response_type,"
    "get_tuple,put_writes,_load_blobs,_generate,_agenerate,_llm_type,"
    "_missing_,__getattr__,__dir__"
)


def _repo_root() -> str:
    """Return the repository root (the parent of this script's directory)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    root = _repo_root()
    src = os.path.join(root, "backend", "src", "modulo")
    whitelist = os.path.join(root, ".vulture_whitelist.py")
    if not Path(src).is_dir() or not Path(whitelist).is_file():
        print(
            f"run_vulture.py: could not resolve repo layout (src={src!r}, whitelist={whitelist!r})",
            file=sys.stderr,
        )
        return 2

    env = {k: v for k, v in os.environ.items() if k not in _GIT_STATE_ENV}
    cmd = [
        "uv",
        "run",
        "--project",
        "backend",
        "--no-sync",
        "vulture",
        src,
        whitelist,
        "--min-confidence",
        "60",
        "--ignore-decorators",
        _IGNORE_DECORATORS,
        "--ignore-names",
        _IGNORE_NAMES,
    ]
    # Pin the uv subprocess cwd to the repo root so `--project backend`
    # resolves identically whether this wrapper was invoked from the repo root
    # (pre-commit) or from backend/ (CI's working-directory).
    result = subprocess.run(cmd, env=env, cwd=root, capture_output=True, text=True)

    # `unused variable` findings are framework-metaclass noise (Pydantic /
    # dataclass / SQLAlchemy / Alembic-consumed names) — report but never block.
    suppressed = 0
    blocking: list[str] = []
    for line in result.stdout.splitlines():
        if "unused variable '" in line:
            suppressed += 1
            continue
        blocking.append(line)

    if suppressed:
        print(
            f"run_vulture.py: {suppressed} 'unused variable' findings suppressed "
            "(framework-metaclass consumers — see module docstring)",
            file=sys.stderr,
        )
    for line in blocking:
        print(line)

    # 0/3: clean / dead-code-found-but-only-variables. Any blocking finding → 1.
    if result.returncode not in (0, 3):
        return result.returncode
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
