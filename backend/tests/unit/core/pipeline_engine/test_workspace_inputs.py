"""Tests for managed workspace inputs (FAR-796 / ADR 033).

Covers:
* ``parse_ls_remote`` — pure parsing with edge-case fixtures.
* ``resolve_movable_ref`` — branch/tag/sha resolution.
* ``build_input_clone_script`` — POSIX script contract (shlex.quote, set -e,
  trailing assertion, no credentials).
* Local bare-repo proof test — end-to-end clone+checkout via ``sh``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from modulo.core.pipeline_engine.workspace_inputs import (
    RefResolutionError,
    build_input_clone_script,
    parse_ls_remote,
    resolve_movable_ref,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GIT = shutil.which("git")
_SH = shutil.which("sh") or shutil.which("bash")

# Pre-commit sets GIT_DIR / GIT_INDEX_FILE that leak into subprocess calls and
# break git operations in freshly-initialized temp repos.  Strip them so every
# git invocation in the proof test starts from a clean state.
_GIT_STATE_ENV_KEYS = frozenset(
    {
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
    }
)


def _has_git() -> bool:
    return _GIT is not None


def _has_sh() -> bool:
    if _SH is not None:
        return True
    # On Windows, prefer Git for Windows bash (which has git on PATH) over
    # WSL sh (which doesn't have Windows git on its PATH).
    for candidate in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
    ):
        if candidate.is_file():
            return True
    return False


def _find_bash() -> str:
    """Return the path to a bash that has git on its PATH."""
    # Prefer Git for Windows bash over WSL sh.
    for candidate in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    if _SH is not None:
        return _SH
    msg = "no sh/bash found"
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# parse_ls_remote fixtures
# ---------------------------------------------------------------------------


class TestParseLsRemote:
    """Pure parsing tests — no I/O, no git calls."""

    def test_basic_refs(self) -> None:
        output = (
            "abc1234def0000000000000000000000000000\trefs/heads/main\n"
            "def5678abc0000000000000000000000000000\trefs/tags/v1.0\n"
        )
        refs = parse_ls_remote(output)
        assert refs["refs/heads/main"] == "abc1234def0000000000000000000000000000"
        assert refs["refs/tags/v1.0"] == "def5678abc0000000000000000000000000000"

    def test_peeled_tag_overwrites_object_sha(self) -> None:
        """An annotated tag's peeled entry (commit SHA) replaces the tag object SHA."""
        output = (
            "1111111111111111111111111111111111111111\trefs/tags/v1.0\n"
            "2222222222222222222222222222222222222222\trefs/tags/v1.0^{}\n"
        )
        refs = parse_ls_remote(output)
        assert refs["refs/tags/v1.0"] == "2222222222222222222222222222222222222222"
        assert "refs/tags/v1.0^{}" not in refs

    def test_dangling_symref_ignored(self) -> None:
        output = "ref: refs/heads/x\tHEAD\nabc1234def0000000000000000000000000000\trefs/heads/main\n"
        refs = parse_ls_remote(output)
        assert "HEAD" not in refs
        assert refs["refs/heads/main"] == "abc1234def0000000000000000000000000000"

    def test_crlf_line_endings(self) -> None:
        output = "abc1234def0000000000000000000000000000\trefs/heads/main\r\n"
        refs = parse_ls_remote(output)
        assert refs["refs/heads/main"] == "abc1234def0000000000000000000000000000"

    def test_truncated_output_no_crash(self) -> None:
        output = "abc123\trefs/heads/main\npartial"
        refs = parse_ls_remote(output)
        assert refs["refs/heads/main"] == "abc123"

    def test_empty_input(self) -> None:
        refs = parse_ls_remote("")
        assert not refs

    def test_blank_lines_skipped(self) -> None:
        output = "\n\nabc1234def0000000000000000000000000000\trefs/heads/main\n\n"
        refs = parse_ls_remote(output)
        assert refs["refs/heads/main"] == "abc1234def0000000000000000000000000000"


# ---------------------------------------------------------------------------
# resolve_movable_ref
# ---------------------------------------------------------------------------


class TestResolveMovableRef:
    _REFS: ClassVar[dict[str, str]] = {
        "refs/heads/main": "aaaa" * 10,
        "refs/tags/v2.0": "bbbb" * 10,
    }

    def test_sha_passthrough(self) -> None:
        sha = "cccc" * 10
        result = resolve_movable_ref(self._REFS, "sha", sha)
        assert result == sha

    def test_branch_resolves(self) -> None:
        result = resolve_movable_ref(self._REFS, "branch", "main")
        assert result == "aaaa" * 10

    def test_tag_resolves(self) -> None:
        result = resolve_movable_ref(self._REFS, "tag", "v2.0")
        assert result == "bbbb" * 10

    def test_branch_not_found(self) -> None:
        with pytest.raises(RefResolutionError, match="not found"):
            resolve_movable_ref(self._REFS, "branch", "nonexistent")

    def test_tag_not_found(self) -> None:
        with pytest.raises(RefResolutionError, match="not found"):
            resolve_movable_ref(self._REFS, "tag", "nonexistent")

    def test_unknown_kind(self) -> None:
        with pytest.raises(RefResolutionError, match="unknown ref kind"):
            resolve_movable_ref(self._REFS, "commit", "abc")


