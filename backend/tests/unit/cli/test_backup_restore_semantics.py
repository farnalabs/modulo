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

import click
import pytest
from click.testing import CliRunner

import modulo.cli.backup as backup_mod
from modulo.cli.backup import (
    MANIFEST_VERSION,
    _fernet_key_hash,
    _record_last_backup,
    cli,
)
from modulo.launcher.state import LauncherState, load_state, save_state

_FERNET_KEY = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_RESTORE_OUTPUTS = "restore-outputs"

_FAKE_ARCHIVED_PAYLOAD = {"schema_version": 1, "postgres_port": 15432, "redis_port": 16379, "api_port": 18000}
_DB_SQL_NAME = "database.sql"

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
        # Default: the target DB probes EMPTY (a real probe would try to
        # connect to the developer's own Postgres). Individual tests override.
        lambda: patch("modulo.cli.backup._db_has_modulo_tables", return_value=False),
        # Default: the local alembic head matches the archive's recorded
        # ["rev1"] birthplace schema (no surprise downgrade refusals).
        lambda: patch("modulo.cli.backup._get_schema_versions", return_value=["rev1"]),
        # Default: the pre-restore safety dump "succeeds" (a real invocation
        # shells out to pg_dump). Individual tests override with side effects.
        lambda: patch("modulo.cli.backup._take_safety_dump", return_value=Path("pre-restore-dump-fake")),
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
        # The archived state.json is a REAL HMAC envelope (payload + mac):
        # adoption is only possible from archives whose state.json carries
        # the fingerprint the manifest records.
        envelope = {"payload": _FAKE_ARCHIVED_PAYLOAD, "mac": state_mac if isinstance(state_mac, str) else None}
        (temp_backup / "state.json").write_text(json.dumps(envelope), encoding="utf-8")
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
        # Interactive-terminal gate: the confirmation is only reachable from
        # a real tty (a piped "echo y |" must not be able to approve it).
        stack.enter_context(patch("modulo.cli.backup._is_interactive_stdin", return_value=True))
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir), "--replace-cluster"]
        result = CliRunner().invoke(cli, args, input="n\n")
    assert result.exit_code != 0
    assert "dd" * 32 in result.output
    assert _target_mac(data_dir) in result.output
    assert "Replace the target instance with the archived one?" in result.output


def test_restore_replace_cluster_refuses_piped_confirmation(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    archive = _make_archive(tmp_path, MANIFEST_VERSION, "aa" * 32)
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir), "--replace-cluster"]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code != 0
    assert "interactive" in result.output
    assert "Replace the target instance with the archived one?" not in result.output


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
    envelope = {"payload": _FAKE_ARCHIVED_PAYLOAD, "mac": _target_mac(data_dir)}
    (archive / "state.json").write_text(json.dumps(envelope), encoding="utf-8")
    for name in ("secrets.json", "config.env"):
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


#    QA gate fixes: directory modes, identity round-trip, restore safety


