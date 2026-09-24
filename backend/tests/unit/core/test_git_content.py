"""Unit tests for modulo.core.pipeline_engine.git_content (FAR-220).

Covers the git-sourced content ref form (``git+<repo>[@<ref>]#<path>``):
parsing + fail-closed rejection, resolution against ``git ls-remote`` output,
pin-on-apply rewriting, and the render-point content substitution helper.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from modulo.core.pipeline_engine import git_content
from modulo.core.pipeline_engine.git_content import (
    GitContentFetchError,
    GitContentRefError,
    _git,
    default_git_content_resolver,
    fetch_git_content,
    git_content_values,
    is_git_content_ref,
    parse_git_content_ref,
    pin_git_content_node_fields,
    pin_git_content_spec,
    resolve_against_ls_remote,
    resolve_git_content_field,
    run_git_ls_remote,
)

_SHA_A = "a" * 40
_SHA_B = "B" * 40  # uppercase exercises canonical lowering
_REPO = "https://github.com/example/repo.git"
_PINNED = f"git+{_REPO}@{_SHA_A}#prompts/x.md"


# ---------------------------------------------------------------------------
# is_git_content_ref / parse_git_content_ref
# ---------------------------------------------------------------------------


def test_is_git_content_ref_positive() -> None:
    assert is_git_content_ref(_PINNED)


def test_is_git_content_ref_rejects_non_strings() -> None:
    assert not is_git_content_ref(42)
    assert not is_git_content_ref(None)


def test_is_git_content_ref_requires_leading_marker() -> None:
    """A value that only MENTIONS git+ mid-string is inline content, not a ref."""
    assert not is_git_content_ref("clone the repo, git+https://x/y#z is the ref form")


def test_parse_full_form() -> None:
    ref = parse_git_content_ref(_PINNED)
    assert ref.repo_url == _REPO
    assert ref.ref == _SHA_A
    assert ref.path == "prompts/x.md"
    assert ref.is_pinned


def test_parse_movable_branch_ref() -> None:
    ref = parse_git_content_ref(f"git+{_REPO}@main#prompts/x.md")
    assert ref.ref == "main"
    assert not ref.is_pinned


def test_parse_absent_ref_is_head_unpinned() -> None:
    ref = parse_git_content_ref(f"git+{_REPO}#prompts/x.md")
    assert ref.ref is None
    assert not ref.is_pinned


def test_parse_scp_url_with_ref() -> None:
    ref = parse_git_content_ref("git+git@github.com:example/repo.git@main#drivers/run.py")
    assert ref.repo_url == "git@github.com:example/repo.git"
    assert ref.ref == "main"
    assert ref.path == "drivers/run.py"


def test_parse_scp_url_without_ref_keeps_git_user() -> None:
    ref = parse_git_content_ref("git+git@github.com:example/repo.git#drivers/run.py")
    assert ref.repo_url == "git@github.com:example/repo.git"
    assert ref.ref is None


def test_parse_branch_with_slash() -> None:
    ref = parse_git_content_ref(f"git+{_REPO}@feature/x#prompts/a.md")
    assert ref.ref == "feature/x"


def test_parse_trims_whitespace() -> None:
    ref = parse_git_content_ref(f"  {_PINNED}  ")
    assert ref.path == "prompts/x.md"


def test_parse_missing_fragment_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="#<path>"):
        parse_git_content_ref(f"git+{_REPO}@main")


def test_parse_empty_path_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="non-empty"):
        parse_git_content_ref(f"git+{_REPO}@main#")


def test_parse_absolute_path_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="repository-relative"):
        parse_git_content_ref(f"git+{_REPO}@main#/etc/passwd")


def test_parse_path_traversal_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="traverse"):
        parse_git_content_ref(f"git+{_REPO}@main#a/../../b")


def test_parse_newline_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="newlines"):
        parse_git_content_ref(f"git+{_REPO}@main#a\nb")


def test_parse_unsupported_scheme_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="unsupported scheme"):
        parse_git_content_ref("git+http://github.com/example/repo@main#a.md")


def test_parse_embedded_credentials_are_rejected() -> None:
    with pytest.raises(GitContentRefError, match="credentials"):
        parse_git_content_ref("git+https://user:pass@github.com/example/repo@main#a.md")


def test_parse_non_git_prefixed_value_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="must start with"):
        parse_git_content_ref("https://github.com/example/repo@main#a.md")


def test_parse_non_string_value_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="must be a string"):
        parse_git_content_ref(123)  # type: ignore[arg-type]


def test_parse_missing_repository_url_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="missing the repository URL"):
        parse_git_content_ref("git+#prompts/x.md")


def test_parse_second_hash_in_path_is_rejected() -> None:
    """Only the FIRST '#' separates repo from path; a second is malformed."""
    with pytest.raises(GitContentRefError, match="single '#'"):
        parse_git_content_ref(f"git+{_REPO}@main#a#b")


def test_parse_whitespace_in_repository_url_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="whitespace"):
        parse_git_content_ref("git+https://github.com/exa mple/repo@main#a.md")


def test_parse_scp_url_with_extra_at_is_rejected() -> None:
    """A second '@' inside an SCP-style URL is a credential-shaped refusal."""
    with pytest.raises(GitContentRefError, match="SCP-style"):
        parse_git_content_ref("git+git@host:a@b@main#x.md")


def test_parse_url_without_host_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="malformed"):
        parse_git_content_ref("git+https://#a.md")


def test_parse_url_without_path_is_rejected() -> None:
    with pytest.raises(GitContentRefError, match="malformed"):
        parse_git_content_ref("git+https://github.com#a.md")


# ---------------------------------------------------------------------------
# resolve_against_ls_remote / pin_git_content_spec
# ---------------------------------------------------------------------------

_LS_REMOTE_REFS = {
    "HEAD": _SHA_A,
    "refs/heads/main": _SHA_A,
    "refs/tags/v1.0": _SHA_B.lower(),
}


def test_resolve_pinned_is_identity_without_lookup() -> None:
    assert resolve_against_ls_remote(parse_git_content_ref(_PINNED), {}) == _SHA_A


def test_resolve_head_uses_head_entry() -> None:
    ref = parse_git_content_ref(f"git+{_REPO}#prompts/x.md")
    assert resolve_against_ls_remote(ref, _LS_REMOTE_REFS) == _SHA_A


def test_resolve_branch_prefers_heads() -> None:
    ref = parse_git_content_ref(f"git+{_REPO}@main#prompts/x.md")
    assert resolve_against_ls_remote(ref, _LS_REMOTE_REFS) == _SHA_A


def test_resolve_tag_falls_back_to_tags() -> None:
    ref = parse_git_content_ref(f"git+{_REPO}@v1.0#prompts/x.md")
    assert resolve_against_ls_remote(ref, _LS_REMOTE_REFS) == _SHA_B.lower()


def test_resolve_unknown_ref_is_typed_error() -> None:
    ref = parse_git_content_ref(f"git+{_REPO}@nope#prompts/x.md")
    with pytest.raises(GitContentRefError, match="not found"):
        resolve_against_ls_remote(ref, _LS_REMOTE_REFS)


def test_resolve_missing_head_is_typed_error() -> None:
    ref = parse_git_content_ref(f"git+{_REPO}#prompts/x.md")
    with pytest.raises(GitContentRefError, match="HEAD not found"):
        resolve_against_ls_remote(ref, {})


def test_pin_spec_is_canonical_lowercase() -> None:
    ref = parse_git_content_ref(f"git+{_REPO}@{'A' * 40}#prompts/x.md")
    pinned = pin_git_content_spec(ref, _SHA_B)
    assert pinned == f"git+{_REPO}@{_SHA_B.lower()}#prompts/x.md"


def test_pin_spec_rejects_non_sha() -> None:
    ref = parse_git_content_ref(f"git+{_REPO}@main#prompts/x.md")
    with pytest.raises(GitContentRefError, match="40-hex"):
        pin_git_content_spec(ref, "not-a-sha")


# ---------------------------------------------------------------------------
# default resolver (plan-time)
# ---------------------------------------------------------------------------


def test_default_resolver_pinned_never_touches_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_url: str) -> str:
        raise AssertionError("network must not be used for a pinned ref")

    monkeypatch.setattr(
        "modulo.core.pipeline_engine.git_content.run_git_ls_remote",
        _boom,
    )
    assert default_git_content_resolver(parse_git_content_ref(_PINNED)) == _SHA_A


def test_default_resolver_movable_uses_ls_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ls(_url: str) -> str:
        return f"{_SHA_A}\trefs/heads/main\n"

    monkeypatch.setattr(
        "modulo.core.pipeline_engine.git_content.run_git_ls_remote",
        _ls,
    )
    ref = parse_git_content_ref(f"git+{_REPO}@main#prompts/x.md")
    assert default_git_content_resolver(ref) == _SHA_A


def test_default_resolver_unresolvable_ref_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ls(_url: str) -> str:
        return f"{_SHA_A}\trefs/heads/other\n"

    monkeypatch.setattr(
        "modulo.core.pipeline_engine.git_content.run_git_ls_remote",
        _ls,
    )
    ref = parse_git_content_ref(f"git+{_REPO}@main#prompts/x.md")
    with pytest.raises(GitContentRefError, match="not found"):
        default_git_content_resolver(ref)


# ---------------------------------------------------------------------------
# git_content_values / pin_git_content_node_fields
# ---------------------------------------------------------------------------


def test_git_content_values_covers_three_fields() -> None:
    node = {
        "agent_prompt": "git+x",
        "script_command": "echo hi",
        "agent_commands": ["a", "git+y", 7],
    }
    assert git_content_values(node) == [
        ("agent_prompt", "git+x"),
        ("script_command", "echo hi"),
        ("agent_commands[0]", "a"),
        ("agent_commands[1]", "git+y"),
    ]


def test_pin_node_fields_pins_movable_via_resolver() -> None:
    def _resolver(ref) -> str:
        assert ref.ref == "main"
        return _SHA_A

    node = {
        "node_type": "sandbox_agent",
        "agent_prompt": f"git+{_REPO}@main#prompts/x.md",
        "script_command": None,
        "agent_commands": ["opencode run"],
    }
    pin_git_content_node_fields(node, resolver=_resolver)
    assert node["agent_prompt"] == f"git+{_REPO}@{_SHA_A}#prompts/x.md"


def test_pin_node_fields_pinned_skips_resolver() -> None:
    def _boom(_ref) -> str:
        raise AssertionError("resolver must not run for a pinned ref")

    node = {
        "node_type": "sandbox_agent",
        "agent_prompt": f"git+{_REPO}@{_SHA_B}#prompts/x.md",
        "agent_commands": [f"git+{_REPO}@{_SHA_A}#drivers/run.py"],
    }
    pin_git_content_node_fields(node, resolver=_boom)
    assert node["agent_prompt"] == f"git+{_REPO}@{_SHA_B.lower()}#prompts/x.md"
    assert node["agent_commands"][0] == f"git+{_REPO}@{_SHA_A}#drivers/run.py"


def test_pin_node_fields_lowercases_uppercase_sha() -> None:
    node = {"node_type": "sandbox_agent", "agent_prompt": f"git+{_REPO}@{_SHA_B}#prompts/x.md"}
    pin_git_content_node_fields(node, resolver=None)
    # Default resolver is identity for pins — no network — via its pinned fast path.
    assert node["agent_prompt"] == f"git+{_REPO}@{_SHA_B.lower()}#prompts/x.md"


def test_pin_node_fields_malformed_ref_raises() -> None:
    node = {"node_type": "sandbox_agent", "agent_prompt": "git+https://github.com/example/repo"}
    with pytest.raises(GitContentRefError, match="#<path>"):
        pin_git_content_node_fields(node, resolver=lambda _ref: _SHA_A)


def test_pin_node_fields_resolution_failure_propagates() -> None:
    def _resolver(_ref) -> str:
        raise GitContentRefError("remote unreachable")

    node = {"node_type": "sandbox_agent", "agent_prompt": f"git+{_REPO}@main#prompts/x.md"}
    with pytest.raises(GitContentRefError, match="remote unreachable"):
        pin_git_content_node_fields(node, resolver=_resolver)


def test_pin_node_fields_inline_content_untouched() -> None:
    node = {"node_type": "sandbox_agent", "agent_prompt": "inline prompt", "agent_commands": ["opencode run"]}
    pin_git_content_node_fields(node, resolver=lambda _ref: _SHA_A)
    assert node["agent_prompt"] == "inline prompt"
    assert node["agent_commands"] == ["opencode run"]


def test_pin_node_fields_skips_non_sandbox_nodes() -> None:
    """Only sandbox_agent nodes resolve git content refs at run time (Minor 2).

    Pinning a ref on any other node type would rewrite a movable ref into a
    literal prompt that nothing ever resolves — no fetch, no drift signal — so
    non-sandbox nodes are skipped entirely and the resolver never runs.
    """

    def _boom(_ref) -> str:
        raise AssertionError("resolver must not run for non-sandbox nodes")

    node = {"node_type": "agent", "agent_prompt": f"git+{_REPO}@main#prompts/x.md"}
    result = pin_git_content_node_fields(node, resolver=_boom)
    assert result is node
    assert node["agent_prompt"] == f"git+{_REPO}@main#prompts/x.md"


# ---------------------------------------------------------------------------
# resolve_git_content_field (render point)
# ---------------------------------------------------------------------------


async def test_resolve_field_passthrough_for_inline_content() -> None:
    assert await resolve_git_content_field("inline prompt") == "inline prompt"


async def test_resolve_field_pinned_fetches_content(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    async def _fetch(repo_url: str, sha: str, path: str, **_kwargs: object) -> str:
        seen["repo_url"] = repo_url
        seen["sha"] = sha
        seen["path"] = path
        return "PROMPT CONTENT FROM GIT"

    monkeypatch.setattr("modulo.core.pipeline_engine.git_content.fetch_git_content", _fetch)
    result = await resolve_git_content_field(_PINNED, node_id="n1", field="agent_prompt")
    assert result == "PROMPT CONTENT FROM GIT"
    assert seen == {"repo_url": _REPO, "sha": _SHA_A, "path": "prompts/x.md"}


async def test_resolve_field_unpinned_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched = False

    async def _fetch(*_args: object, **_kwargs: object) -> str:
        nonlocal fetched
        fetched = True
        return "unused"

    monkeypatch.setattr("modulo.core.pipeline_engine.git_content.fetch_git_content", _fetch)
    with pytest.raises(GitContentRefError, match="unpinned"):
        await resolve_git_content_field(f"git+{_REPO}@main#prompts/x.md", field="agent_prompt")
    assert not fetched


async def test_resolve_field_unpinned_error_message_closes_paren() -> None:
    """The pinned-form example in the unpinned error is a closed parenthetical (Minor 6)."""
    with pytest.raises(GitContentRefError) as excinfo:
        await resolve_git_content_field(f"git+{_REPO}@main#prompts/x.md", field="agent_prompt")
    assert "#<path>)" in str(excinfo.value)


async def test_resolve_field_fetch_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fetch(*_args: object, **_kwargs: object) -> str:
        raise GitContentFetchError("clone failed")

    monkeypatch.setattr("modulo.core.pipeline_engine.git_content.fetch_git_content", _fetch)
    with pytest.raises(GitContentFetchError, match="clone failed"):
        await resolve_git_content_field(_PINNED, field="agent_prompt")


async def test_fetch_git_content_requires_pinned_sha() -> None:
    with pytest.raises(GitContentFetchError, match="40-hex"):
        await fetch_git_content(_REPO, "main", "prompts/x.md")


# ---------------------------------------------------------------------------
# Bounded subprocess seams — run_git_ls_remote / _git / fetch_git_content
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal asyncio subprocess stand-in for the bounded git seams.

    ``communicate`` hangs on its FIRST call when *hang* is set so
    ``asyncio.wait_for`` exercises the timeout path; the post-kill second call
    returns immediately (mirrors the real reap-then-return sequence).
    """

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, hang: bool = False) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.kill_called = False
        self._hang = hang
        self._calls = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        self._calls += 1
        if self._hang and self._calls == 1:
            await asyncio.sleep(30)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.kill_called = True


