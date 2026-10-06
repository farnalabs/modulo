"""Unit tests for scripts/run_check_uv_lock.py (uv lockfile freshness gate).

The gate shells out to ``uv lock --project backend --check`` and maps the exit
code 1:1. These tests pin the exact command/cwd and both verdict arms by
substituting ``subprocess.run``.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_check_uv_lock.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_check_uv_lock.py)")

_loader = SourceFileLoader("run_check_uv_lock", str(script_path))
mod = module_from_spec(spec_from_loader("run_check_uv_lock", _loader))
_loader.exec_module(mod)


class _Completed:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_run(monkeypatch, result: _Completed) -> list[tuple[list[str], dict]]:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return result

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return calls


def test_main_returns_zero_on_fresh_lockfile(monkeypatch, capsys):
    calls = _install_run(monkeypatch, _Completed(0, stdout="Resolved 42 packages\n"))
    monkeypatch.setattr(mod, "REPO_ROOT", "/repo/root")

    assert mod.main() == 0

    assert calls[0][0] == ["uv", "lock", "--project", "backend", "--check"]
    assert calls[0][1]["cwd"] == "/repo/root"
    assert calls[0][1]["check"] is False
    out = capsys.readouterr().out
    assert "Resolved 42 packages" in out


def test_main_returns_one_on_stale_lockfile(monkeypatch, capsys):
    _install_run(monkeypatch, _Completed(1, stderr="error: lockfile is stale\n"))

    assert mod.main() == 1

    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "lockfile is stale" in err


def test_main_forwards_stderr_on_success(monkeypatch, capsys):
    _install_run(monkeypatch, _Completed(0, stderr="using cached resolution\n"))

    assert mod.main() == 0

    err = capsys.readouterr().err
    assert "using cached resolution" in err
    assert "fresh with no dependency conflicts" in err


def test_main_prints_no_output_when_streams_empty(monkeypatch, capsys):
    _install_run(monkeypatch, _Completed(0))

    assert mod.main() == 0

    captured = capsys.readouterr()
    assert not captured.out
    assert "Checking uv lockfile freshness" in captured.err