@pytest.mark.skipif(os.name != "posix", reason="directory modes are POSIX-only")
def test_backup_directories_get_the_search_bit(tmp_path: Path) -> None:
    """Directories are 0700 — the e(xecute) bit IS the directory search bit:
    a 0600 directory is unsearchable and files inside it unresolvable."""
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "b"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        args = ["backup", "--output-dir", str(backup_dir), "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    tls_dir = backup_dir / "tls"
    assert (tls_dir.stat().st_mode & 0o777) == 0o700, f"{tls_dir} is not 0700"
    for path in backup_dir.rglob("*"):
        if path.is_dir():
            assert (path.stat().st_mode & 0o777) == 0o700, f"{path} is not 0700"


@pytest.mark.skipif(os.name != "posix", reason="chmod is a POSIX-only guarantee")
def test_backup_chmod_failure_continues_per_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One unchmoddable artefact logs a warning — it must never abort the
    sweep (or the backup) for the remaining entries."""
    backup_dir = tmp_path / "b"
    real_chmod = os.chmod

    def _flaky_chmod(path: Any, mode: int, follow_symlinks: bool = True) -> None:
        if str(path).endswith(_DB_SQL_NAME):
            raise OSError("simulated EPERM")
        real_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr("modulo.cli.backup.os.chmod", _flaky_chmod)
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        result = CliRunner().invoke(cli, ["backup", "--output-dir", str(backup_dir)], input="y\n")
    assert result.exit_code == 0, result.output
    manifest_path = backup_dir / "backup-info.json"
    assert (manifest_path.stat().st_mode & 0o777) == 0o600, "other artefacts must still be tightened"


@pytest.mark.skipif(os.name != "posix", reason="chmod is a POSIX-only guarantee")
def test_backup_only_tightens_artifacts_it_created(tmp_path: Path) -> None:
    """A user-supplied --output-dir the backup did NOT CREATE keeps its own
    root mode and its pre-existing files' modes."""
    backup_dir = tmp_path / "b"
    backup_dir.mkdir()
    placeholder = backup_dir / "operator-notes.txt"
    placeholder.write_text("mine", encoding="utf-8")
    placeholder.chmod(0o644)
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        args = ["backup", "--output-dir", str(backup_dir), "--data-dir", str(_seed_data_dir(tmp_path))]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert (placeholder.stat().st_mode & 0o777) == 0o644, "pre-existing user file must keep its mode"


def test_backup_round_trips_to_the_same_instance_without_replace(tmp_path: Path) -> None:
    """backup --data-dir then restore onto the SAME instance: the manifest's
    state_mac must match the data dir's FINAL on-disk state (record BEFORE
    snapshot), so no --replace-cluster is ever demanded."""
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "b"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        args = ["backup", "--output-dir", str(backup_dir), "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    manifest = json.loads((backup_dir / "backup-info.json").read_text(encoding="utf-8"))
    arc_mac = manifest["launcher_snapshot"]["state_mac"]
    assert arc_mac == _target_mac(data_dir), "archived mac must equal the final on-disk state mac"
    assert arc_mac == _target_mac(backup_dir)
    # and the restore accepts the same instance identity without --replace-cluster
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup._check_collation_versions_sync"))
        args = ["restore", str(backup_dir), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert "Restore complete" in result.output


def test_restore_writes_pre_restore_safety_dump_before_psql(tmp_path: Path) -> None:
    """A mid-import psql failure leaves the previous cluster restorable via
    the pre-restore safety dump taken BEFORE the import."""
    archive = _make_archive(tmp_path, MANIFEST_VERSION, None)
    safety_dumped: list[Path] = []
    order: list[str] = []

    def _fake_safety(raw_url: str, timeout: int = 300) -> Path:
        order.append("safety")
        safety_dumped.append(Path("safety"))
        return Path("safety")

    def _failing_psql(*args: Any, **kwargs: Any) -> None:
        order.append("psql")
        raise RuntimeError("simulated mid-import failure")

    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup._take_safety_dump", side_effect=_fake_safety))
        stack.enter_context(patch("modulo.cli.backup._run_psql", side_effect=_failing_psql))
        result = CliRunner().invoke(cli, ["restore", str(archive), "--yes"])
    assert result.exit_code != 0
    assert order == ["safety", "psql"], order
    assert "mid-import failure" in result.output


def test_restore_safety_dump_failure_refuses_before_the_wipe(tmp_path: Path) -> None:
    """When the safety dump cannot be taken, the restore refuses (nothing
    was touched); --no-safety-dump is the loud explicit opt-out."""
    archive = _make_archive(tmp_path, MANIFEST_VERSION, None)

    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(
            patch(
                "modulo.cli.backup._take_safety_dump",
                side_effect=click.ClickException("safety dump FAILED"),
            )
        )
        psql_mock = MagicMock()
        stack.enter_context(patch("modulo.cli.backup._run_psql", psql_mock))
        result = CliRunner().invoke(cli, ["restore", str(archive), "--yes"])
    assert result.exit_code != 0
    assert psql_mock.called is False

    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup._check_collation_versions_sync"))
        psql_mock = MagicMock()
        stack.enter_context(patch("modulo.cli.backup._run_psql", psql_mock))
        args = ["restore", str(archive), "--yes", "--no-safety-dump"]
        result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert psql_mock.called


def test_restore_adopts_launcher_files_only_after_db_restore_succeeds(tmp_path: Path) -> None:
    """Adoption-after-success ordering: when psql FAILS, no archive launcher
    artefact may reach the target data dir (bricked-on-failure guard)."""
    data_dir = _seed_data_dir(tmp_path)
    archive = _make_archive(tmp_path, MANIFEST_VERSION, _target_mac(data_dir))
    (archive / "secrets.json").write_text(json.dumps({"adopted": True}), encoding="utf-8")
    original_secrets = json.loads((data_dir / "secrets.json").read_text(encoding="utf-8"))
    call_order: list[str] = []
    psql_started = MagicMock()

    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())

        def _failing_psql(*args: Any, **kwargs: Any) -> None:
            call_order.append("psql")
            psql_started()
            raise RuntimeError("simulated psql failure")

        stack.enter_context(patch("modulo.cli.backup._run_psql", side_effect=_failing_psql))
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code != 0
    assert call_order == ["psql"]
    on_disk = json.loads((data_dir / "secrets.json").read_text(encoding="utf-8"))
    assert on_disk == original_secrets, "the ARCHIVE's secrets must NOT be adopted on a failed DB restore"


def test_restore_fresh_target_over_populated_db_is_not_exempt(tmp_path: Path) -> None:
    """A populated database behind an ABSENT data dir bypasses the state.json
    identity check — the DB-emptiness probe must close it."""
    archive = _make_archive(tmp_path, MANIFEST_VERSION, "ee" * 32)
    data_dir = tmp_path / "fresh-data"
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup._db_has_modulo_tables", return_value=True))
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code != 0
    assert "ALREADY CONTAINS" in result.output


def test_restore_refuses_schema_downgrade(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, MANIFEST_VERSION, None)
    archive_manifest = json.loads((archive / "backup-info.json").read_text(encoding="utf-8"))
    archive_manifest["schema_versions"] = ["newer_head"]
    (archive / "backup-info.json").write_text(json.dumps(archive_manifest), encoding="utf-8")
    psql_mock = MagicMock()
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup._get_schema_versions", return_value=["older_head"]))
        stack.enter_context(patch("modulo.cli.backup._run_psql", psql_mock))
        result = CliRunner().invoke(cli, ["restore", str(archive), "--yes"])
    assert result.exit_code != 0
    assert "NEWER than this installation speaks" in result.output
    assert psql_mock.called is False


def test_restore_accepts_matching_schema_versions(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, MANIFEST_VERSION, None)
    archive_manifest = json.loads((archive / "backup-info.json").read_text(encoding="utf-8"))
    archive_manifest["schema_versions"] = ["same_head"]
    (archive / "backup-info.json").write_text(json.dumps(archive_manifest), encoding="utf-8")
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        stack.enter_context(patch("modulo.cli.backup._get_schema_versions", return_value=["same_head"]))
        stack.enter_context(patch("modulo.cli.backup._check_collation_versions_sync"))
        result = CliRunner().invoke(cli, ["restore", str(archive), "--yes"])
    assert result.exit_code == 0, result.output


def test_restore_refuses_unverifiable_target_identity(tmp_path: Path) -> None:
    """A populated target whose state.json has NO mac must be REFUSED — the
    both-sides-None bypass must not let --yes skip verification."""
    data_dir = _seed_data_dir(tmp_path)
    envelope = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
    envelope.pop("mac")
    (data_dir / "state.json").write_text(json.dumps(envelope), encoding="utf-8")
    archive = _make_archive(tmp_path, MANIFEST_VERSION, None)
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code != 0
    assert "WITHOUT an identity fingerprint" in result.output


def test_restore_checks_archived_state_mac_before_adoption(tmp_path: Path) -> None:
    """The archived state.json's mac must equal the manifest's recorded
    state_mac BEFORE any launcher artefact is adopted."""
    data_dir = _seed_data_dir(tmp_path)
    archive = _make_archive(tmp_path, MANIFEST_VERSION, _target_mac(data_dir))
    # the archived state.json's envelope disagrees with the manifest record
    mismatched_envelope = {"payload": _FAKE_ARCHIVED_PAYLOAD, "mac": "ff" * 32}
    (archive / "state.json").write_text(json.dumps(mismatched_envelope), encoding="utf-8")
    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, lock=MagicMock())
        args = ["restore", str(archive), "--yes", "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code != 0
    assert "does NOT match" in result.output or "inconsistent" in result.output


def test_restore_verifies_ride_along_files_via_manifest_checksums(tmp_path: Path) -> None:
    """Every ride-along file (state.json / secrets.json / tls/*) is covered
    by the manifest's file_checksums so restore's verification loop runs."""
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "b"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        args = [
            "backup",
            "--output-dir",
            str(backup_dir),
            "--data-dir",
            str(data_dir),
            "--include-secrets",
        ]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    manifest = json.loads((backup_dir / "backup-info.json").read_text(encoding="utf-8"))
    for expected in ("state.json", "secrets.json", "config.env", "tls/server.crt"):
        assert expected in manifest["file_checksums"], f"{expected} missing from file_checksums"


def test_restore_refuses_malformed_manifest_before_echo(tmp_path: Path) -> None:
    """The manifest is shape-validated BEFORE any field is echoed: a JSON
    array (or hashes, or wrong-typed fields) never reaches the transcript."""
    bad_archive = tmp_path / "badlist"
    bad_archive.mkdir()
    (bad_archive / "backup-info.json").write_text("[]", encoding="utf-8")
    result = CliRunner().invoke(cli, ["restore", str(bad_archive), "--yes"])
    assert result.exit_code != 0
    assert "not a JSON object" in result.output
    assert "Backup timestamp" not in result.output


def test_restore_refuses_corrupt_manifest(tmp_path: Path) -> None:
    bad_archive = tmp_path / "badjson"
    bad_archive.mkdir()
    (bad_archive / "backup-info.json").write_text("{not json", encoding="utf-8")
    result = CliRunner().invoke(cli, ["restore", str(bad_archive), "--yes"])
    assert result.exit_code != 0
    assert "unreadable" in result.output


def test_restore_typed_field_validation_refuses_wrong_types(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, MANIFEST_VERSION, None)
    archive_manifest = json.loads((archive / "backup-info.json").read_text(encoding="utf-8"))
    archive_manifest["schema_versions"] = "rev1"  # must be a LIST of strings
    (archive / "backup-info.json").write_text(json.dumps(archive_manifest), encoding="utf-8")
    result = CliRunner().invoke(cli, ["restore", str(archive), "--yes"])
    assert result.exit_code != 0
    assert "not a list of strings" in result.output


def test_restore_psql_single_transaction_and_no_password_argv(tmp_path: Path) -> None:
    """psql replays the --clean dump under -1 (single transaction) and the
    password NEVER appears in a child argv — PGPASSWORD env only."""
    seen: dict[str, Any] = {}

    def _capture(argv: list[str], **kwargs: Any) -> Any:
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return MagicMock(returncode=0, stderr=b"")

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("modulo.cli.backup.shutil.which", return_value="/usr/bin/psql"))
        stack.enter_context(patch("modulo.cli.backup.subprocess.run", side_effect=_capture))
        # -1 must reach psql
        dump_input = tmp_path / "dump-input.sql"
        dump_input.write_text("-- dump", encoding="utf-8")
        backup_mod._run_psql("postgresql://modulo:secret@127.0.0.1:5432/modulo", dump_input)
    assert "-1" in seen["argv"]
    joined = " ".join(str(a) for a in seen["argv"])
    assert "secret" not in joined
    assert seen["env"]["PGPASSWORD"] == "secret"


def test_backup_pg_dump_no_password_in_argv(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def _capture(argv: list[str], **kwargs: Any) -> Any:
        seen["argv"] = argv
        seen["env"] = kwargs.get("env", {})
        return MagicMock(returncode=0, stderr=b"")

    output = tmp_path / "out.sql"
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("modulo.cli.backup.shutil.which", return_value="/usr/bin/pg_dump"))
        stack.enter_context(patch("modulo.cli.backup.subprocess.run", side_effect=_capture))
        backup_mod._run_pg_dump("postgresql://modulo:secret@127.0.0.1:5432/modulo", output)
    joined = " ".join(str(a) for a in seen["argv"])
    assert "secret" not in joined
    assert seen["env"]["PGPASSWORD"] == "secret"
    assert seen["argv"][-1] == "postgresql://modulo@127.0.0.1:5432/modulo"


@pytest.mark.skipif(os.name != "posix", reason="creation-mode guarantee is POSIX-only")
def test_pg_dump_output_file_is_created_private(tmp_path: Path) -> None:
    """The dump file is CREATED 0600 — a mid-dump SIGKILL must not leave the
    SQL dump readable at umask mode."""
    output = tmp_path / "out.sql"
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("modulo.cli.backup.shutil.which", return_value="/usr/bin/pg_dump"))
        stack.enter_context(patch("modulo.cli.backup.subprocess.run", return_value=MagicMock(returncode=0, stderr=b"")))
        backup_mod._run_pg_dump("postgresql://modulo:secret@127.0.0.1:5432/modulo", output)
    assert (output.stat().st_mode & 0o777) == 0o600, "the dump must be born 0600"


