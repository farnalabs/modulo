"""FAR-672 — launcher/upgrade.py: the installer-enforced pre-upgrade pg_dump.

Locks the upgrade-helper contract the installer depends on: the dump failure
aborts with an actionable UpgradeError, a zero-byte dump is refused, an
empty/non-bootstrapped data dir is refused, the snapshot path (with a
manifest) is reported, the snapshot is owner-only on POSIX, and the
swap-phase lock refusal names a LIVE holder PID while tolerating a stale
record.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from modulo.launcher.state import LauncherState, load_state, save_state
from modulo.launcher.upgrade import (
    SNAPSHOT_MANIFEST_NAME,
    SNAPSHOT_PREFIX,
    UpgradeError,
    assert_not_held,
    main,
    pre_upgrade_dump,
)

_RESTORE_MANIFEST_NAME = "backup-info.json"

_HMAC_KEY = bytes(range(32))


def _seed_data_dir(tmp_path: Path) -> Path:
    """A bootstrapped-looking data dir (state.json + secrets + pgdata)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "secrets.json").write_text(
        json.dumps({"postgres_password": "pw", "redis_password": "rw", "state_hmac_key": _HMAC_KEY.hex()}),
        encoding="utf-8",
    )
    save_state(LauncherState(postgres_port=15432, redis_port=16379, api_port=18000), data_dir / "state.json", _HMAC_KEY)
    pgdata = data_dir / "pgdata"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("16\n", encoding="utf-8")
    return data_dir


