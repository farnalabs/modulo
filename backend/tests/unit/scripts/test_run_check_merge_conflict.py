"""Unit tests for scripts/run_check_merge_conflict.py."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_check_merge_conflict.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_check_merge_conflict.py)")

_loader = SourceFileLoader("run_check_merge_conflict", str(script_path))
mod = module_from_spec(spec_from_loader("run_check_merge_conflict", _loader))
_loader.exec_module(mod)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_git(fakes: dict[tuple[str, ...], tuple[int, str, str]]):
    """Return a callable that behaves like ``_run_git`` but returns canned
    responses keyed on the argument tuple.

    If a call is not in *fakes*, it falls through to ``subprocess.run`` so
    the test does not break on an unexpected invocation.
    """
    import subprocess as _sp

    calls: list[tuple[str, ...]] = []

    def _fake(*args: str) -> tuple[int, str, str]:
        calls.append(args)
        if args in fakes:
            return fakes[args]
        # Fallback: run real git (should rarely happen in unit tests).
        proc = _sp.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    return _fake, calls


# Common git invocations used by the script (in order).
_REMOTE = ("remote",)
_REV_PARSE_HEAD = ("rev-parse", "--abbrev-ref", "HEAD")
_REV_PARSE_UPSTREAM = (
    "rev-parse",
    "--abbrev-ref",
    "--symbolic-full-name",
    "@{u}",
)
_FETCH = ("fetch", "origin")
_REV_PARSE_ORIGIN_MAIN = ("rev-parse", "--verify", "--quiet", "origin/main")
_MERGE_BASE_IS_ANCESTOR = ("merge-base", "--is-ancestor", "origin/main", "HEAD")
_MERGE_TREE = (
    "merge-tree",
    "--write-tree",
    "--name-only",
    "origin/main",
    "HEAD",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCleanMerge:
    """merge-tree rc=0 -> pass."""

    def test_returns_zero(self, capsys: pytest.CaptureFixture[str]):
        fake, calls = _make_fake_git(
            {
                _REMOTE: (0, "origin\n", ""),
                _REV_PARSE_HEAD: (0, "feature-x\n", ""),
                _REV_PARSE_UPSTREAM: (128, "", "fatal: ..."),
                _FETCH: (0, "", ""),
                _REV_PARSE_ORIGIN_MAIN: (0, "", ""),
                _MERGE_BASE_IS_ANCESTOR: (1, "", ""),
                _MERGE_TREE: (0, "deadbeef\n", ""),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 0
            assert _MERGE_TREE in calls
        finally:
            monkeypatch.undo()


class TestConflict:
    """merge-tree rc=1 -> fail with conflicted paths in stderr."""

    def test_returns_one_and_lists_paths(self, capsys: pytest.CaptureFixture[str]):
        stdout = "deadbeef\nfoo.py\nbar.py\n"
        fake, _ = _make_fake_git(
            {
                _REMOTE: (0, "origin\n", ""),
                _REV_PARSE_HEAD: (0, "feature-x\n", ""),
                _REV_PARSE_UPSTREAM: (128, "", "fatal: ..."),
                _FETCH: (0, "", ""),
                _REV_PARSE_ORIGIN_MAIN: (0, "", ""),
                _MERGE_BASE_IS_ANCESTOR: (1, "", ""),
                _MERGE_TREE: (1, stdout, ""),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 1
            captured = capsys.readouterr()
            assert "foo.py" in captured.err
            assert "bar.py" in captured.err
            assert "FAILED" in captured.err
        finally:
            monkeypatch.undo()


class TestConflictNoPathsParsed:
    """merge-tree rc=1 but parse yields no paths -> still fail."""

    def test_returns_one(self, capsys: pytest.CaptureFixture[str]):
        fake, _ = _make_fake_git(
            {
                _REMOTE: (0, "origin\n", ""),
                _REV_PARSE_HEAD: (0, "feature-x\n", ""),
                _REV_PARSE_UPSTREAM: (128, "", "fatal: ..."),
                _FETCH: (0, "", ""),
                _REV_PARSE_ORIGIN_MAIN: (0, "", ""),
                _MERGE_BASE_IS_ANCESTOR: (1, "", ""),
                _MERGE_TREE: (1, "deadbeef\n", ""),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 1
            captured = capsys.readouterr()
            assert "FAILED" in captured.err
            assert "conflict detail is in git" in captured.err
        finally:
            monkeypatch.undo()


class TestUnexpectedMergeTreeExit:
    """merge-tree rc != 0 and rc != 1 -> fail-open."""

    def test_returns_zero_on_rc_128(self, capsys: pytest.CaptureFixture[str]):
        fake, _ = _make_fake_git(
            {
                _REMOTE: (0, "origin\n", ""),
                _REV_PARSE_HEAD: (0, "feature-x\n", ""),
                _REV_PARSE_UPSTREAM: (128, "", "fatal: ..."),
                _FETCH: (0, "", ""),
                _REV_PARSE_ORIGIN_MAIN: (0, "", ""),
                _MERGE_BASE_IS_ANCESTOR: (1, "", ""),
                _MERGE_TREE: (128, "", "fatal: ..."),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 0
        finally:
            monkeypatch.undo()


class TestOriginMainAlreadyAncestor:
    """origin/main is ancestor of HEAD -> pass, merge-tree never called."""

    def test_returns_zero_and_skips_merge_tree(self, capsys: pytest.CaptureFixture[str]):
        fake, calls = _make_fake_git(
            {
                _REMOTE: (0, "origin\n", ""),
                _REV_PARSE_HEAD: (0, "feature-x\n", ""),
                _REV_PARSE_UPSTREAM: (128, "", "fatal: ..."),
                _FETCH: (0, "", ""),
                _REV_PARSE_ORIGIN_MAIN: (0, "", ""),
                _MERGE_BASE_IS_ANCESTOR: (0, "", ""),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 0
            assert _MERGE_TREE not in calls
        finally:
            monkeypatch.undo()


class TestAlreadyPushed:
    """Branch already has an upstream -> pass without fetching."""

    def test_returns_zero(self, capsys: pytest.CaptureFixture[str]):
        fake, calls = _make_fake_git(
            {
                _REMOTE: (0, "origin\n", ""),
                _REV_PARSE_HEAD: (0, "feature-x\n", ""),
                _REV_PARSE_UPSTREAM: (0, "origin/feature-x\n", ""),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 0
            assert _FETCH not in calls
        finally:
            monkeypatch.undo()


class TestFetchFails:
    """git fetch origin fails -> fail-open."""

    def test_returns_zero(self, capsys: pytest.CaptureFixture[str]):
        fake, _ = _make_fake_git(
            {
                _REMOTE: (0, "origin\n", ""),
                _REV_PARSE_HEAD: (0, "feature-x\n", ""),
                _REV_PARSE_UPSTREAM: (128, "", "fatal: ..."),
                _FETCH: (1, "", "fatal: unable to access"),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 0
        finally:
            monkeypatch.undo()


class TestDetachedHead:
    """Detached HEAD -> pass."""

    def test_returns_zero(self, capsys: pytest.CaptureFixture[str]):
        fake, _ = _make_fake_git(
            {
                _REMOTE: (0, "origin\n", ""),
                _REV_PARSE_HEAD: (0, "HEAD\n", ""),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 0
        finally:
            monkeypatch.undo()


class TestNoOriginRemote:
    """No 'origin' remote -> pass."""

    def test_returns_zero(self, capsys: pytest.CaptureFixture[str]):
        fake, _ = _make_fake_git(
            {
                _REMOTE: (0, "upstream\n", ""),
                _REV_PARSE_HEAD: (0, "feature-x\n", ""),
                _REV_PARSE_UPSTREAM: (128, "", "fatal: ..."),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 0
        finally:
            monkeypatch.undo()


class TestUpstreamIsOriginMain:
    """Upstream == origin/main is treated as first push."""

    def test_continues_to_fetch(self, capsys: pytest.CaptureFixture[str]):
        fake, calls = _make_fake_git(
            {
                _REMOTE: (0, "origin\n", ""),
                _REV_PARSE_HEAD: (0, "feature-x\n", ""),
                _REV_PARSE_UPSTREAM: (0, "origin/main\n", ""),
                _FETCH: (0, "", ""),
                _REV_PARSE_ORIGIN_MAIN: (0, "", ""),
                _MERGE_BASE_IS_ANCESTOR: (0, "", ""),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 0
            assert _FETCH in calls
            assert _MERGE_BASE_IS_ANCESTOR in calls
        finally:
            monkeypatch.undo()


class TestParseConflictedPaths:
    """Unit test the _parse_conflicted_paths helper."""

    def test_normal_output(self):
        assert mod._parse_conflicted_paths("deadbeef\nfoo.py\nbar.py\n") == [
            "foo.py",
            "bar.py",
        ]

    def test_single_path(self):
        assert mod._parse_conflicted_paths("deadbeef\nonly.py\n") == ["only.py"]

    def test_no_paths(self):
        assert mod._parse_conflicted_paths("deadbeef\n") == []

    def test_empty(self):
        assert mod._parse_conflicted_paths("") == []

    def test_trailing_newlines(self):
        assert mod._parse_conflicted_paths("deadbeef\na.py\nb.py\n\n\n") == [
            "a.py",
            "b.py",
        ]


class TestOriginMainDoesNotResolve:
    """origin/main rev-parse fails -> fail-open."""

    def test_returns_zero(self, capsys: pytest.CaptureFixture[str]):
        fake, _ = _make_fake_git(
            {
                _REMOTE: (0, "origin\n", ""),
                _REV_PARSE_HEAD: (0, "feature-x\n", ""),
                _REV_PARSE_UPSTREAM: (128, "", "fatal: ..."),
                _FETCH: (0, "", ""),
                _REV_PARSE_ORIGIN_MAIN: (128, "", "fatal: ..."),
            }
        )
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_run_git", fake)
        try:
            assert mod.main() == 0
        finally:
            monkeypatch.undo()
