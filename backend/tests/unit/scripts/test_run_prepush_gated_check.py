"""Unit tests for scripts/run_prepush_gated_check.py (the pre-push gate helper).

The gate exists to SKIP expensive pre-push checks when no relevant files
changed, so every failure mode below is a silent-skip trap:

1. Glob semantics — ``fnmatch`` gives ``**`` no special meaning and lets ``*``
   span ``/``, so ``backend/src/**/*.py`` did not match ``backend/src/foo.py``
   and the gate fail-opened whenever only files directly under ``backend/src/``
   changed.  It only appeared to work because every module happens to live
   under ``backend/src/modulo/``.
2. Broken base ref — a missing/stale ``origin/main`` must warn distinctly, not
   masquerade as "no files changed".
3. Exit codes — a matching change must propagate the underlying command's exit
   code, and a malformed invocation must exit 2.
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
    assert mod._matches_pattern(["frontend/src/views/B.vue"], "frontend/src/*.vue") == []
    assert mod._matches_pattern(["frontend/src/B.vue"], "frontend/src/*.vue") == ["frontend/src/B.vue"]


def test_question_mark_matches_one_non_separator_character():
    assert mod._matches_pattern(["frontend/src/a.ts"], "frontend/src/?.ts") == ["frontend/src/a.ts"]
    assert mod._matches_pattern(["frontend/src/ab.ts"], "frontend/src/?.ts") == []
    assert mod._matches_pattern(["frontend/src/a/b.ts"], "frontend/src/?.ts") == []


def test_pattern_is_anchored_to_the_whole_path():
    assert mod._matches_pattern(["backend/other/src/foo.py"], "backend/src/**/*.py") == []
    assert mod._matches_pattern(["xbackend/src/foo.py"], "backend/src/**/*.py") == []


def test_non_python_files_do_not_match_python_pattern():
    assert mod._matches_pattern(["backend/src/foo.md"], "backend/src/**/*.py") == []


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


def test_main_warns_and_skips_when_diff_fails(monkeypatch, capsys):
    _set_argv(monkeypatch, _ARGV)
    monkeypatch.setattr(mod, "_git_diff_names", lambda: None)
    with patch.object(mod.subprocess, "run") as run:
        assert mod.main() == 0
    run.assert_not_called()
    assert "skipping check" in capsys.readouterr().err


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