def _allow_windows_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The secrets-file platform gate is P3-gated; the helper's own Linux
    contract must stay testable on every developer platform (TODO(P3))."""
    from modulo.launcher import secrets_file

    monkeypatch.setattr(secrets_file, "assert_supported_platform", lambda: None)


def _fake_run(returncode: int = 0, payload: bytes = b"-- pg_dump output\n") -> Callable[..., Any]:
    def _run(argv: list[str], stdout: Any = None, **kwargs: Any) -> MagicMock:
        if stdout is not None and returncode == 0:
            stdout.write(payload)
        return MagicMock(returncode=returncode, stderr=b"connection refused" if returncode else b"")

    return _run


def _enter_fake_run(stack: contextlib.ExitStack, **kwargs: Any) -> MagicMock:
    runner = patch("modulo.launcher.upgrade.subprocess.run", new=_fake_run(**kwargs))
    return stack.enter_context(runner)


def _settings_env_ready() -> bool:
    """The restore e2e imports ``modulo.cli.backup``; the ``modulo.cli`` package
    transitively imports ``modulo.db.session``, which builds its module-global
    engine from ``Settings`` at import time. ``Settings`` requires
    ``DATABASE_URL`` / ``SECRET_KEY`` / ``FERNET_KEY`` — the CI test jobs export
    them, but a bare developer checkout may not — so probe the env instead of
    letting the in-test import crash."""
    return all(os.environ.get(name) for name in ("DATABASE_URL", "SECRET_KEY", "FERNET_KEY"))


def test_pre_upgrade_dump_reports_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack)
        snapshot = pre_upgrade_dump(data_dir)
    assert snapshot.directory.parent == data_dir
    assert snapshot.directory.name.startswith(SNAPSHOT_PREFIX)
    assert snapshot.dump_path.exists()
    assert snapshot.bytes == len(b"-- pg_dump output\n")
    manifest = json.loads((snapshot.directory / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["snapshot_for_upgrade"] is True
    assert manifest["dump_bytes"] == snapshot.bytes


def test_dump_failure_aborts_and_sweeps_the_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack, returncode=1)
        with pytest.raises(UpgradeError, match="dump FAILED"):
            pre_upgrade_dump(data_dir)
    leftovers = [entry for entry in data_dir.iterdir() if entry.name.startswith(SNAPSHOT_PREFIX)]
    assert not leftovers


def test_empty_dump_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack, payload=b"")
        with pytest.raises(UpgradeError, match="EMPTY"):
            pre_upgrade_dump(data_dir)
    leftovers = [entry for entry in data_dir.iterdir() if entry.name.startswith(SNAPSHOT_PREFIX)]
    assert not leftovers


def test_unbootstrapped_data_dir_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_windows_secrets(monkeypatch)
    empty = tmp_path / "empty-data"
    empty.mkdir()
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack)
        with pytest.raises(UpgradeError, match="not bootstrapped"):
            pre_upgrade_dump(empty)


def test_missing_cluster_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_windows_secrets(monkeypatch)
    data_dir = tmp_path / "no-cluster"
    data_dir.mkdir()
    (data_dir / "state.json").write_text(json.dumps({"payload": {}, "mac": "x"}), encoding="utf-8")
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack)
        with pytest.raises(UpgradeError, match="initialised bundled cluster"):
            pre_upgrade_dump(data_dir)


def test_dump_carries_the_bundled_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    seen: dict[str, Any] = {}

    def _capture(argv: list[str], stdout: Any = None, **kwargs: Any) -> MagicMock:
        seen["argv"] = argv
        if stdout is not None:
            stdout.write(b"-- pg_dump output\n")
        return MagicMock(returncode=0)

    with patch("modulo.launcher.upgrade.subprocess.run", new=_capture):
        pre_upgrade_dump(data_dir)
    argv = seen["argv"]
    assert argv[0].endswith("pg_dump") or argv[0].endswith("pg_dump.exe")
    argv_str = " ".join(argv)
    assert "--clean" in argv_str
    assert "--format=plain" in argv_str
    assert "15432" in argv_str


@pytest.mark.skipif(os.name != "posix", reason="/proc-only liveness check (Linux-first, P1a)")
def test_assert_not_held_refuses_a_live_holder(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lock_path = data_dir.parent / (data_dir.name + ".lock")
    lock_path.write_text(json.dumps({"pid": os.getpid(), "mode": "serve", "acquired_at": 0.0}), encoding="utf-8")
    with pytest.raises(UpgradeError, match=f"holder PID {os.getpid()}"):
        assert_not_held(data_dir)


@pytest.mark.skipif(os.name != "posix", reason="/proc-only liveness check (Linux-first, P1a)")
def test_assert_not_held_tolerates_a_stale_record(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lock_path = data_dir.parent / (data_dir.name + ".lock")
    lock_path.write_text(json.dumps({"pid": 999_999_999, "mode": "serve", "acquired_at": 0.0}), encoding="utf-8")
    assert_not_held(data_dir)  # dead holder: the kernel released the flock


def test_assert_not_held_clean_slate(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert_not_held(data_dir)  # no lock file: nothing to refuse


def test_main_prints_the_snapshot_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack)
        code = main(["--data-dir", str(data_dir), "--bin-dir", str(tmp_path / "bin")])
    assert code == 0
    captured = capsys.readouterr()
    printed = captured.out.strip().splitlines()[-1]
    assert Path(printed).is_absolute()
    assert Path(printed).exists()
    state = load_state(data_dir / "state.json", _HMAC_KEY)
    assert state.postgres_port == 15432


def test_snapshot_carries_the_restore_compatible_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`modulo restore` hard-requires backup-info.json: the snapshot carries
    BOTH manifests so the installer's printed command works verbatim."""
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack)
        snapshot = pre_upgrade_dump(data_dir)
    restore_manifest = json.loads((snapshot.directory / _RESTORE_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert SNAPSHOT_MANIFEST_NAME in {entry.name for entry in snapshot.directory.iterdir()}
    assert "file_checksums" in restore_manifest
    checksum = restore_manifest["file_checksums"]["database.sql"]
    assert len(checksum) == 64
    assert isinstance(restore_manifest["schema_versions"], list)


def test_missing_secrets_file_refuses_and_generates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dump helper is load-ONLY: a missing secrets file refuses — it must
    never GENERATE one (an orphan secrets.json bricks the next boot)."""
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    (data_dir / "secrets.json").unlink()
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack)
        with pytest.raises(UpgradeError, match="must never generate credentials"):
            pre_upgrade_dump(data_dir)
    assert not (data_dir / "secrets.json").exists(), "a generated orphan must never be left behind"


@pytest.mark.skipif(os.name != "posix", reason="directory modes are POSIX-only")
def test_snapshot_directory_gets_the_search_bit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot DIR is 0700 (0600 strips the directory search bit and
    makes the dump inside unresolvable)."""
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack)
        snapshot = pre_upgrade_dump(data_dir)
    assert (snapshot.directory.stat().st_mode & 0o777) == 0o700
    assert (snapshot.dump_path.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="creation-mode guarantee is POSIX-only")
def test_dump_file_is_born_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dump file is CREATED 0600 — a mid-dump SIGKILL never leaves it at
    umask mode."""
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack)
        snapshot = pre_upgrade_dump(data_dir)
    assert (snapshot.dump_path.stat().st_mode & 0o777) == 0o600


def test_dump_password_is_never_in_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The postgres password travels ONLY as child-env PGPASSWORD — never in
    the world-readable /proc/<pid>/cmdline argv."""
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    seen: dict[str, Any] = {}

    def _capture(argv: list[str], stdout: Any = None, **kwargs: Any) -> MagicMock:
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        if stdout is not None:
            stdout.write(b"-- pg_dump output\n")
        return MagicMock(returncode=0)

    with patch("modulo.launcher.upgrade.subprocess.run", new=_capture):
        pre_upgrade_dump(data_dir)
    joined = " ".join(str(a) for a in seen["argv"])
    assert "pw" not in joined
    assert seen["argv"][-1] == "postgresql://modulo@127.0.0.1:15432/modulo"
    assert seen["env"]["PGPASSWORD"] == "pw"


def test_state_with_last_backup_stamps_v2(tmp_path: Path) -> None:
    """FAR-672 forward-compat: the optional field stamps v2 so an OLD
    launcher refuses via the DESIGNED version gate (a clear "written by a
    NEWER launcher" refusal) — not an "unknown field(s)" integrity error."""
    from modulo.launcher.state import (
        SCHEMA_VERSION,
        SCHEMA_VERSION_WITH_LAST_BACKUP,
        StateVersionError,
        _mac_for,
        load_state,
        save_state,
    )

    marked = LauncherState(postgres_port=15432, redis_port=16379, api_port=18000, last_backup_at="t")
    payload = marked.to_payload()
    assert payload["schema_version"] == SCHEMA_VERSION_WITH_LAST_BACKUP

    # The v2-shaped file still loads on THIS reader (the field is optional).
    state_path = tmp_path / "state.json"
    save_state(marked, state_path, bytes(range(32)))
    assert load_state(state_path, bytes(range(32))).last_backup_at == "t"

    # A file stamped by an EVEN NEWER writer refuses with the clean version message.
    newer_payload = payload | {"schema_version": SCHEMA_VERSION + 999}
    envelope = {"payload": newer_payload, "mac": _mac_for(newer_payload, bytes(range(32)))}
    newer_path = tmp_path / "newer.json"
    newer_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(StateVersionError, match="NEWER launcher"):
        load_state(newer_path, bytes(range(32)))

    # A v1 payload WITHOUT the field loads unchanged (byte-level compat).
    v1_payload = {"schema_version": SCHEMA_VERSION, "postgres_port": 15432, "redis_port": 16379, "api_port": 18000}
    v1_envelope = {"payload": v1_payload, "mac": _mac_for(v1_payload, bytes(range(32)))}
    v1_path = tmp_path / "v1.json"
    v1_path.write_text(json.dumps(v1_envelope), encoding="utf-8")
    loaded = load_state(v1_path, bytes(range(32)))
    assert loaded.postgres_port == 15432
    assert loaded.last_backup_at is None


@pytest.mark.skipif(
    not _settings_env_ready(),
    reason="modulo.cli import chain builds Settings (DATABASE_URL/SECRET_KEY/FERNET_KEY) at import — exported by CI",
)
def test_dumped_snapshot_restores_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E: pre_upgrade_dump -> `modulo restore <snapshot> --yes` succeeds,
    exactly as the installer's printed hint advertises."""
    from click.testing import CliRunner

    from modulo.cli.backup import cli as backup_cli

    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack)
        snapshot = pre_upgrade_dump(data_dir)
    restore_manifest = json.loads((snapshot.directory / "backup-info.json").read_text(encoding="utf-8"))

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("modulo.cli.backup.get_settings", **{"return_value.fernet_key": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})
        )
        stack.enter_context(
            patch("modulo.cli.backup._get_schema_versions", return_value=restore_manifest["schema_versions"])
        )
        stack.enter_context(patch("modulo.cli.backup.shutil.which", return_value="/usr/bin/pg_dump"))
        stack.enter_context(
            patch(
                "modulo.cli.backup._run_pg_dump",
                side_effect=lambda _url, output, timeout=300: Path(output).write_text(
                    "-- safety dump", encoding="utf-8"
                ),
            )
        )
        stack.enter_context(patch("modulo.cli.backup._run_psql"))
        stack.enter_context(patch("modulo.cli.backup._ensure_restore_posture"))
        stack.enter_context(patch("modulo.cli.backup._check_collation_versions_sync"))
        result = CliRunner().invoke(backup_cli, ["restore", str(snapshot.directory), "--yes"])
    assert result.exit_code == 0, result.output
    assert "Restore complete" in result.output
    assert "Database restored from SQL dump" in result.output
