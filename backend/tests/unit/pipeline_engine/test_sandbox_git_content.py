"""Unit tests for the FAR-220 git-content render point (node_runner).

``_resolve_sandbox_git_content_config`` runs at the start of
``_sandbox_agent_impl`` — BEFORE dispatch — and replaces whole-field
``git+<repo>@<sha>#<path>`` refs on ``agent_prompt`` / ``agent_command`` with
the file content at the pinned commit. Non-ref fields pass through untouched;
unpinned refs and fetch failures are typed, fail-closed errors (the node never
dispatches the raw ref string to the agent).
"""

from __future__ import annotations

import uuid

import pytest

import modulo.core.pipeline_engine.node_runner as nr
from modulo.core.pipeline_engine.git_content import (
    GitContentFetchError,
    GitContentRefError,
)
from modulo.core.pipeline_engine.node_runner import (
    _build_sandbox_node_config,
    _resolve_sandbox_git_content_config,
)

_SHA = "a" * 40
_REPO = "https://github.com/example/repo.git"
_PINNED_PROMPT = f"git+{_REPO}@{_SHA}#prompts/x.md"
_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"


def _llm_config(**overrides):
    node_def = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "mode": "llm",
        "template_id": "opencode",
        "agent_prompt": "inline prompt",
        "agent_commands": [_COMMAND],
    }
    node_def.update(overrides)
    return _build_sandbox_node_config(node_def, session_factory=None, single_sandbox_node=True)


def _script_config(**overrides):
    node_def = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "mode": "script",
        "template_id": "opencode",
        "script_command": "echo hi",
    }
    node_def.update(overrides)
    return _build_sandbox_node_config(node_def, session_factory=None, single_sandbox_node=True)


async def test_resolve_config_passthrough_without_refs() -> None:
    config = _llm_config()
    resolved = await _resolve_sandbox_git_content_config(config)
    assert resolved is config


async def test_resolve_config_replaces_pinned_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    async def _fetch(repo_url: str, sha: str, path: str, **_kwargs: object) -> str:
        seen["repo_url"] = repo_url
        seen["sha"] = sha
        seen["path"] = path
        return "PROMPT CONTENT FROM GIT"

    monkeypatch.setattr("modulo.core.pipeline_engine.git_content.fetch_git_content", _fetch)
    config = _llm_config(agent_prompt=_PINNED_PROMPT)
    resolved = await _resolve_sandbox_git_content_config(config)
    assert resolved.agent_prompt_template == "PROMPT CONTENT FROM GIT"
    assert resolved.agent_command == _COMMAND
    assert seen == {"repo_url": _REPO, "sha": _SHA, "path": "prompts/x.md"}


async def test_resolve_config_replaces_pinned_script_command(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fetch(*_args: object, **_kwargs: object) -> str:
        return "echo from-git-driver"

    monkeypatch.setattr("modulo.core.pipeline_engine.git_content.fetch_git_content", _fetch)
    config = _script_config(script_command=_PINNED_PROMPT.replace("prompts/x.md", "drivers/run.py"))
    resolved = await _resolve_sandbox_git_content_config(config)
    assert resolved.agent_command == "echo from-git-driver"
    # script mode has no prompt — stays empty, untouched.
    assert not resolved.agent_prompt_template


async def test_resolve_config_unpinned_ref_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched = False

    async def _fetch(*_args: object, **_kwargs: object) -> str:
        nonlocal fetched
        fetched = True
        return "unused"

    monkeypatch.setattr("modulo.core.pipeline_engine.git_content.fetch_git_content", _fetch)
    config = _llm_config(agent_prompt=f"git+{_REPO}@main#prompts/x.md")
    with pytest.raises(GitContentRefError, match="unpinned"):
        await _resolve_sandbox_git_content_config(config)
    assert not fetched


async def test_resolve_config_fetch_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fetch(*_args: object, **_kwargs: object) -> str:
        raise GitContentFetchError("clone failed: repository unreachable")

    monkeypatch.setattr("modulo.core.pipeline_engine.git_content.fetch_git_content", _fetch)
    config = _llm_config(agent_prompt=_PINNED_PROMPT)
    with pytest.raises(GitContentFetchError, match="clone failed"):
        await _resolve_sandbox_git_content_config(config)


async def test_resolve_config_mixed_command_list_fails_closed() -> None:
    """A joined non-ref command containing git+ never dispatches (M1).

    The 2-item list joins to ``cd /workspace && git+...`` — not itself a ref,
    but carrying a raw ref token that would reach the shell verbatim. The
    render point must fail closed with the typed error (defence in depth for
    save-gate bypasses such as MCP ``update_pipeline_graph``).
    """
    config = _llm_config(
        agent_commands=["cd /workspace", f"git+{_REPO}@{_SHA}#drivers/run.py"],
    )
    with pytest.raises(GitContentRefError, match="whole field"):
        await _resolve_sandbox_git_content_config(config)


async def test_sandbox_impl_resolves_git_content_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring proof: ``_sandbox_agent_impl`` invokes the resolver FIRST.

    The recorder raises a sentinel so the test observes the call without
    entering the heavy dispatch body — without the call the sentinel never
    fires (pre-change: the helper does not exist and this wiring fails).
    """

    class _SentinelError(Exception):
        pass

    recorded: dict[str, object] = {}

    async def _recorder(config):
        recorded["config"] = config
        raise _SentinelError

    monkeypatch.setattr(nr, "_resolve_sandbox_git_content_config", _recorder)
    config = _llm_config(agent_prompt=_PINNED_PROMPT)
    with pytest.raises(_SentinelError):
        await nr._sandbox_agent_impl({}, config=config)
    assert recorded["config"] is config
