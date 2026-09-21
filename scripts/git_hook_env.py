"""Shared handling of git hook-context environment variables.

Git and pre-commit inject variables such as ``GIT_DIR`` and
``GIT_INDEX_FILE`` into hook processes.  Any subprocess that runs git
commands, or a test that initialises a scratch repository, must strip them:
left in place, git ignores the subprocess ``cwd`` and targets the enclosing
repository instead.

Keeping the list in one module stops the copies from drifting as hooks and
their tests evolve.
"""

from __future__ import annotations

from collections.abc import Mapping

# Variables injected by git/pre-commit when a hook runs.  These must be
# stripped from any subprocess environment that performs git operations in a
# different working tree.
GIT_HOOK_CONTEXT_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)


def strip_git_hook_context_vars(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of *env* without the git hook-context variables."""
    return {key: value for key, value in env.items() if key not in GIT_HOOK_CONTEXT_VARS}
