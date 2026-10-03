#!/usr/bin/env python3
"""Unit tests for scripts/run_check_merge_conflict.py.

Run from the repo root:
    uv run --project backend pytest tests/unit/scripts/test_run_check_merge_conflict.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure the scripts directory is importable.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from run_check_merge_conflict import (  # noqa: E402
    _PREFIX,
    _parse_conflicted_paths,
    _warn,
    main,
)

# ---------------------------------------------------------------------------
# _parse_conflicted_paths
# ---------------------------------------------------------------------------


class TestParseConflictedPaths:
    def test_empty_output_means_no_conflicts(self):
        result = _parse_conflicted_paths("")
        assert result == []

    def test_whitespace_only_output_means_no_conflicts(self):
        result = _parse_conflicted_paths("   \n  \n")
        assert result == []

    def test_tree_oid_alone_means_no_conflicts(self):
        result = _parse_conflicted_paths("f1e2d3c4b5a6\n")
        assert result == []

    def test_trailing_paths_are_returned_in_order(self):
        stdout = "f1e2d3c4b5a6\nbackend/src/modulo/api/routes/foo.py\nfrontend/src/index.ts\n"
        assert _parse_conflicted_paths(stdout) == [
            "backend/src/modulo/api/routes/foo.py",
            "frontend/src/index.ts",
        ]

    def test_blank_lines_in_payload_are_skipped(self):
        stdout = "f1e2d3c4b5a6\n\nsrc/foo.py\n\n"
        assert _parse_conflicted_paths(stdout) == ["src/foo.py"]


# ---------------------------------------------------------------------------
# _warn
# ---------------------------------------------------------------------------


class TestWarn:
    def test_warn_prints_prefixed_message_to_stderr(self, capsys):
        _warn("boom")
        captured = capsys.readouterr()
        assert captured.err == f"{_PREFIX} boom\n"
        assert not captured.out


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMainDetachedHead:
    def test_unresolved_branch_reports_and_passes(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "origin\n", ""),  # git remote
                (128, "", "fatal: not a git repository"),  # rev-parse HEAD
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 0
        captured = capsys.readouterr()
        assert "detached HEAD / unresolved branch - skipping (pass)" in captured.err

    def test_branch_named_head_reports_and_passes(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "origin\n", ""),  # git remote
                (0, "HEAD\n", ""),  # rev-parse --abbrev-ref HEAD
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 0
        captured = capsys.readouterr()
        assert "detached HEAD / unresolved branch - skipping (pass)" in captured.err


class TestMainFirstPushRule:
    def test_push_already_configured_short_circuits(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "origin\n", ""),  # git remote
                (0, "feature\n", ""),  # rev-parse HEAD
                (0, "origin/feature\n", ""),  # @{u}
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 0
        captured = capsys.readouterr()
        assert "'feature' already has upstream 'origin/feature' - pass (first-push rule only)." in captured.err

    def test_upstream_of_origin_main_is_treated_as_first_push(self, monkeypatch, capsys):
        # Worktree-creation flows pin upstream to origin/main even though the
        # branch has never been pushed to its own remote branch.
        def _fake_git(*args):
            if args == ("remote",):
                return (0, "origin\n", "")
            if args == ("rev-parse", "--abbrev-ref", "HEAD"):
                return (0, "feature\n", "")
            if args == ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                return (0, "origin/main\n", "")
            if args == ("fetch", "origin"):
                return (0, "", "")
            if args == ("rev-parse", "--verify", "--quiet", "origin/main"):
                return (0, "", "")
            if args == ("merge-base", "--is-ancestor", "origin/main", "HEAD"):
                return (1, "", "")
            if args == ("merge-tree", "--write-tree", "--name-only", "origin/main", "HEAD"):
                return (1, "abc123\ntests/unit/test_foo.py\n", "")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr("run_check_merge_conflict._run_git", _fake_git)
        assert main() == 1
        captured = capsys.readouterr()
        assert "'feature' conflicts with origin/main" in captured.err


class TestMainOriginUnavailable:
    def test_no_origin_remote_passes_open(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "upstream\n", ""),  # git remote — no origin
                (0, "feature\n", ""),  # rev-parse HEAD
                (128, "", "fatal: no such branch"),  # @{u} (first push)
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 0
        captured = capsys.readouterr()
        assert "no 'origin' remote available - skipping merge-conflict check (pass)." in captured.err

    def test_fetch_failure_passes_open(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "origin\n", ""),  # git remote
                (0, "feature\n", ""),  # rev-parse HEAD
                (128, "", "fatal: no upstream"),  # @{u} (first push)
                (128, "", "fatal: could not read from remote"),  # fetch origin
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 0
        captured = capsys.readouterr()
        assert "'git fetch origin' failed" in captured.err
        assert "could not read from remote" in captured.err

    def test_unknown_origin_main_passes_open(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "origin\n", ""),  # git remote
                (0, "feature\n", ""),  # rev-parse HEAD
                (128, "", "fatal: no upstream"),  # @{u} (first push)
                (0, "", ""),  # fetch origin
                (128, "", "fatal: Needed a single revision"),  # rev-parse --verify origin/main
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 0
        captured = capsys.readouterr()
        assert "origin/main does not resolve locally - skipping merge-conflict check (pass)." in captured.err


class TestMainConflictResolution:
    def test_merge_base_ancestor_is_a_pass(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "origin\n", ""),  # git remote
                (0, "feature\n", ""),  # rev-parse HEAD
                (128, "", ""),  # @{u} (first push)
                (0, "", ""),  # fetch origin
                (0, "", ""),  # rev-parse --verify origin/main
                (0, "", ""),  # merge-base --is-ancestor
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 0
        captured = capsys.readouterr()
        assert "'feature' is based on latest origin/main - pass." in captured.err

    def test_clean_merge_tree_is_a_pass(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "origin\n", ""),  # git remote
                (0, "feature\n", ""),  # rev-parse HEAD
                (128, "", ""),  # @{u} (first push)
                (0, "", ""),  # fetch origin
                (0, "", ""),  # rev-parse --verify origin/main
                (1, "", ""),  # merge-base --is-ancestor
                (0, "abc123\n", ""),  # merge-tree
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 0
        captured = capsys.readouterr()
        assert "'feature' merges cleanly with origin/main - pass." in captured.err

    def test_conflicting_paths_are_reported_and_fail_closed(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "origin\n", ""),  # git remote
                (0, "feature\n", ""),  # rev-parse HEAD
                (128, "", ""),  # @{u} (first push)
                (0, "", ""),  # fetch origin
                (0, "", ""),  # rev-parse --verify origin/main
                (1, "", ""),  # merge-base --is-ancestor
                (
                    1,
                    "abc123\nbackend/src/modulo/api/routes/triggers.py\nfrontend/src/manifest.yaml\n",
                    "",
                ),  # merge-tree
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 1
        captured = capsys.readouterr()
        assert "FAILED - 'feature' conflicts with origin/main" in captured.err
        assert "backend/src/modulo/api/routes/triggers.py" in captured.err
        assert "frontend/src/manifest.yaml" in captured.err

    def test_conflict_without_paths_falls_back_to_generic_message(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "origin\n", ""),  # git remote
                (0, "feature\n", ""),  # rev-parse HEAD
                (128, "", ""),  # @{u} (first push)
                (0, "", ""),  # fetch origin
                (0, "", ""),  # rev-parse --verify origin/main
                (1, "", ""),  # merge-base --is-ancestor
                (1, "abc123\n", ""),  # merge-tree — tree oid only
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 1
        captured = capsys.readouterr()
        assert "(conflict detail is in git's output — see stderr above)" in captured.err

    def test_unexpected_merge_tree_code_fails_open(self, monkeypatch, capsys):
        _run_git = MagicMock(
            side_effect=[
                (0, "origin\n", ""),  # git remote
                (0, "feature\n", ""),  # rev-parse HEAD
                (128, "", ""),  # @{u} (first push)
                (0, "", ""),  # fetch origin
                (0, "", ""),  # rev-parse --verify origin/main
                (1, "", ""),  # merge-base --is-ancestor
                (2, "", ""),  # merge-tree — unrelated histories / old git
            ]
        )
        monkeypatch.setattr("run_check_merge_conflict._run_git", _run_git)
        assert main() == 0
        captured = capsys.readouterr()
        assert "git merge-tree exited with unexpected code 2" in captured.err
