"""Unit tests for the new ``_validate_restore_args`` / ``_print_restore_mode`` helpers in scripts/restore.py."""

from __future__ import annotations

import argparse
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "restore.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/restore.py)")


_loader = SourceFileLoader("restore_helper_test", str(script_path))
mod = module_from_spec(spec_from_loader("restore_helper_test", _loader))
_loader.exec_module(mod)


def _args(tmp_path: Path, **overrides) -> argparse.Namespace:
    base: dict[str, object] = {
        "input": str(tmp_path / "backup.tar.gz.enc"),
        "dry_run": False,
        "full": False,
        "data_only": False,
        "config_only": False,
        "passphrase": None,
        "db_url": None,
        "pg_restore": "pg_restore",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_validate_missing_input_exits(tmp_path):
    args = _args(tmp_path)  # file does not exist
    with pytest.raises(SystemExit):
        mod._validate_restore_args(args)


def test_validate_conflicting_modes_exits(tmp_path):
    f = tmp_path / "backup.tar.gz.enc"
    f.write_bytes(b"x")
    args = _args(tmp_path, full=True, data_only=True)
    with pytest.raises(SystemExit):
        mod._validate_restore_args(args)


def test_validate_no_mode_exits(tmp_path):
    f = tmp_path / "backup.tar.gz.enc"
    f.write_bytes(b"x")
    args = _args(tmp_path)  # no mode, no dry_run
    with pytest.raises(SystemExit):
        mod._validate_restore_args(args)


def test_validate_dry_run_ok(tmp_path):
    f = tmp_path / "backup.tar.gz.enc"
    f.write_bytes(b"x")
    args = _args(tmp_path, dry_run=True)
    assert mod._validate_restore_args(args) is None  # no exit


def test_validate_full_ok(tmp_path):
    f = tmp_path / "backup.tar.gz.enc"
    f.write_bytes(b"x")
    args = _args(tmp_path, full=True)
    assert mod._validate_restore_args(args) is None  # no exit


def test_print_restore_mode_dry_run(tmp_path, capsys):
    args = _args(tmp_path, dry_run=True)
    mod._print_restore_mode(args)
    assert "Dry-run mode" in capsys.readouterr().out


def test_print_restore_mode_data_only(tmp_path, capsys):
    args = _args(tmp_path, data_only=True)
    mod._print_restore_mode(args)
    assert "Data-only restore mode" in capsys.readouterr().out


def test_print_restore_mode_config_only(tmp_path, capsys):
    args = _args(tmp_path, config_only=True)
    mod._print_restore_mode(args)
    assert "Config-only restore mode" in capsys.readouterr().out


def test_print_restore_mode_full(tmp_path, capsys):
    args = _args(tmp_path, full=True)
    mod._print_restore_mode(args)
    assert "Full restore mode" in capsys.readouterr().out
