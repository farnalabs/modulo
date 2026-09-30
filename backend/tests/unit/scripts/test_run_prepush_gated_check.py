"""Unit tests for scripts/run_prepush_gated_check.py (the pre-push gate helper).

The gate exists to SKIP expensive pre-push checks when no relevant files
changed, so every failure mode below is a silent-skip trap:

1. Glob semantics — ``fnmatch`` gives ``**`` no special meaning and lets ``*``
   span ``/``, so ``backend/src/**/*.py`` did not match ``backend/src/foo.py``
   and the gate fail-opened whenever only files directly under ``backend/src/``
   changed.  It only appeared to work because every module happens to live
   under ``backend/src/modulo/``.
2. Broken base ref — a missing/stale ``origin/main`` must warn distinctly, not
   masquerade as "no files changed", and (FAR-1353) must NOT skip the gate:
   after a best-effort ``git fetch origin main`` retry the check runs
   unconditionally with a loud warning.
3. Exit codes — a matching change must propagate the underlying command's exit
   code, and a malformed invocation must exit 2.

The three gate branches (undiffable base / diffable-no-match / diffable-match)
are additionally covered end-to-end by running the script as a real
subprocess against throwaway git repositories (see the
``test_gate_end_to_end_*`` block), so the tests fail against the old
fail-open behaviour rather than asserting against mocks.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_prepush_gated_check.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_prepush_gated_check.py)")

_loader = SourceFileLoader("run_prepush_gated_check", str(script_path))
mod = module_from_spec(spec_from_loader("run_prepush_gated_check", _loader))
_loader.exec_module(mod)


# ---------------------------------------------------------------------------
# Glob semantics (gitignore-style `**`, `*`, `?`)
# ---------------------------------------------------------------------------
def test_double_star_matches_file_directly_under_base():
    """Regression (PR #739 review): `**/` must match ZERO directories.

    `fnmatch` treats `*` as spanning `/`, so `backend/src/**/*.py` required a
    literal extra `/` and did NOT match `backend/src/foo.py`.
    """
    assert mod._matches_pattern(["backend/src/foo.py"], "backend/src/**/*.py") == ["backend/src/foo.py"]


def test_double_star_matches_nested_directories():
    assert mod._matches_pattern(["backend/src/modulo/api/x.py"], "backend/src/**/*.py") == [
        "backend/src/modulo/api/x.py"
    ]


def test_double_star_matches_every_depth_at_once():
    files = [
        "backend/src/foo.py",
        "backend/src/modulo/foo.py",
        "backend/src/modulo/api/deep/foo.py",
    ]
    assert mod._matches_pattern(files, "backend/src/**/*.py") == files


def test_trailing_double_star_matches_any_depth():
    files = ["frontend/src/a.ts", "frontend/src/views/B.vue"]
    assert mod._matches_pattern(files, "frontend/src/**") == files


def test_single_star_does_not_cross_path_separator():
    assert not mod._matches_pattern(["frontend/src/views/B.vue"], "frontend/src/*.vue")
    assert mod._matches_pattern(["frontend/src/B.vue"], "frontend/src/*.vue") == ["frontend/src/B.vue"]


def test_question_mark_matches_one_non_separator_character():
    assert mod._matches_pattern(["frontend/src/a.ts"], "frontend/src/?.ts") == ["frontend/src/a.ts"]
    assert not mod._matches_pattern(["frontend/src/ab.ts"], "frontend/src/?.ts")
    assert not mod._matches_pattern(["frontend/src/a/b.ts"], "frontend/src/?.ts")


def test_pattern_is_anchored_to_the_whole_path():
    assert not mod._matches_pattern(["backend/other/src/foo.py"], "backend/src/**/*.py")
    assert not mod._matches_pattern(["xbackend/src/foo.py"], "backend/src/**/*.py")


def test_non_python_files_do_not_match_python_pattern():
    assert not mod._matches_pattern(["backend/src/foo.md"], "backend/src/**/*.py")


def test_windows_separators_are_normalised():
    assert mod._matches_pattern(["backend\\src\\modulo\\foo.py"], "backend/src/**/*.py") == [
        "backend\\src\\modulo\\foo.py"
    ]


# ---------------------------------------------------------------------------
# _git_diff_names: distinct failure signal
# ---------------------------------------------------------------------------
def test_git_diff_names_returns_changed_files():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="a.py\n\nb.py\n", stderr="")
    with patch.object(mod.subprocess, "run", return_value=completed):
        assert mod._git_diff_names() == ["a.py", "b.py"]


def test_git_diff_names_returns_none_when_git_fails(capsys):
    error = subprocess.CalledProcessError(128, ["git"], stderr="fatal: bad revision 'origin/main...HEAD'")
    with patch.object(mod.subprocess, "run", side_effect=error):
        assert mod._git_diff_names() is None
    assert "origin/main" in capsys.readouterr().err


def test_git_diff_names_fetches_origin_main_then_retries_once(capsys):
    """First diff failure triggers ONE best-effort fetch + one diff retry.

    Sequence: diff fails -> `git fetch origin main` -> diff succeeds, so the
    result is the retry's files and the fetch itself is silent on success.
    """
    error = subprocess.CalledProcessError(128, ["git"], stderr="fatal: bad revision 'origin/main...HEAD'")
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="a.py\n", stderr="")
    with patch.object(mod.subprocess, "run", side_effect=[error, ok, ok]) as run:
        assert mod._git_diff_names() == ["a.py"]
    assert run.call_count == 3
    fetch_cmd = run.call_args_list[1].args[0]
    assert fetch_cmd[:3] == ["git", "fetch", "origin"]
    assert run.call_args_list[2].args[0][0:2] == ["git", "diff"]
    err = capsys.readouterr().err
    assert "git fetch origin main` failed" not in err


def test_git_diff_names_gives_up_after_failed_fetch(capsys):
    """Fetch failure must not raise: the diff is retried once, then None."""
    error = subprocess.CalledProcessError(128, ["git"], stderr="fatal: bad revision")
    with patch.object(mod.subprocess, "run", side_effect=error):
        assert mod._git_diff_names() is None
    err = capsys.readouterr().err
    assert "git fetch origin main` failed" in err
    assert "origin/main" in err


# ---------------------------------------------------------------------------
# main(): skip / run / exit-code propagation / usage errors
# ---------------------------------------------------------------------------
_PATTERN = "backend/src/**/*.py"
_ARGV = ["--pattern", _PATTERN, "--", "echo", "hi"]


def _set_argv(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["run_prepush_gated_check.py", *argv])


def test_main_skips_when_no_files_changed(monkeypatch, capsys):
    _set_argv(monkeypatch, _ARGV)
    monkeypatch.setattr(mod, "_git_diff_names", list)
    with patch.object(mod.subprocess, "run") as run:
        assert mod.main() == 0
    run.assert_not_called()
    assert "no files changed" in capsys.readouterr().err


def test_main_skips_when_no_changed_file_matches(monkeypatch, capsys):
    _set_argv(monkeypatch, _ARGV)
    monkeypatch.setattr(mod, "_git_diff_names", lambda: ["docs/readme.md"])
    with patch.object(mod.subprocess, "run") as run:
        assert mod.main() == 0
    run.assert_not_called()
    assert "no files matching" in capsys.readouterr().err


def test_main_runs_command_when_diff_fails(monkeypatch, capsys):
    """FAR-1353 regression: an undiffable base must RUN the gate, not skip it.

    The old code returned 0 without executing the command here, which made a
    disabled gate indistinguishable from a passing one.
    """
    _set_argv(monkeypatch, _ARGV)
    monkeypatch.setattr(mod, "_git_diff_names", lambda: None)
    with patch.object(mod.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        assert mod.main() == 0
    run.assert_called_once_with(["echo", "hi"], check=False)
    err = capsys.readouterr().err
    assert "UNCONDITIONALLY" in err
    assert "origin/main" in err


def test_main_propagates_command_failure_when_diff_fails(monkeypatch):
    """The unconditional run must still propagate the command's exit code."""
    _set_argv(monkeypatch, _ARGV)
    monkeypatch.setattr(mod, "_git_diff_names", lambda: None)
    with patch.object(mod.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=3)
        assert mod.main() == 3


def test_main_runs_command_when_a_file_matches(monkeypatch):
    _set_argv(monkeypatch, _ARGV)
    monkeypatch.setattr(mod, "_git_diff_names", lambda: ["backend/src/modulo/core/x.py"])
    with patch.object(mod.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        assert mod.main() == 0
    run.assert_called_once_with(["echo", "hi"], check=False)


def test_main_propagates_command_failure_exit_code(monkeypatch):
    _set_argv(monkeypatch, _ARGV)
    monkeypatch.setattr(mod, "_git_diff_names", lambda: ["backend/src/modulo/core/x.py"])
    with patch.object(mod.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=3)
        assert mod.main() == 3


def test_main_usage_error_when_pattern_is_missing(monkeypatch, capsys):
    _set_argv(monkeypatch, ["--", "echo", "hi"])
    with patch.object(mod.subprocess, "run") as run:
        assert mod.main() == 2
    run.assert_not_called()
    assert "Usage" in capsys.readouterr().err


def test_main_usage_error_when_command_is_missing(monkeypatch, capsys):
    """Regression: `--pattern X` with no `-- <command>` used to fall through.

    The old parser left ``cmd_start`` at 0 when no ``--`` appeared, so it tried
    to execute ``--pattern X`` as the command.
    """
    _set_argv(monkeypatch, ["--pattern", _PATTERN])
    with patch.object(mod.subprocess, "run") as run:
        assert mod.main() == 2
    run.assert_not_called()
    assert "Usage" in capsys.readouterr().err


def test_main_usage_error_on_unknown_argument(monkeypatch, capsys):
    _set_argv(monkeypatch, ["--verbose", *_ARGV])
    with patch.object(mod.subprocess, "run") as run:
        assert mod.main() == 2
    run.assert_not_called()
    assert "unexpected argument" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# End-to-end: the three gate branches run as a real subprocess against a
# throwaway git repository (no mocks of the script itself).
# ---------------------------------------------------------------------------
_GATE_TIMEOUT_SECONDS = 60


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — test helper, fixed git argv
        ["git", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
        timeout=30,
    )


def _make_repo(tmp_path: Path, *, with_origin: bool = True) -> Path:
    """Throwaway repo with one commit; *with_origin* publishes that commit as
    ``refs/remotes/origin/main`` so the diff base resolves."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "gate-test@example.com")
    _git(repo, "config", "user.name", "Gate Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    if with_origin:
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "update-ref", "refs/remotes/origin/main", sha)
    return repo


def _commit(repo: Path, relpath: str) -> None:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {relpath}")


def _sentinel(marker: Path, exit_code: int) -> list[str]:
    """A real command that writes *marker* then exits with *exit_code*."""
    code = (
        f"import pathlib, sys; pathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8'); sys.exit({exit_code})"
    )
    return [sys.executable, "-c", code]


def _run_gate(repo: Path, cmd: list[str], pattern: str = _PATTERN) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — test helper, fixed argv (real script under test)
        [sys.executable, str(script_path), "--pattern", pattern, "--", *cmd],
        cwd=repo,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=_GATE_TIMEOUT_SECONDS,
    )


def test_gate_end_to_end_undiffable_base_runs_command(tmp_path):
    """FAR-1353: no origin/main ref AND no origin remote -> the gate MUST run.

    Old behaviour: exit 0 with the command never executed, indistinguishable
    from a passing gate.  This test fails against that code (marker absent,
    exit 0 instead of the sentinel's 7).
    """
    repo = _make_repo(tmp_path, with_origin=False)  # base unresolvable, fetch has no remote
    marker = tmp_path / "marker.txt"
    proc = _run_gate(repo, _sentinel(marker, 7))

    assert proc.returncode == 7, proc.stderr
    assert marker.exists()
    err = proc.stderr
    assert "UNCONDITIONALLY" in err
    assert "origin/main" in err


def test_gate_end_to_end_diffable_without_matching_files_skips(tmp_path):
    """Diff computable, changed file does not match -> skip (exit 0), command NOT run."""
    repo = _make_repo(tmp_path)
    _commit(repo, "docs/readme.md")
    marker = tmp_path / "marker.txt"
    proc = _run_gate(repo, _sentinel(marker, 7))

    assert proc.returncode == 0
    assert not marker.exists()
    assert "no files matching" in proc.stderr


def test_gate_end_to_end_diffable_with_matching_file_runs_and_propagates(tmp_path):
    """Diff computable, changed file matches -> command runs, non-zero exit propagates."""
    repo = _make_repo(tmp_path)
    _commit(repo, "backend/src/modulo/core/changed.py")
    marker = tmp_path / "marker.txt"
    proc = _run_gate(repo, _sentinel(marker, 7))

    assert proc.returncode == 7, proc.stderr
    assert marker.exists()
    assert "running check" in proc.stderr


def test_gate_end_to_end_undiffable_base_propagates_success_too(tmp_path):
    """Same branch, sentinel exiting 0: still runs, still exits 0."""
    repo = _make_repo(tmp_path, with_origin=False)
    marker = tmp_path / "marker.txt"
    proc = _run_gate(repo, _sentinel(marker, 0))

    assert proc.returncode == 0, proc.stderr
    assert marker.exists()
    assert "UNCONDITIONALLY" in proc.stderr