def _patch_exec(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> None:
    async def _exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return proc

    monkeypatch.setattr(git_content.asyncio, "create_subprocess_exec", _exec)


async def test_run_git_ls_remote_returns_raw_output(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stdout=f"{_SHA_A}\trefs/heads/main\n".encode())
    _patch_exec(monkeypatch, proc)
    assert await run_git_ls_remote(_REPO) == f"{_SHA_A}\trefs/heads/main\n"


async def test_run_git_ls_remote_nonzero_exit_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stderr=b"fatal: repository not found\n", returncode=128)
    _patch_exec(monkeypatch, proc)
    with pytest.raises(GitContentRefError, match="ls-remote failed"):
        await run_git_ls_remote(_REPO)


async def test_run_git_ls_remote_timeout_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(hang=True)
    _patch_exec(monkeypatch, proc)
    with pytest.raises(GitContentRefError, match="timed out"):
        await run_git_ls_remote(_REPO, timeout_seconds=0.01)
    assert proc.kill_called


async def test_git_returns_stdout_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stdout=b"file bytes")
    _patch_exec(monkeypatch, proc)
    out = await _git(("show", f"{_SHA_A}:prompts/x.md"), cwd="/tmp", timeout_seconds=5, what="show", repo_url=_REPO)
    assert out == b"file bytes"


