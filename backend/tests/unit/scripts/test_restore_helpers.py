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


def test_restore_from_archive_dry_run(tmp_path, monkeypatch, capsys):
    args = _args(tmp_path, dry_run=True)
    monkeypatch.setattr(mod, "decrypt_archive", lambda *a, **k: None)
    monkeypatch.setattr(mod, "extract_archive", lambda *a, **k: {})
    monkeypatch.setattr(mod, "verify_hashes", lambda *a, **k: True)
    monkeypatch.setattr(mod, "restore_postgres", lambda *a, **k: None)
    monkeypatch.setattr(mod, "restore_config", lambda *a, **k: None)

    mod._restore_from_archive(args, "pass", "db-url")

    out = capsys.readouterr().out
    assert "Dry-run" in out
    # Postgres/config restore must not run in dry-run mode.
    assert "Restore complete" not in out


def test_restore_from_archive_full_restores_data_and_config(tmp_path, monkeypatch, capsys):
    args = _args(tmp_path, full=True)
    monkeypatch.setattr(mod, "decrypt_archive", lambda *a, **k: None)
    monkeypatch.setattr(mod, "extract_archive", lambda *a, **k: {"a": "b"})
    monkeypatch.setattr(mod, "verify_hashes", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(mod, "restore_postgres", lambda *a, **k: calls.append("pg"))
    monkeypatch.setattr(mod, "restore_config", lambda *a, **k: calls.append("cfg"))

    mod._restore_from_archive(args, "pass", "db-url")

    assert "pg" in calls
    assert "cfg" in calls
    assert "Restore complete" in capsys.readouterr().out


def test_restore_from_archive_corrupt_exits(tmp_path, monkeypatch):
    args = _args(tmp_path, full=True)
    monkeypatch.setattr(mod, "decrypt_archive", lambda *a, **k: None)
    monkeypatch.setattr(mod, "extract_archive", lambda *a, **k: {})
    monkeypatch.setattr(mod, "verify_hashes", lambda *a, **k: False)

    with pytest.raises(SystemExit):
        mod._restore_from_archive(args, "pass", "db-url")


async def test_main_resolves_passphrase_and_restores(tmp_path, monkeypatch):
    args = _args(tmp_path, full=True)
    monkeypatch.setattr(mod, "parse_args", lambda: args)
    monkeypatch.setattr(mod, "_validate_restore_args", lambda a: None)
    monkeypatch.setattr(mod, "resolve_passphrase", lambda p: "pw")
    monkeypatch.setattr(mod, "get_db_url", lambda u: "db-url")
    monkeypatch.setattr(mod, "_print_restore_mode", lambda a: None)

    captured = {}
    monkeypatch.setattr(mod, "_restore_from_archive", lambda a, p, u: captured.update(args=a, passphrase=p, db_url=u))

    await mod.main()

    assert captured["passphrase"] == "pw"
    assert captured["db_url"] == "db-url"
