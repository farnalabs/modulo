"""FAR-672 â€” backup/restore data-safety semantics (ADR 031 Decisions 6/7).

Locks the contract added on top of the pre-epic ``modulo backup`` /
``modulo restore`` tool: the versioned manifest, secrets exclusion by
default (loud ``--include-secrets`` opt-in), 0600 artifacts on POSIX, the
cloud-sync warning, the persisted last-backup timestamp, the data-dir lock
refusals, verify-before-wipe ordering (instance identity + disk
pre-flight), the fresh-target exemption, the populated-mismatch refusal
with the ``--replace-cluster`` confirmation, the newer-manifest refusal,
the collation drift warning, and the promoted bootstrap posture re-run.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from modulo.cli.backup import (
    MANIFEST_VERSION,
    _fernet_key_hash,
    _record_last_backup,
    cli,
)
from modulo.launcher.state import LauncherState, load_state, save_state

_FERNET_KEY = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_RESTORE_OUTPUTS = "restore-outputs"

_LOCK_REFUSAL = (
    "Refusing to start: data dir /data is locked by another launcher "
    "(holder PID 4242, mode 'serve'); the requested acquire (mode 'backup') is refused."
)


def _seed_data_dir(tmp_path: Path) -> Path:
    """A bootstrapped-looking data dir: state.json + secrets + conf + TLS."""
    hmac_key = bytes(range(32))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "secrets.json").write_text(
        json.dumps({"postgres_password": "pw", "redis_password": "rw", "state_hmac_key": hmac_key.hex()}),
        encoding="utf-8",
    )
    save_state(LauncherState(postgres_port=15432, redis_port=16379, api_port=18000), data_dir / "state.json", hmac_key)
    (data_dir / "config.env").write_text("DATABASE_URL=postgresql+asyncpg://credential-bearing\n", encoding="utf-8")
    tls_dir = data_dir / "tls"
    tls_dir.mkdir()
    (tls_dir / "server.crt").write_text("-----CERT-----", encoding="utf-8")
    return data_dir


def _enter_backup_patches(stack: contextlib.ExitStack, lock: MagicMock | None = None) -> None:
    for factory in (
        lambda: patch("modulo.cli.backup._print_size"),
        lambda: patch("modulo.cli.backup._export_checkpoint_writes_sync", return_value=[]),
        lambda: patch("modulo.cli.backup._export_checkpoints_sync", return_value=[]),
        lambda: patch(
            "modulo.cli.backup._export_credentials_references_sync",
            return_value={"connector_instances": [], "model_backends": []},
        ),
        lambda: patch("modulo.cli.backup._export_checkpoint_blobs_sync", return_value=[]),
        lambda: patch("modulo.cli.backup._get_schema_versions", return_value=["rev1"]),
        lambda: patch("modulo.cli.backup._get_db_version", return_value="PostgreSQL 16.0"),
        lambda: patch("modulo.cli.backup._run_pg_dump", side_effect=_write_dump_for),
        lambda: patch("modulo.cli.backup.get_settings", **{"return_value.fernet_key": _FERNET_KEY}),
    ):
        stack.enter_context(factory())
    if lock is not None:
        stack.enter_context(patch("modulo.cli.backup._acquire_data_lock", return_value=lock))


def _write_dump_for(_url: Any, output: Path) -> None:
    output.write_text("-- pg_dump output", encoding="utf-8")


def _enter_restore_patches(stack: contextlib.ExitStack, lock: MagicMock | None = None) -> None:
    for factory in (
        lambda: patch("modulo.cli.backup._restore_checkpoint_writes_sync", return_value=0),
        lambda: patch("modulo.cli.backup._restore_checkpoints_sync", return_value=0),
        lambda: patch("modulo.cli.backup._restore_checkpoint_blobs_sync", return_value=0),
        lambda: patch("modulo.cli.backup._run_psql"),
        lambda: patch("modulo.cli.backup._ensure_restore_posture"),
        lambda: patch(
            "modulo.cli.backup.get_settings",
            **{
                "return_value.fernet_key": _FERNET_KEY,
                "return_value.database_url": "postgresql+asyncpg://modulo:pw@127.0.0.1:5432/modulo",
            },
        ),
    ):
        stack.enter_context(factory())
    if lock is not None:
        stack.enter_context(patch("modulo.cli.backup._acquire_data_lock", return_value=lock))


def _base_manifest_versioned(manifest_version: int, state_mac: str | None) -> dict[str, Any]:
    return {
        "timestamp": "2024-01-01T00:00:00+00:00",
        "manifest_version": manifest_version,
        "backup_type": "full",
        "db_version": "PostgreSQL 16.0",
        "schema_versions": ["rev1"],
        "fernet_key_hash": _fernet_key_hash(_FERNET_KEY),
        "file_checksums": {},
        "launcher_snapshot": {"state_json_included": state_mac is not None, "state_mac": state_mac},
    }


def _legacy_manifest_with_identity() -> dict[str, Any]:
    """A (legacy-style) manifest still carrying state JSON metadata."""
    return {
        "timestamp": "2024-01-01T00:00:00+00:00",
        "backup_type": "full",
        "db_version": "PostgreSQL 16.0",
        "schema_versions": ["rev1"],
        "fernet_key_hash": _fernet_key_hash(_FERNET_KEY),
        "file_checksums": {},
        "launcher_snapshot": {"state_json_included": True, "state_mac": "cc" * 32},
    }


def _target_mac(data_dir: Path) -> str:
    envelope = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
    mac = envelope["mac"]
    assert isinstance(mac, str)
    return mac


def _enter_click_refusal(stack: contextlib.ExitStack, message: str) -> None:
    import click

    stack.enter_context(patch("modulo.cli.backup._acquire_data_lock", side_effect=click.ClickException(message)))


#    Backup: versioned manifest + default exclusions


def test_backup_manifest_is_versioned(tmp_path: Path) -> None:
    backup_dir = tmp_path / "b"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        result = CliRunner().invoke(cli, ["backup", "--output-dir", str(backup_dir)], input="y\n")
    assert result.exit_code == 0, result.output
    manifest = json.loads((backup_dir / "backup-info.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == MANIFEST_VERSION


def test_backup_manifest_documents_ephemeral_by_design(tmp_path: Path) -> None:
    backup_dir = tmp_path / "b"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        result = CliRunner().invoke(cli, ["backup", "--output-dir", str(backup_dir)], input="y\n")
    assert result.exit_code == 0, result.output
    manifest = json.loads((backup_dir / "backup-info.json").read_text(encoding="utf-8"))
    note = manifest["launcher_snapshot"]["ephemeral"]["redis_saq_queue_data"]
    assert "ephemeral" in note
    assert "EXCLUDED" in note


def test_backup_state_ride_along_with_data_dir(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "b"
    lock_mock = MagicMock()
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=lock_mock)
        args = ["backup", "--output-dir", str(backup_dir), "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert lock_mock.release.called
    manifest = json.loads((backup_dir / "backup-info.json").read_text(encoding="utf-8"))
    locked_section = manifest["launcher_snapshot"]
    assert locked_section["state_json_included"] is True
    assert (backup_dir / "state.json").exists()


def test_backup_default_archive_carries_no_credentials(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "b"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        args = ["backup", "--output-dir", str(backup_dir), "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert not (backup_dir / "secrets.json").exists()
    assert not (backup_dir / "config.env").exists()
    manifest = json.loads((backup_dir / "backup-info.json").read_text(encoding="utf-8"))
    locked_section = manifest["launcher_snapshot"]
    assert locked_section["secrets_file"]["included"] is False
    assert locked_section["conf_profile"]["included"] is False
    assert locked_section["tls_files"] == ["tls/server.crt"]
    assert (backup_dir / "tls" / "server.crt").exists()


def test_backup_include_secrets_copies_and_warns_loudly(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "b"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        args = ["backup", "--output-dir", str(backup_dir), "--data-dir", str(data_dir), "--include-secrets"]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert (backup_dir / "secrets.json").exists()
    assert (backup_dir / "config.env").exists()
    manifest = json.loads((backup_dir / "backup-info.json").read_text(encoding="utf-8"))
    locked_section = manifest["launcher_snapshot"]
    assert locked_section["secrets_file"]["included"] is True
    assert "--include-secrets" in result.output


def test_backup_refuses_while_a_launcher_is_running(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    dump_mock = MagicMock()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("modulo.cli.backup._run_pg_dump", dump_mock))
        _enter_click_refusal(stack, _LOCK_REFUSAL)
        result = CliRunner().invoke(cli, ["backup", "--data-dir", str(data_dir)], input="y\n")
    assert result.exit_code != 0
    assert "4242" in result.output
    assert dump_mock.called is False


def test_backup_persists_last_backup_timestamp(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "b"
    state_path = data_dir / "state.json"
    hmac_key = bytes(range(32))
    before = load_state(state_path, hmac_key)
    assert before.last_backup_at is None
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        args = ["backup", "--output-dir", str(backup_dir), "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    after = load_state(state_path, hmac_key)
    assert after.last_backup_at is not None
    assert after.postgres_port == before.postgres_port


def test_record_last_backup_helper_is_roundtrip_stable(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    _record_last_backup(data_dir, "2026-09-10T00:00:00+00:00")
    hmac_key = bytes(range(32))
    after = load_state(data_dir / "state.json", hmac_key)
    assert after.last_backup_at == "2026-09-10T00:00:00+00:00"


@pytest.mark.skipif(os.name != "posix", reason="0600 artifact modes are POSIX-only")
def test_backup_artifacts_are_owner_only(tmp_path: Path) -> None:
    backup_dir = tmp_path / "b"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        result = CliRunner().invoke(cli, ["backup", "--output-dir", str(backup_dir)], input="y\n")
    assert result.exit_code == 0, result.output
    for path in backup_dir.rglob("*"):
        if path.is_file():
            assert (path.stat().st_mode & 0o777) == 0o600, f"{path} is not 0600"


def test_backup_inside_cloud_synced_root_warns(tmp_path: Path) -> None:
    cloud_dir = tmp_path / "Dropbox" / "remotebackup"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        result = CliRunner().invoke(cli, ["backup", "--output-dir", str(cloud_dir)], input="y\n")
    assert result.exit_code == 0, result.output
    assert "cloud-synced folder" in result.output


#    Restore: verify-before-wipe


def _make_archive(
    tmp_path: Path,
    manifest_version: int | None,
    state_mac: str | None,
    *,
    launcher_files: bool = True,
) -> Path:
    temp_backup = tmp_path / (_RESTORE_OUTPUTS + str(len(list(tmp_path.iterdir()))))
    temp_backup.mkdir()
    manifest: dict[str, Any] = {}
    if manifest_version is None:
        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": ["rev1"],
            "fernet_key_hash": _fernet_key_hash(_FERNET_KEY),
            "file_checksums": {},
        }
    else:
        manifest = _base_manifest_versioned(manifest_version, state_mac)
    (temp_backup / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
    (temp_backup / "database.sql").write_text("-- dump", encoding="utf-8")
    (temp_backup / "checkpoint_blobs.json").write_text("[]", encoding="utf-8")
    (temp_backup / "checkpoints.json").write_text("[]", encoding="utf-8")
    (temp_backup / "checkpoint_writes.json").write_text("[]", encoding="utf-8")
    (temp_backup / "credentials_references.json").write_text("{}", encoding="utf-8")
    if launcher_files:
        (temp_backup / "state.json").write_text("{}", encoding="utf-8")
    return temp_backup


def test_restore_refuses_newer_manifest_version(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, MANIFEST_VERSION + 1, None)
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        result = CliRunner().invoke(cli, ["restore", str(archive), "--yes"])
    assert result.exit_code != 0
    assert "NEWER than this Modulo supports" in result.output


def test_restore_accepts_supported_manifest_version(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, MANIFEST_VERSION, None)
    psql_mock = MagicMock()
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup._run_psql", psql_mock))
        result = CliRunner().invoke(cli, ["restore", str(archive), "--yes"])
    assert result.exit_code == 0, result.output
    assert "Restore complete" in result.output
    assert psql_mock.called


def test_restore_refuses_populated_instance_mismatch(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    archive = _make_archive(tmp_path, MANIFEST_VERSION, "bb" * 32)
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0
    assert "Instance identity MISMATCH" in result.output


def test_restore_replace_cluster_requires_loud_confirmation_if_declined(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    archive = _make_archive(tmp_path, MANIFEST_VERSION, "dd" * 32)
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir), "--replace-cluster"]
        result = CliRunner().invoke(cli, args, input="n\n")
    assert result.exit_code != 0
    assert "dd" * 32 in result.output
    assert _target_mac(data_dir) in result.output
    assert "Replace the target instance with the archived one?" in result.output


def test_restore_fresh_target_is_exempt(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, MANIFEST_VERSION, "ee" * 32)
    data_dir = tmp_path / "fresh-data"
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert (data_dir / "state.json").exists()


def test_restore_legacy_archive_warns_when_identity_unverifiable(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    archive = _make_archive(tmp_path, None, None)
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "cannot verify this restore targets" in result.output


def test_restore_adopts_launcher_files(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    archive = tmp_path / "arch"
    archive.mkdir()
    manifest = _base_manifest_versioned(MANIFEST_VERSION, _target_mac(data_dir))
    (archive / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name in ("state.json", "secrets.json", "config.env"):
        (archive / name).write_text(json.dumps({"adopted": name}), encoding="utf-8")
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert (data_dir / "secrets.json").exists()


def test_restore_refused_while_data_dir_is_locked(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    archive = _make_archive(tmp_path, MANIFEST_VERSION, _target_mac(data_dir))
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        _enter_click_refusal(stack, _LOCK_REFUSAL)
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code != 0
    assert "Restore complete" not in result.output


def test_restore_disk_preflight_refuses_dump_too_large_for_disk(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    archive = tmp_path / "small-store"
    archive.mkdir()
    manifest = _base_manifest_versioned(MANIFEST_VERSION, _target_mac(data_dir))
    (archive / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
    (archive / "database.sql").write_text("x" * 1024, encoding="utf-8")
    usage = MagicMock()
    usage.free = 16
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup.shutil.disk_usage", return_value=usage))
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code != 0
    assert "Disk pre-flight FAILED" in result.output


def test_restore_declined_confirmation_reaches_no_psql(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    archive = _make_archive(tmp_path, MANIFEST_VERSION, _target_mac(data_dir))
    psql_mock = MagicMock()
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup._run_psql", psql_mock))
        args = ["restore", str(archive), "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="n\n")
    assert result.exit_code != 0
    assert psql_mock.called is False


def test_restore_collation_drift_prints_hard_warning(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, MANIFEST_VERSION, None)
    collation_conn = _make_collation_connection([("en_US.UTF-8", "2.28", "2.35", "c")])
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup._ensure_restore_posture"))
        stack.enter_context(patch("modulo.cli.backup.psycopg.connect", return_value=collation_conn))
        args = ["restore", str(archive), "--yes", "--db-url", "postgresql://modulo:pw@127.0.0.1:5432/modulo"]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert "COLLATION VERSION MISMATCH" in result.output
    assert "reindexdb" in result.output


def _make_collation_connection(rows: list[tuple[Any, ...]]) -> MagicMock:
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.__enter__.return_value = conn  # `with psycopg.connect(...) as conn` binds the mock itself
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn


def test_restore_reruns_promoted_posture_bootstrap(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, MANIFEST_VERSION, None)
    posture_mock = MagicMock()
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup._ensure_restore_posture", posture_mock))
        stack.enter_context(patch("modulo.cli.backup._check_collation_versions_sync"))
        args = ["restore", str(archive), "--yes", "--db-url", "postgresql://modulo:pw@127.0.0.1:5432/modulo"]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert posture_mock.call_count == 1
    admin_url, app_url = posture_mock.call_args[0]
    assert app_url.startswith("postgresql+asyncpg://")
    assert admin_url.startswith("postgresql://")
    assert admin_url
