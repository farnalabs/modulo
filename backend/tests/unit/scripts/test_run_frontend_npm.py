"""Unit tests for scripts/run_frontend_npm.py.

The ESLint pre-commit hook sets ``pass_filenames: true``, so the script must
lint exactly the staged frontend files. Two regressions motivated these tests
(PR #753 review):

1. ``FRONTEND_DIR`` was a ``str``, so ``FRONTEND_DIR / "node_modules"`` raised
   ``TypeError: unsupported operand type(s) for /: 'str' and 'str'`` and the
   hook crashed for every staged frontend file (CI was green because
   pre-commit hooks do not run there).
2. pre-commit passes repo-root-relative paths, but eslint runs with
   ``cwd=frontend``; without translation eslint failed with
   ``No files matching the pattern "frontend/src/main.ts" were found``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

for _parent in Path(__file__).resolve().parents:
    _script_path = _parent / "scripts" / "run_frontend_npm.py"
    if _script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_frontend_npm.py)")

_loader = SourceFileLoader("run_frontend_npm", str(_script_path))
mod = module_from_spec(spec_from_loader("run_frontend_npm", _loader))
_loader.exec_module(mod)


def _patch_constants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "FRONTEND_DIR", tmp_path / "frontend")


def _patch_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripts: dict[str, str] | None = None,
) -> Path:
    frontend = tmp_path / "frontend"
    frontend.mkdir(parents=True, exist_ok=True)
    package_json = frontend / "package.json"
    package_json.write_text(
        json.dumps({"scripts": scripts or {"lint": "eslint src"}}),
        encoding="utf-8",
    )
    _patch_constants(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "PACKAGE_JSON", package_json)
    # The package-manager lookup precedes the eslint path; pin it so the test is
    # deterministic regardless of what is installed on the host.
    monkeypatch.setattr(mod, "find_package_manager", lambda: "pnpm")
    return frontend


def _recording_run(
    calls: list[tuple[list[str], dict[str, object]]],
) -> object:
    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    return run


# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------
def test_frontend_relative_paths_translates_frontend_paths(tmp_path, monkeypatch):
    _patch_constants(tmp_path, monkeypatch)
    assert mod.frontend_relative_paths(["frontend/src/main.ts", "frontend/src/App.vue"]) == [
        "src/main.ts",
        "src/App.vue",
    ]


def test_frontend_relative_paths_skips_non_frontend_paths(tmp_path, monkeypatch):
    _patch_constants(tmp_path, monkeypatch)
    assert mod.frontend_relative_paths(
        [
            "docs/api/examples/auth-login/js.js",
            "tests/performance/foo.js",
            "frontend/src/main.ts",
        ]
    ) == ["src/main.ts"]


def test_frontend_relative_paths_handles_absolute_paths(tmp_path, monkeypatch):
    _patch_constants(tmp_path, monkeypatch)
    assert mod.frontend_relative_paths(
        [
            str(tmp_path / "frontend" / "src" / "main.ts"),
            str(tmp_path / "docs" / "api.js"),
        ]
    ) == ["src/main.ts"]


# ---------------------------------------------------------------------------
# main() - extra args (the ESLint hook path)
# ---------------------------------------------------------------------------
def test_main_lint_extra_args_invokes_eslint_binary(tmp_path, monkeypatch):
    """Regression: `FRONTEND_DIR / "node_modules"` crashed before this fix."""
    frontend = _patch_repo(tmp_path, monkeypatch)
    eslint_bin = frontend / "node_modules" / ".bin" / "eslint"
    eslint_bin.parent.mkdir(parents=True)
    eslint_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["run_frontend_npm.py", "lint", "frontend/src/main.ts"])

    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(mod.subprocess, "run", _recording_run(calls))

    assert mod.main() == 0
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == [str(eslint_bin), "--cache", "--cache-location", ".cache/eslint", "src/main.ts"]
    assert kwargs["cwd"] == frontend


def test_main_lint_skips_when_no_frontend_files(tmp_path, monkeypatch):
    _patch_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_frontend_npm.py", "lint", "docs/api/examples/auth-login/js.js", "tests/performance/foo.js"],
    )

    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(mod.subprocess, "run", _recording_run(calls))

    assert mod.main() == 0
    assert calls == []


def test_main_win32_npx_fallback_wraps_cmd_exe(tmp_path, monkeypatch):
    _patch_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["run_frontend_npm.py", "lint", "frontend/src/main.ts"])
    monkeypatch.setattr(mod.sys, "platform", "win32")

    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(mod.subprocess, "run", _recording_run(calls))

    assert mod.main() == 0
    cmd, _kwargs = calls[0]
    assert cmd[:3] == ["cmd.exe", "/c", "npx"]
    assert cmd[-1] == "src/main.ts"


# ---------------------------------------------------------------------------
# main() - no extra args (the existing pnpm script path)
# ---------------------------------------------------------------------------
def test_main_without_extra_args_runs_package_manager_script(tmp_path, monkeypatch):
    _patch_repo(tmp_path, monkeypatch, scripts={"type-check": "vue-tsc --noEmit"})
    monkeypatch.setattr(sys, "argv", ["run_frontend_npm.py", "type-check"])
    monkeypatch.setattr(mod.sys, "platform", "linux")

    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(mod.subprocess, "run", _recording_run(calls))

    assert mod.main() == 0
    assert calls[0][0] == ["pnpm", "run", "type-check"]


def test_main_skips_when_script_absent(tmp_path, monkeypatch):
    _patch_repo(tmp_path, monkeypatch, scripts={"lint": "eslint src"})
    monkeypatch.setattr(sys, "argv", ["run_frontend_npm.py", "type-check"])

    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(mod.subprocess, "run", _recording_run(calls))

    assert mod.main() == 0
    assert calls == []