async def test_git_nonzero_exit_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stderr=b"fatal: bad object\n", returncode=1)
    _patch_exec(monkeypatch, proc)
    with pytest.raises(GitContentFetchError, match="git show failed"):
        await _git(("show", f"{_SHA_A}:prompts/x.md"), cwd="/tmp", timeout_seconds=5, what="show", repo_url=_REPO)


async def test_git_timeout_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(hang=True)
    _patch_exec(monkeypatch, proc)
    with pytest.raises(GitContentFetchError, match="timed out"):
        await _git(("show", f"{_SHA_A}:prompts/x.md"), cwd="/tmp", timeout_seconds=0.01, what="show", repo_url=_REPO)
    assert proc.kill_called


async def test_fetch_git_content_decodes_utf8_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def _fake_git(args, *, cwd, timeout_seconds, what, repo_url):
        calls.append({"args": args, "what": what, "cwd": cwd})
        return b"PROMPT FROM GIT" if what.startswith("show") else b""

    monkeypatch.setattr(git_content, "_git", _fake_git)
    out = await fetch_git_content(_REPO, _SHA_A, "prompts/x.md")
    assert out == "PROMPT FROM GIT"
    assert calls[0]["what"] == "clone"
    assert calls[0]["args"][0] == "clone"  # type: ignore[index]
    assert calls[1]["what"] == "show prompts/x.md"
    assert calls[1]["args"] == ("show", f"{_SHA_A}:prompts/x.md")
    assert not await asyncio.to_thread(Path(str(calls[0]["cwd"])).exists)


async def test_fetch_git_content_non_utf8_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_git(*_args: object, **_kwargs: object) -> bytes:
        return b"\xff\xfe\xfa"

    monkeypatch.setattr(git_content, "_git", _fake_git)
    with pytest.raises(GitContentFetchError, match="not UTF-8"):
        await fetch_git_content(_REPO, _SHA_A, "prompts/x.md")
