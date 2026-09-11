"""FAR-677 — cross-machine restore harness (two-instance, test-level sim).

Two isolated "machines" (two temp launcher data dirs, no real CI jobs —
the simulation lives entirely in this test file):

* machine A boots, seeds, and backs up into an archive (the REAL
  ``modulo backup`` snapshot semantics: the state.json ride-along emits a
  state_mac fingerprint and ``--include-secrets`` adds secrets.json + the
  conf profile; a fabricated ``database.sql`` stands in for pg_dump).
* machine B (a fresh runner) restores from the archive into a fresh data
  dir and must boot healthy with the data intact: the identity fingerprint
  exemption admits the fresh target, the launcher artefacts are adopted,
  the adopted secrets CONTINUE (never regenerated), and the unchanged
  FERNET_KEY is recognised (no re-encryption needed).
* populated-mismatch refusal: restoring A's backup onto a POPULATED B
  (a different instance with its own fingerprint) refuses without
  ``--replace-cluster`` — nothing touches B.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

requires_posix = pytest.mark.skipif(sys.platform == "win32", reason="restore takes the POSIX flock data-dir lock")


def _settings_env_ready() -> bool:
    """``modulo.cli`` builds Settings at import; the CI jobs export these."""
    return all(os.environ.get(name) for name in ("DATABASE_URL", "SECRET_KEY", "FERNET_KEY"))


requires_cli_env = pytest.mark.skipif(
    not _settings_env_ready(),
    reason="modulo.cli import chain builds Settings at import — exported by CI",
)


@pytest.fixture(autouse=True)
def _reset_launcher_state() -> Iterator[None]:
    """Restore the module-level pin, guard, and the get_settings cache."""
    import modulo.settings as settings_module
    from modulo.settings import get_settings

    get_settings.cache_clear()
    yield
    settings_module._pinned_env_file = None
    settings_module._first_boot_guard = None
    get_settings.cache_clear()


_FERNET_KEY = "9u9GToDRLOtSKkZGkZlFnXlJpNdIay2y5vUsVC5J0Bk="


def real_snapshot_files(data_dir: Path, backup_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """The REAL launcher-artefact rides-along (state + secrets + conf)."""
    from modulo.cli.backup import _launcher_snapshot_files

    return _launcher_snapshot_files(data_dir, backup_dir, include_secrets=True)


def _enter_restore_patches(stack: contextlib.ExitStack, fernet_key: str) -> None:
    """The DB-side restore seams (psql/checks) under test control."""
    for factory in (
        lambda: patch("modulo.cli.backup._run_psql"),
        lambda: patch("modulo.cli.backup._ensure_restore_posture"),
        lambda: patch(
            "modulo.cli.backup.get_settings",
            **{
                "return_value.fernet_key": fernet_key,
                "return_value.database_url": "postgresql+asyncpg://modulo:pw@127.0.0.1:5432/modulo",
            },
        ),
        lambda: patch("modulo.cli.backup._db_has_modulo_tables", return_value=False),
        lambda: patch("modulo.cli.backup._get_schema_versions", return_value=["rev1"]),
        lambda: patch("modulo.cli.backup._take_safety_dump", return_value=Path("pre-restore-dump-fake")),
        lambda: patch("modulo.cli.backup._check_collation_versions_sync"),
    ):
        stack.enter_context(factory())


def _boot_machine(tmp_path: Path, name: str) -> tuple[Path, Any]:
    """One isolated machine: its own secrets/state, with a seeded cluster."""
    import modulo.launcher.secrets_file as secrets_file_module
    from modulo.launcher.state import LauncherState, save_state

    data_dir = tmp_path / name
    data_dir.mkdir()
    secrets = secrets_file_module.load_or_create(data_dir / secrets_file_module.SECRETS_FILENAME)
    state = LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)
    save_state(state, data_dir / "state.json", secrets.state_hmac_key)
    pgdata = data_dir / "pgdata"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("16\n", encoding="ascii")
    (data_dir / "config.env").write_text(
        "DATABASE_URL=postgresql+asyncpg://modulo:a-credential@127.0.0.1:15432/modulo\n",
        encoding="utf-8",
    )
    return data_dir, secrets


def _fabricate_machine_backup(data_dir: Path, archive_dir: Path) -> Path:
    """A REAL machine-migration archive: state_MAC + include_secrets."""
    from modulo.cli.backup import (
        MANIFEST_VERSION,
        _fernet_key_hash,
        _file_checksum,
        _record_last_backup,
    )

    archive_dir.mkdir()
    # The real backup ordering: persist the timestamp BEFORE snapshotting.
    _record_last_backup(data_dir, "2026-09-10T00:00:00+00:00")
    launcher_section, launcher_checksums = real_snapshot_files(data_dir, archive_dir)
    (archive_dir / "database.sql").write_text("-- cross-machine dump seed\n", encoding="utf-8")
    file_checksums = {"database.sql": _file_checksum(archive_dir / "database.sql")}
    for name in launcher_checksums:
        file_checksums[name] = _file_checksum(archive_dir / name)
    manifest = {
        "timestamp": "2026-09-10T00:00:00+00:00",
        "manifest_version": MANIFEST_VERSION,
        "backup_type": "full",
        "db_version": "PostgreSQL 16.0",
        "schema_versions": ["rev1"],
        "fernet_key_hash": _fernet_key_hash(_FERNET_KEY),
        "file_checksums": file_checksums,
        "launcher_snapshot": launcher_section,
    }
    (archive_dir / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
    return archive_dir


def _restore_invocation(archive_dir: Path, machine_data_dir: Path) -> Any:
    """Run the machine-migration CLI invocation under the restore seams."""
    from modulo.cli.backup import cli

    with contextlib.ExitStack() as stack:
        _enter_restore_patches(stack, _FERNET_KEY)
        return CliRunner().invoke(
            cli,
            ["restore", str(archive_dir), "--yes", "--data-dir", str(machine_data_dir)],
        )


@requires_posix
@requires_cli_env
def test_machine_b_restores_machine_a_backup_onto_a_fresh_target(tmp_path: Path) -> None:
    """A restores onto a fresh B: exemption + adoption + credential match."""
    import modulo.launcher.secrets_file as secrets_file_module
    from modulo.launcher.state import load_state

    machine_a, secrets_a = _boot_machine(tmp_path, "machine-a")
    archive = _fabricate_machine_backup(machine_a, tmp_path / "archive")
    machine_b = tmp_path / "machine-b"
    machine_b.mkdir()  # a FRESH, empty data dir (the disaster-recovery target)
    result = _restore_invocation(archive, machine_b)
    assert result.exit_code == 0, result.output
    assert "Restore complete" in result.output
    assert "Adopted launcher artefacts into" in result.output
    # Credential continuity: machine B's adopted secrets ARE A's secrets.
    adopted = secrets_file_module._parse((machine_b / "secrets.json").read_bytes())
    assert adopted.postgres_password == secrets_a.postgres_password
    assert adopted.redis_password == secrets_a.redis_password
    state_b = load_state(machine_b / "state.json", adopted.state_hmac_key)
    assert state_b.postgres_port == 15432
    assert state_b.last_backup_at is not None  # the persisted record rides along
    # Fresh-target identity exemption: the restore ran WITHOUT any refusal.
    assert "Instance identity MISMATCH" not in result.output
    # FERNET round-trip: the restore recognised the unchanged key and the
    # adopted credentials carry through (no re-encryption was needed).
    assert "FERNET_KEY unchanged" in result.output


@requires_posix
@requires_cli_env
def test_cross_machine_instance_identity_mismatch_refuses_populated_target(
    tmp_path: Path,
) -> None:
    """Restoring A's archive onto POPULATED machine B refuses by identity."""
    machine_a, _secrets_a = _boot_machine(tmp_path, "machine-a")
    machine_b, _secrets_b = _boot_machine(tmp_path, "machine-b")
    assert machine_a != machine_b
    archive = _fabricate_machine_backup(machine_a, tmp_path / "archive-a")
    original_state_bytes = (machine_b / "state.json").read_bytes()
    original_secrets_bytes = (machine_b / "secrets.json").read_bytes()
    result = _restore_invocation(archive, machine_b)
    assert result.exit_code != 0
    assert "Instance identity MISMATCH" in result.output
    # Nothing was replaced: machine B's state + secrets are untouched bytes.
    assert (machine_b / "state.json").read_bytes() == original_state_bytes
    assert (machine_b / "secrets.json").read_bytes() == original_secrets_bytes
