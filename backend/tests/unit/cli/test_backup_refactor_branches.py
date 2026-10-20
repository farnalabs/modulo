"""Focused coverage for the S3776 decomposition helpers in ``modulo.cli.backup``.

These exercise the branches that only became separate functions during the
complexity decomposition: an absent TLS dir, an unreadable launcher state file,
and the manifest's optional ``fernet_key_hash`` field.
"""

import json
from pathlib import Path

import click
import pytest

from modulo.cli.backup import (
    _launcher_snapshot_files,
    _read_and_validate_manifest,
)


def _empty_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


def test_snapshot_without_state_or_tls_copies_nothing(tmp_path: Path) -> None:
    data_dir = _empty_data_dir(tmp_path)
    backup_dir = tmp_path / "b"
    backup_dir.mkdir()

    section, checksums = _launcher_snapshot_files(data_dir, backup_dir, include_secrets=False)

    assert section["state_json_included"] is False
    assert section["state_mac"] is None
    assert not section["tls_files"]
    assert not checksums


def test_snapshot_skips_directories_inside_the_tls_dir(tmp_path: Path) -> None:
    data_dir = _empty_data_dir(tmp_path)
    tls_dir = data_dir / "tls"
    tls_dir.mkdir()
    (tls_dir / "server.crt").write_text("cert", encoding="utf-8")
    (tls_dir / "nested").mkdir()
    backup_dir = tmp_path / "b"
    backup_dir.mkdir()

    section, checksums = _launcher_snapshot_files(data_dir, backup_dir, include_secrets=False)

    assert section["tls_files"] == ["tls/server.crt"]
    assert checksums == ["tls/server.crt"]


def test_snapshot_unreadable_state_logs_and_continues(tmp_path: Path) -> None:
    data_dir = _empty_data_dir(tmp_path)
    (data_dir / "state.json").write_text("{not valid json", encoding="utf-8")
    backup_dir = tmp_path / "b"
    backup_dir.mkdir()

    section, checksums = _launcher_snapshot_files(data_dir, backup_dir, include_secrets=False)

    assert section["state_json_included"] is True
    assert section["state_mac"] is None
    assert checksums == ["state.json"]


def test_manifest_without_fernet_key_hash_is_accepted(tmp_path: Path) -> None:
    manifest_path = tmp_path / "backup-info.json"
    manifest_path.write_text(json.dumps({"timestamp": "2024-01-01"}), encoding="utf-8")

    manifest = _read_and_validate_manifest(tmp_path)

    assert manifest["timestamp"] == "2024-01-01"


def test_manifest_with_non_string_fernet_key_hash_is_refused(tmp_path: Path) -> None:
    manifest_path = tmp_path / "backup-info.json"
    manifest_path.write_text(json.dumps({"fernet_key_hash": 123}), encoding="utf-8")

    with pytest.raises(click.ClickException, match="fernet_key_hash"):
        _read_and_validate_manifest(tmp_path)