def test_backup_records_state_before_snapshotting(tmp_path: Path) -> None:
    """ORDER lock: _record_last_backup runs BEFORE the launcher snapshot files
    are archived (identity round-trip; see the round-trip test)."""
    order: list[str] = []
    with contextlib.ExitStack() as stack:
        real_record: Any = backup_mod._record_last_backup
        real_snapshot: Any = backup_mod._launcher_snapshot_files

        def _spy_record(data_dir: Path, timestamp: str) -> None:
            order.append("record")
            real_record(data_dir, timestamp)

        def _spy_snapshot(data_dir: Path, backup_dir: Path, include_secrets: bool) -> tuple[dict[str, Any], list[str]]:
            order.append("snapshot")
            return real_snapshot(data_dir, backup_dir, include_secrets)

        stack.enter_context(patch("modulo.cli.backup._record_last_backup", side_effect=_spy_record))
        stack.enter_context(patch("modulo.cli.backup._launcher_snapshot_files", side_effect=_spy_snapshot))
        _enter_backup_patches(stack, lock=MagicMock())
        data_dir = _seed_data_dir(tmp_path)
        backup_dir = tmp_path / "b"
        args = ["backup", "--output-dir", str(backup_dir), "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert order == ["record", "snapshot"], order


def test_backup_include_secrets_warning_only_when_a_secret_was_copied(tmp_path: Path) -> None:
    bare = tmp_path / "bare-data"
    bare.mkdir()
    (bare / "secrets.json").write_text(json.dumps({"postgres_password": "pw"}), encoding="utf-8")  # secrets only
    backup_dir = tmp_path / "b"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        args = ["backup", "--output-dir", str(backup_dir), "--data-dir", str(bare), "--include-secrets"]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert "FULL-fidelity archive" in result.output

    empty = tmp_path / "empty-data"
    empty.mkdir()
    backup_dir2 = tmp_path / "b2"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        args = ["backup", "--output-dir", str(backup_dir2), "--data-dir", str(empty), "--include-secrets"]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    assert "FULL-fidelity archive" not in result.output


def test_backup_documents_tls_private_key_in_the_manifest(tmp_path: Path) -> None:
    data_dir = _seed_data_dir(tmp_path)
    backup_dir = tmp_path / "b"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        args = ["backup", "--output-dir", str(backup_dir), "--data-dir", str(data_dir)]
        result = CliRunner().invoke(cli, args, input="y\n")
    assert result.exit_code == 0, result.output
    manifest = json.loads((backup_dir / "backup-info.json").read_text(encoding="utf-8"))
    assert "private key" in manifest["launcher_snapshot"].get("tls_note", "").casefold()
    assert "tls/server.crt" in manifest["launcher_snapshot"]["tls_files"]


def test_backup_measure_cloud_sync_by_component_equality(tmp_path: Path) -> None:
    """Vendor detection: component EQUALITY (case-folded) — a directory whose
    name merely CONTAINS a vendor word must not warn."""
    non_cloud = tmp_path / "dropbox-migrations"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        result = CliRunner().invoke(cli, ["backup", "--output-dir", str(non_cloud)], input="y\n")
    assert result.exit_code == 0, result.output
    assert "cloud-synced folder" not in result.output

    vendor_case = tmp_path / "onedrive"
    with contextlib.ExitStack() as stack:
        _enter_backup_patches(stack, lock=MagicMock())
        result = CliRunner().invoke(cli, ["backup", "--output-dir", str(vendor_case)], input="y\n")
    assert result.exit_code == 0, result.output
    assert "cloud-synced folder" in result.output