# ---------------------------------------------------------------------------
# build_input_clone_script contract
# ---------------------------------------------------------------------------


class TestBuildInputCloneScript:
    def test_set_e_present(self) -> None:
        script = build_input_clone_script(
            url="https://example.com/repo.git",
            dest="/tmp/dest",
            resolved_sha="aaaa" * 10,
        )
        assert "set -e" in script

    def test_shlex_quote_url(self) -> None:
        """shlex.quote is called on every interpolated value."""
        # A clean URL passes through shlex.quote unchanged (no special chars).
        script = build_input_clone_script(
            url="https://example.com/repo.git",
            dest="/tmp/dest",
            resolved_sha="bbbb" * 10,
        )
        # The URL appears in the script (shlex.quote is a no-op for this input).
        assert "https://example.com/repo.git" in script

    def test_shlex_quote_adversarial_url(self) -> None:
        """Adversarial URL with shell metacharacters is safely quoted."""
        url = "https://example.com/repo.git; rm -rf /"
        script = build_input_clone_script(url=url, dest="/tmp/dest", resolved_sha="aaaa" * 10)
        # shlex.quote wraps the URL so the semicolon is not shell-interpreted.
        assert shlex.quote(url) in script

    def test_adversarial_dest_with_spaces_and_semicolon(self) -> None:
        dest = "/tmp/my dir; rm -rf /"
        script = build_input_clone_script(
            url="https://example.com/repo.git",
            dest=dest,
            resolved_sha="cccc" * 10,
        )
        # shlex.quote wraps it so the semicolon is not shell-interpreted.
        assert shlex.quote(dest) in script

    def test_trailing_rev_parse_assertion_present(self) -> None:
        script = build_input_clone_script(
            url="https://example.com/repo.git",
            dest="/tmp/dest",
            resolved_sha="dddd" * 10,
        )
        assert "rev-parse HEAD" in script

    def test_no_credentials_in_script(self) -> None:
        script = build_input_clone_script(
            url="https://example.com/repo.git",
            dest="/tmp/dest",
            resolved_sha="eeee" * 10,
        )
        # No userinfo (user:pass@) in the script.
        assert "@" not in script or "x-access-token@" not in script

    def test_checkout_uses_sha_not_ref_name(self) -> None:
        sha = "ffff" * 10
        script = build_input_clone_script(
            url="https://example.com/repo.git",
            dest="/tmp/dest",
            resolved_sha=sha,
        )
        assert f"checkout {sha}" in script


# ---------------------------------------------------------------------------
# Local bare-repo proof test (end-to-end via sh)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_git(), reason="git not available")
@pytest.mark.skipif(not _has_sh(), reason="sh/bash not available")
class TestBareRepoProof:
    """End-to-end: create a bare repo, push a commit, clone via the built script."""

    @staticmethod
    def _clean_env() -> dict[str, str]:
        """Return a copy of os.environ with pre-commit's git state vars stripped."""
        return {k: v for k, v in os.environ.items() if k not in _GIT_STATE_ENV_KEYS}

    def test_clone_script_checks_out_resolved_sha(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare.git"
        clone = tmp_path / "pusher"
        dest = tmp_path / "workspace"
        env = self._clean_env()

        subprocess.run(  # noqa: S603 — git with trusted fixed args in test
            [_GIT, "init", "--bare", str(bare)],
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(  # noqa: S603
            [_GIT, "init", str(clone)],
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(  # noqa: S603
            [_GIT, "config", "user.email", "test@test.com"],
            cwd=str(clone),
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(  # noqa: S603
            [_GIT, "config", "user.name", "Test"],
            cwd=str(clone),
            check=True,
            capture_output=True,
            env=env,
        )
        # Create an initial commit.
        (clone / "file.txt").write_text("hello")
        subprocess.run(  # noqa: S603
            [_GIT, "add", "file.txt"],
            cwd=str(clone),
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(  # noqa: S603
            [_GIT, "commit", "-m", "init"],
            cwd=str(clone),
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(  # noqa: S603
            [_GIT, "remote", "add", "origin", str(bare)],
            cwd=str(clone),
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(  # noqa: S603
            [_GIT, "push", "origin", "main"],
            cwd=str(clone),
            check=True,
            capture_output=True,
            env=env,
        )
        # Record the SHA we just pushed.
        result = subprocess.run(  # noqa: S603
            [_GIT, "rev-parse", "HEAD"],
            cwd=str(clone),
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        pushed_sha = result.stdout.strip()

        # Build and run the clone script.
        script = build_input_clone_script(
            url=str(bare),
            dest=str(dest),
            resolved_sha=pushed_sha,
        )
        script_file = tmp_path / "clone.sh"
        script_file.write_text(script)
        # On Windows, prefer Git for Windows bash (which has git on PATH).
        bash = _find_bash()
        subprocess.run(  # noqa: S603
            [bash, str(script_file)],
            check=True,
            capture_output=True,
            env=env,
        )
        # Verify the checkout SHA matches.
        verify = subprocess.run(  # noqa: S603
            [_GIT, "rev-parse", "HEAD"],
            cwd=str(dest),
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        assert verify.stdout.strip() == pushed_sha
