"""FAR-672 ÔÇö launcher/upgrade.py: the installer-enforced pre-upgrade pg_dump.

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
    """The dump helper is load-ONLY: a missing secrets file refuses ÔÇö it must
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
    """The dump file is CREATED 0600 ÔÇö a mid-dump SIGKILL never leaves it at
    umask mode."""
    _allow_windows_secrets(monkeypatch)
    data_dir = _seed_data_dir(tmp_path)
    with contextlib.ExitStack() as stack:
        _enter_fake_run(stack)
        snapshot = pre_upgrade_dump(data_dir)
    assert (snapshot.dump_path.stat().st_mode & 0o777) == 0o600


def test_dump_password_is_never_in_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The postgres password travels ONLY as child-env PGPASSWORD ÔÇö never in
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
    NEWER launcher" refusal) ÔÇö not an "unknown field(s)" integrity error."""
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

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:pw@127.0.0.1:15432/modulo")
    monkeypatch.setenv("SECRET_KEY", "dev-secret-key-not-a-real-production-value-0001")
    monkeypatch.setenv("FERNET_KEY", "MrqQRrGCmUJ8YjA1OZ0mu23deGU0ZgGLT9oFyVAmA_k=")

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


# ---------------------------------------------------------------------------
# FAR-675 - the full `modulo upgrade` flow walkthrough tests.
# Every helper uses function-local imports (no mid-file module-level imports).
# ---------------------------------------------------------------------------

from modulo.launcher import upgrade as upgrade_module  # noqa: E402 (appended flow tests)

_UPGRADE_SIGNING_KEY_HEX = "101acdeda0cd35fdb51f4dae6eff9838b2d07c97641e41a09deb72a2a1a2254d"


def _upgrade_release_fixture(staging, target_version="1.2.0"):
    """A staged target release: tarball + SIGNED manifest + the dev-key .sig."""
    import hashlib
    import json as json_module
    import tarfile as tarfile_module
    from types import SimpleNamespace

    from modulo.launcher import manifest as manifest_module

    bundle_root = staging / f"modulo-{target_version}-linux-amd64"
    (bundle_root / "backend" / "src" / "modulo" / "db" / "migrations").mkdir(parents=True)
    (bundle_root / "VERSION").write_text(f"bundle-v{target_version}\n", encoding="utf-8")
    (bundle_root / "launcher").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for extra in ("pg/bin/postgres", "redis/redis-server"):
        extra_path = bundle_root / extra
        extra_path.parent.mkdir(parents=True, exist_ok=True)
        extra_path.write_bytes(b"bundled binary bytes\n")
    artifact_names = ("launcher", "VERSION", "SHA256SUMS", "pg/bin/postgres", "redis/redis-server")
    (bundle_root / "SHA256SUMS").write_text("", encoding="utf-8")
    digests = {
        artifact: hashlib.sha256((bundle_root / artifact).read_bytes()).hexdigest() for artifact in artifact_names
    }
    sums_payload = "".join(f"{digest}  {artifact}\n" for artifact, digest in digests.items())
    (bundle_root / "SHA256SUMS").write_text(sums_payload, encoding="utf-8")
    tarball_path = staging / f"modulo-{target_version}-linux-amd64.tar.gz"
    with tarfile_module.open(tarball_path, "w:gz") as archive:
        archive.add(str(bundle_root), arcname=bundle_root.name)
    manifest_body = {
        "manifest_version": 1,
        "release": f"bundle-v{target_version}",
        "platform": "linux-amd64",
        "generated_at": "2026-09-10T00:00:00Z",
        "components": {"postgres": "16.10.1", "redis": "8.4.0", "python": "3.12.11"},
        "artifacts": [
            {
                "name": tarball_path.name,
                "sha256": hashlib.sha256(tarball_path.read_bytes()).hexdigest(),
                "version": target_version,
            },
            {
                "name": "pg/bin/postgres",
                "sha256": digests["pg/bin/postgres"],
                "version": "16.10.1",
            },
            {
                "name": "redis/redis-server",
                "sha256": digests["redis/redis-server"],
                "version": "8.4.0",
            },
        ],
    }
    manifest_payload = json_module.dumps(manifest_body, indent=2, sort_keys=True).encode("utf-8")
    manifest_path = staging / "RELEASE_MANIFEST.json"
    manifest_path.write_bytes(manifest_payload)
    signature = manifest_module.sign_release_bytes(
        manifest_payload, key_id=manifest_module._KEY_ID_CURRENT, private_key_hex=_UPGRADE_SIGNING_KEY_HEX
    )
    signature_path = staging / (manifest_module.MANIFEST_NAME + manifest_module.SIGNATURE_SUFFIX)
    signature_path.write_text(json.dumps(signature), encoding="utf-8")
    return SimpleNamespace(tarball=tarball_path, manifest=manifest_path, signature=signature_path)


def _upgrade_fetch_seam(release_fixture):
    """The flow's fetch seam (the staged release copies to the download paths)."""
    import shutil

    def _fetch(url, target):
        mapping = {
            release_fixture.tarball.name: release_fixture.tarball,
            release_fixture.manifest.name: release_fixture.manifest,
            "RELEASE_MANIFEST.json.sig": release_fixture.signature,
        }
        shutil.copyfile(mapping[url.rsplit("/", 1)[-1]], target)

    return _fetch


def _upgrade_dump_seam(data_dir, *, recorded_heads=None):
    """The ENFORCED pre-upgrade dump seam (a verified, non-empty snapshot)."""
    import json as json_module

    from modulo.launcher.upgrade import PreUpgradeSnapshot

    def _dump(data_dir, *, bin_dir=None):
        snapshot_dir = data_dir / "pre-upgrade-dump-fake-1"
        snapshot_dir.mkdir()
        (snapshot_dir / "database.sql").write_bytes(b"-- the enforced dump\n")
        (snapshot_dir / "manifest.json").write_text(json_module.dumps({"dump_bytes": 19}), encoding="utf-8")
        (snapshot_dir / "backup-info.json").write_text(
            json_module.dumps({"schema_versions": recorded_heads or ["3ab2c1d"], "dump_bytes": 19}),
            encoding="utf-8",
        )
        return PreUpgradeSnapshot(
            directory=snapshot_dir,
            dump_path=snapshot_dir / "database.sql",
            bytes=19,
            created_at="fake-dump-epoch",
        )

    return _dump


def _upgrade_boot_seam(record):

    default_timeout = upgrade_module._DEFAULT_BOOT_TIMEOUT
    default_poll = upgrade_module._BOOT_POLL_INTERVAL

    def _boot(boot_argv, *, api_port, timeout=default_timeout, poll_interval=default_poll):
        record.append((boot_argv, api_port))
        return 0

    return _boot


def _upgrade_poisoned_boot_seam(record):

    default_timeout = upgrade_module._DEFAULT_BOOT_TIMEOUT
    default_poll = upgrade_module._BOOT_POLL_INTERVAL

    def _boot(boot_argv, *, api_port, timeout=default_timeout, poll_interval=default_poll):
        record.append((boot_argv, api_port))
        raise upgrade_module.UpgradeError("bootstrap poisoned: stubbed backend exits at boot")

    return _boot


def _upgrade_restore_head(text):
    """The 'Exact manual restore command:' line's full payload."""
    marker_line = next(line for line in text.splitlines() if line.strip().startswith("Exact manual restore command:"))
    return marker_line.strip().removeprefix("Exact manual restore command:").strip()


def _upgrade_seed_install_root(install_root, previous_version="1.1.0"):
    """An existing install: versions/<previous> + a resolvable `current` link."""
    previous_dir = install_root / "versions" / previous_version
    (previous_dir / "pg" / "bin").mkdir(parents=True)
    (previous_dir / "launcher").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (install_root / "current").symlink_to(f"versions/{previous_version}")


def _upgrade_seed_data_dir(data_dir):
    """A bootstrapped-shaped data dir (state.json + secrets + pgdata)."""
    import json as json_module

    from modulo.launcher.state import LauncherState, save_state

    hmac_key = bytes(range(32))
    data_dir.mkdir()
    (data_dir / "secrets.json").write_text(
        json_module.dumps(
            {
                "postgres_password": "pw",
                "redis_password": "rw",
                "state_hmac_key": hmac_key.hex(),
            }
        ),
        encoding="utf-8",
    )
    save_state(
        LauncherState(postgres_port=15432, redis_port=16379, api_port=18000),
        data_dir / "state.json",
        hmac_key,
    )
    pgdata = data_dir / "pgdata"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("16\n", encoding="utf-8")


@pytest.fixture
def upgrade_plant(tmp_path, monkeypatch):
    from types import SimpleNamespace

    """The FULL upgrade plant: staged release + data dir + install root."""
    if os.name != "posix":
        pytest.skip("posix symlinks only (TODO(P3) windows seam)")
    monkeypatch.setattr(upgrade_module, "assert_upgrade_platform", lambda: None)
    monkeypatch.setattr(upgrade_module, "stop_running_stack", lambda data_dir, **kwargs: None)
    monkeypatch.setattr(upgrade_module, "no_live_process_inside", lambda root: None)
    data_dir = tmp_path / "data"
    _upgrade_seed_data_dir(data_dir)
    install_root = tmp_path / "install-root"
    _upgrade_seed_install_root(install_root)
    staging = tmp_path / "releases"
    staging.mkdir()
    release_fixture = _upgrade_release_fixture(staging)
    boot_record = []
    return SimpleNamespace(
        data_dir=data_dir,
        install_root=install_root,
        release=release_fixture,
        fetch=None,
        boot=None,
        boot_record=boot_record,
    )


def test_perform_upgrade_full_flow(upgrade_plant):
    """Fetch -> verify -> pre-flight -> dump -> stop -> swap -> marker -> boot."""
    boot_record = []

    default_timeout = upgrade_module._DEFAULT_BOOT_TIMEOUT
    default_poll = upgrade_module._BOOT_POLL_INTERVAL

    def _boot(boot_argv, *, api_port, timeout=default_timeout, poll_interval=default_poll):
        boot_record.append((boot_argv, api_port))
        return 0

    result = upgrade_module.perform_upgrade(
        upgrade_plant.data_dir,
        install_root=upgrade_plant.install_root,
        target_version="bundle-v1.2.0",
        fetch=_upgrade_fetch_seam(upgrade_plant.release),
        boot=_boot,
    )
    assert result.previous_version == "1.1.0"
    assert result.version == "1.2.0"
    assert result.snapshot is not None
    assert result.snapshot.is_dir()
    current_link = upgrade_plant.install_root / "current"
    assert current_link.is_symlink()
    assert current_link.resolve(strict=True) == (upgrade_plant.install_root / "versions" / "1.2.0").resolve(strict=True)
    marker_path = upgrade_plant.data_dir / upgrade_module.UPGRADE_MARKER_FILENAME
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["schema_version"] == upgrade_module.UPGRADE_MARKER_SCHEMA_VERSION
    assert marker["previous_version"] == "1.1.0"
    assert marker["target_version"] == "1.2.0"
    assert marker["pre_upgrade_snapshot"] == str(result.snapshot)
    assert len(boot_record) == 1
    boot_argv, boot_api_port = boot_record[0]
    assert boot_argv[0].endswith("launcher")
    assert boot_argv[1:] == ["start", "--data-dir", str(upgrade_plant.data_dir)]
    assert boot_api_port == 18000


@pytest.mark.skipif(os.name != "posix", reason="posix symlinks only (TODO(P3) windows seam)")
def test_perform_upgrade_pg_major_mismatch_refuses(tmp_path):
    """PG major (17 target) vs the data dir's PG_VERSION (16): HARD refusal."""
    from modulo.launcher import upgrade as upgrade_module

    data_dir = tmp_path / "data"
    install_root = tmp_path / "install-root"
    _upgrade_seed_data_dir(data_dir)
    _upgrade_seed_install_root(install_root)
    staging = tmp_path / "releases"
    staging.mkdir()
    release_fixture = _upgrade_release_fixture(staging)
    manifest_body = json.loads(release_fixture.manifest.read_text(encoding="utf-8"))
    manifest_body["components"]["postgres"] = "17"
    release_fixture.manifest.write_bytes(json.dumps(manifest_body, indent=2, sort_keys=True).encode("utf-8"))
    with pytest.raises(upgrade_module.UpgradeError, match="major mismatch"):
        upgrade_module.perform_upgrade(
            data_dir,
            install_root=install_root,
            target_version="bundle-v1.2.0",
            fetch=_upgrade_fetch_seam(release_fixture),
            boot=lambda boot_argv, *, api_port: 0,
        )
    assert not (data_dir / upgrade_module.UPGRADE_MARKER_FILENAME).exists()


@pytest.mark.skipif(os.name != "posix", reason="posix symlinks only (TODO(P3) windows seam)")
def test_perform_upgrade_downgrade_refuses(tmp_path, monkeypatch):
    from modulo.launcher import upgrade as upgrade_module

    data_dir = tmp_path / "data"
    install_root = tmp_path / "install-root"
    _upgrade_seed_data_dir(data_dir)
    _upgrade_seed_install_root(install_root)
    staging = tmp_path / "releases"
    staging.mkdir()
    release_fixture = _upgrade_release_fixture(staging)

    def _dump(data_dir, *, bin_dir=None):
        return _upgrade_dump_seam(data_dir, recorded_heads=["head-the-target-never-shipped"])(data_dir)

    monkeypatch.setattr(upgrade_module, "pre_upgrade_dump", _dump)
    with pytest.raises(upgrade_module.UpgradeError, match="DOWNGRADE"):
        upgrade_module.perform_upgrade(
            data_dir,
            install_root=install_root,
            target_version="bundle-v1.2.0",
            fetch=_upgrade_fetch_seam(release_fixture),
            boot=lambda boot_argv, *, api_port: 0,
        )
    assert not (data_dir / upgrade_module.UPGRADE_MARKER_FILENAME).exists()


@pytest.mark.skipif(os.name != "posix", reason="posix symlinks only (TODO(P3) windows seam)")
def test_perform_upgrade_disk_preflight_refuses(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from modulo.launcher import upgrade as upgrade_module

    data_dir = tmp_path / "data"
    install_root = tmp_path / "install-root"
    _upgrade_seed_data_dir(data_dir)
    _upgrade_seed_install_root(install_root)
    staging = tmp_path / "releases"
    staging.mkdir()
    release_fixture = _upgrade_release_fixture(staging)

    def _disk_usage(_path):
        return SimpleNamespace(free=0)

    monkeypatch.setattr(upgrade_module.shutil, "disk_usage", _disk_usage)
    with pytest.raises(upgrade_module.UpgradeError, match="Insufficient disk"):
        upgrade_module.perform_upgrade(
            data_dir,
            install_root=install_root,
            target_version="bundle-v1.2.0",
            fetch=_upgrade_fetch_seam(release_fixture),
            boot=lambda boot_argv, *, api_port: 0,
        )
    assert not (data_dir / upgrade_module.UPGRADE_MARKER_FILENAME).exists()


@pytest.mark.skipif(os.name != "posix", reason="posix symlinks only (TODO(P3) windows seam)")
def test_perform_upgrade_live_process_aborts(tmp_path, monkeypatch):
    from modulo.launcher import upgrade as upgrade_module

    def _no_live_refusing(_root):
        raise upgrade_module.UpgradeError("Refusing: still-running processes live inside the target - pid 4242")

    monkeypatch.setattr(upgrade_module, "no_live_process_inside", _no_live_refusing)
    data_dir = tmp_path / "data"
    install_root = tmp_path / "install-root"
    _upgrade_seed_data_dir(data_dir)
    _upgrade_seed_install_root(install_root)
    staging = tmp_path / "releases"
    staging.mkdir()
    release_fixture = _upgrade_release_fixture(staging)
    with pytest.raises(upgrade_module.UpgradeError, match="pid 4242"):
        upgrade_module.perform_upgrade(
            data_dir,
            install_root=install_root,
            target_version="bundle-v1.2.0",
            fetch=_upgrade_fetch_seam(release_fixture),
            boot=lambda boot_argv, *, api_port: 0,
        )
    assert not (data_dir / upgrade_module.UPGRADE_MARKER_FILENAME).exists()
    assert (install_root / "current").resolve(strict=True) == (install_root / "versions" / "1.1.0").resolve(strict=True)


@pytest.mark.skipif(os.name != "posix", reason="posix symlinks + sh (the verbatim-restore E2E)")
def test_perform_upgrade_boot_failure_prints_snapshot_and_restore(tmp_path, monkeypatch):
    """The refusal names the snapshot + the exact restore; the EMITTED command
    executes VERBATIM end-to-end (the PATH shim's `modulo` records its argv)."""
    import subprocess

    from modulo.launcher import upgrade as upgrade_module

    data_dir = tmp_path / "data"
    install_root = tmp_path / "install-root"
    _upgrade_seed_data_dir(data_dir)
    _upgrade_seed_install_root(install_root)
    staging = tmp_path / "releases"
    staging.mkdir()
    release_fixture = _upgrade_release_fixture(staging)

    def _dump(data_dir, *, bin_dir=None):
        return _upgrade_dump_seam(data_dir)(data_dir)

    monkeypatch.setattr(upgrade_module, "pre_upgrade_dump", _dump)
    boot_record = []

    def _poisoned(boot_argv, *, api_port):
        boot_record.append((boot_argv, api_port))
        raise upgrade_module.UpgradeError("the new bundle boot FAILED before reaching a healthy /healthz (exit 1)")

    with pytest.raises(upgrade_module.UpgradeError) as caught_exc:
        upgrade_module.perform_upgrade(
            data_dir,
            install_root=install_root,
            target_version="bundle-v1.2.0",
            fetch=_upgrade_fetch_seam(release_fixture),
            boot=_poisoned,
        )
    refusal_text = str(caught_exc.value)
    assert "pre-upgrade-dump-fake-1" in refusal_text
    restore_line = _upgrade_restore_head(refusal_text)
    assert restore_line.startswith("ln -sfn versions/1.1.0")
    assert f"modulo restore {data_dir / 'pre-upgrade-dump-fake-1'}" in restore_line
    # The EMITTED restore command executes VERBATIM (the shim records argv).
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim_file = shim_dir / "modulo"
    shim_file.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    shim_file.chmod(0o755)
    run_env = dict(os.environ, PATH=f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    captured = subprocess.run(  # noqa: S603 - the emitted line, the test-controlled shim path
        ["/bin/sh", "-c", restore_line],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=30,
        check=False,
    )
    assert captured.returncode == 0, captured.stderr
    captured_lines = [line for line in captured.stdout.splitlines() if line.strip()]
    assert captured_lines[-1] == (f"restore {data_dir / 'pre-upgrade-dump-fake-1'} --data-dir {data_dir} --yes")


@pytest.mark.skipif(os.name != "posix", reason="posix symlinks only (TODO(P3) windows seam)")
def test_upgrade_marker_written_before_boot(tmp_path, monkeypatch):
    from modulo.launcher import upgrade as upgrade_module

    data_dir = tmp_path / "data"
    install_root = tmp_path / "install-root"
    _upgrade_seed_data_dir(data_dir)
    _upgrade_seed_install_root(install_root)
    staging = tmp_path / "releases"
    staging.mkdir()
    release_fixture = _upgrade_release_fixture(staging)

    def _dump(data_dir, *, bin_dir=None):
        return _upgrade_dump_seam(data_dir)(data_dir)

    monkeypatch.setattr(upgrade_module, "pre_upgrade_dump", _dump)
    with pytest.raises(upgrade_module.UpgradeError):
        upgrade_module.perform_upgrade(
            data_dir,
            install_root=install_root,
            target_version="bundle-v1.2.0",
            fetch=_upgrade_fetch_seam(release_fixture),
            boot=_upgrade_poisoned_boot_seam([]),
        )
    marker = json.loads((data_dir / upgrade_module.UPGRADE_MARKER_FILENAME).read_text(encoding="utf-8"))
    assert marker["pre_upgrade_snapshot"] is not None


def test_prune_versions_keeps_last_two_and_never_the_referenced(tmp_path):
    from modulo.launcher import upgrade as upgrade_module

    install_root = tmp_path
    versions = install_root / "versions"
    versions.mkdir()
    for name in ("1.0.0", "1.1.0", "1.2.0", "1.3.0"):
        (versions / name).mkdir()
    referenced = versions / "1.2.0"
    pruned = upgrade_module.prune_versions(install_root, current_target=referenced)
    assert pruned == ["1.0.0"]
    assert (versions / "1.1.0").is_dir()
    assert (versions / "1.2.0").is_dir()
    assert (versions / "1.3.0").is_dir()
    assert referenced.is_dir()
    assert not (versions / "1.0.0").exists()


@pytest.mark.skipif(os.name != "posix", reason="posix symlinks only (TODO(P3) windows seam)")
def test_repair_current_symlink_repairs_a_dangling_link(tmp_path):
    from modulo.launcher import upgrade as upgrade_module

    install_root = tmp_path
    versions = install_root / "versions"
    versions.mkdir()
    (versions / "1.0.0").mkdir()
    (versions / "1.1.0").mkdir()
    current_link = install_root / "current"
    current_link.symlink_to("versions/0.9.9")
    repaired = upgrade_module.repair_current_symlink(install_root)
    assert repaired == "1.1.0"
    assert current_link.resolve(strict=True) == (versions / "1.1.0").resolve(strict=True)


# ---------------------------------------------------------------------------
# FAR-675 QA-gate additions - the blocking findings, locked. Unit-level tests
# run on ANY platform; /proc-buffer/symlink subjects keep their own skipif.
# ---------------------------------------------------------------------------


def _manifest_triple(staging, target_version="1.2.0", *, manifest_release=None):
    """A tarball + SIGNED manifest + .sig triple for the verify-stage seam."""
    import hashlib
    import json as json_module
    import tarfile as tarfile_module
    from types import SimpleNamespace

    from modulo.launcher import manifest as manifest_module

    bundle_root = staging / f"modulo-{target_version}-linux-amd64"
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "SHA256SUMS").write_text("", encoding="utf-8")
    tarball_path = staging / f"modulo-{target_version}-linux-amd64.tar.gz"
    with tarfile_module.open(tarball_path, "w:gz") as archive:
        archive.add(str(bundle_root), arcname=bundle_root.name)
    manifest_body = {
        "manifest_version": 1,
        "release": manifest_release if manifest_release is not None else f"bundle-v{target_version}",
        "platform": "linux-amd64",
        "generated_at": "2026-09-10T00:00:00Z",
        "components": {"postgres": "16.10.1"},
        "artifacts": [
            {
                "name": tarball_path.name,
                "sha256": hashlib.sha256(tarball_path.read_bytes()).hexdigest(),
                "version": target_version,
            }
        ],
    }
    manifest_payload = json_module.dumps(manifest_body, indent=2, sort_keys=True).encode("utf-8")
    manifest_path = staging / "RELEASE_MANIFEST.json"
    manifest_path.write_bytes(manifest_payload)
    signature = manifest_module.sign_release_bytes(
        manifest_payload, key_id=manifest_module._KEY_ID_CURRENT, private_key_hex=_UPGRADE_SIGNING_KEY_HEX
    )
    signature_path = staging / (manifest_module.MANIFEST_NAME + manifest_module.SIGNATURE_SUFFIX)
    signature_path.write_text(json_module.dumps(signature), encoding="utf-8")
    return SimpleNamespace(tarball=tarball_path, manifest=manifest_path, signature=signature_path)


def test_verify_release_stage_happy(tmp_path, patched_trust_store):
    release = _manifest_triple(tmp_path)
    manifest = upgrade_module._verify_release_stage(release.manifest, release.signature, release.tarball, "1.2.0")
    assert manifest.release == "bundle-v1.2.0"


def test_verify_release_stage_refuses_a_cross_version_manifest(tmp_path, patched_trust_store):
    """15j: a manifest whose release field names another bundle never runs."""
    release = _manifest_triple(tmp_path, target_version="1.2.0", manifest_release="bundle-v9.9.9")
    with pytest.raises(upgrade_module.UpgradeError, match="cross-version mismatch"):
        upgrade_module._verify_release_stage(release.manifest, release.signature, release.tarball, "1.2.0")


def test_verify_release_stage_tampered_manifest_is_a_clean_upgrade_error(tmp_path, patched_trust_store):
    """Finding 7: a tampered manifest NEVER escapes as ManifestSecurityError -
    the CLI's error handling turns UpgradeError into a clean ClickException."""
    from modulo.launcher.manifest import ManifestSecurityError

    release = _manifest_triple(tmp_path)
    original = release.manifest.read_bytes()
    release.manifest.write_bytes(original.replace(b"1.2.0-l", b"9.9.9-l", 1))
    with pytest.raises(upgrade_module.UpgradeError) as caught:
        upgrade_module._verify_release_stage(release.manifest, release.signature, release.tarball, "1.2.0")
    assert "release manifest verification FAILED" in str(caught.value)
    assert not isinstance(caught.value, ManifestSecurityError)


def test_verify_release_stage_refuses_a_tampered_tarball(tmp_path, patched_trust_store):
    """The tarball sha256 is checked against the SIGNED manifest's entry."""
    release = _manifest_triple(tmp_path)
    release.tarball.write_bytes(release.tarball.read_bytes() + b"tampered tail bytes")
    with pytest.raises(upgrade_module.UpgradeError, match="sha256 MISMATCH"):
        upgrade_module._verify_release_stage(release.manifest, release.signature, release.tarball, "1.2.0")


def test_check_no_downgrade_never_passes_unknown_through(tmp_path, monkeypatch):
    """Finding 6: "unknown" db heads are NEVER pass-through - a blind check
    refuses (no silent 'unknown == known' downgrade race)."""
    monkeypatch.setattr(upgrade_module, "_bundle_alembic_revisions", lambda _bundle: {"3ab2c1d"})
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    with pytest.raises(upgrade_module.UpgradeError, match="cannot verify the database"):
        upgrade_module.check_no_downgrade(["unknown"], None, bundle_dir)


def test_check_no_downgrade_falls_back_to_the_snapshot_when_live_unknown(tmp_path, monkeypatch):
    """The snapshot's recorded schema_versions answer when the live probe
    reads "unknown"; with no snapshot at ALL the check refuses blind."""
    import json as json_module

    monkeypatch.setattr(upgrade_module, "_bundle_alembic_revisions", lambda _bundle: {"3ab2c1d"})
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    snapshot_dir = tmp_path / "pre-upgrade-dump-1"
    snapshot_dir.mkdir()
    (snapshot_dir / "backup-info.json").write_text(
        json_module.dumps({"schema_versions": ["3ab2c1d"]}), encoding="utf-8"
    )
    upgrade_module.check_no_downgrade(["unknown"], snapshot_dir, bundle_dir)  # falls back: no refusal
    with pytest.raises(upgrade_module.UpgradeError, match="cannot verify the database"):
        upgrade_module.check_no_downgrade(["unknown"], None, bundle_dir)


def test_cli_upgrade_surfaces_a_clean_click_exception(monkeypatch, tmp_path):
    """Finding 7 (CLI level): a verification security error (even one
    leaked past the internal conversion) is reported as `Error: ...` - never
    a raw traceback."""
    from click.testing import CliRunner

    from modulo.cli.main import cli
    from modulo.launcher import manifest as manifest_module

    def _raise(_data_dir, **_kwargs):
        raise manifest_module.ManifestSecurityError("signature FAILED to verify - tampered manifest")

    monkeypatch.setattr(upgrade_module, "perform_upgrade", _raise)
    monkeypatch.setattr(upgrade_module, "default_install_root", lambda: tmp_path / "install-root")
    monkeypatch.setattr("modulo.cli.main._resolve_data_dir", lambda _override: tmp_path / "data")
    result = CliRunner().invoke(cli, ["upgrade", "bundle-v1.2.0"])
    assert result.exit_code == 1
    assert "Error" in result.output
    assert "tampered manifest" in result.output


@pytest.mark.skipif(os.name != "posix", reason="/proc-based liveness scan (Linux-first, P1a)")
def test_no_live_process_excludes_the_upgrading_process_itself(tmp_path, monkeypatch):
    """Finding 3 (NO monkeypatching of the check). The upgrading CLI's own
    cwd sits inside the scanned incumbent dir on every self-upgrade: without
    the self-exclusion the flow aborts on ITSELF. A real child process with
    cwd inside the scanned dir STILL refuses."""
    import subprocess as subprocess_module

    inside = tmp_path / "incumbent"
    inside.mkdir()
    live_child = subprocess_module.Popen(
        ["/bin/sh", "-c", "sleep 5"],
        cwd=str(inside),
        stdout=subprocess_module.DEVNULL,
        stderr=subprocess_module.DEVNULL,
    )
    try:
        with pytest.raises(upgrade_module.UpgradeError, match="live processes remain inside"):
            upgrade_module.no_live_process_inside(inside)
    finally:
        live_child.terminate()
        live_child.wait(timeout=10)
    # The self-exclusion: the UPGRADING process's own cwd inside the dir is
    # NOT an offender.
    monkeypatch.chdir(inside)
    upgrade_module.no_live_process_inside(inside)  # would abort on its own cwd pre-fix


def test_perform_upgrade_skip_backup_runs_no_dump(tmp_path, monkeypatch, patched_trust_store):
    """Finding 6: --skip-backup falls back to the LIVE _alembic_heads()
    probe and NEVER runs the dump again (a stopped stack cannot be dumped)."""
    if os.name != "posix":
        pytest.skip("posix symlinks only (TODO(P3) windows seam)")
    monkeypatch.setattr(upgrade_module, "assert_upgrade_platform", lambda: None)
    monkeypatch.setattr(upgrade_module, "stop_running_stack", lambda data_dir, **kwargs: None)
    monkeypatch.setattr(upgrade_module, "no_live_process_inside", lambda _root: None)
    monkeypatch.setattr(upgrade_module, "_bundle_alembic_revisions", lambda _bundle: {"3ab2c1d"})

    def _dump_must_not_run(_data_dir, *, bin_dir=None):
        raise AssertionError("skip_backup must NEVER call pre_upgrade_dump")

    monkeypatch.setattr(upgrade_module, "pre_upgrade_dump", _dump_must_not_run)
    monkeypatch.setattr(upgrade_module, "_alembic_heads", lambda: ["3ab2c1d"])

    data_dir = tmp_path / "data"
    install_root = tmp_path / "install-root"
    _upgrade_seed_data_dir(data_dir)
    _upgrade_seed_install_root(install_root, previous_version="1.2.0")
    staging = tmp_path / "releases"
    staging.mkdir()
    release_fixture = _upgrade_release_fixture(staging)
    result = upgrade_module.perform_upgrade(
        data_dir,
        install_root=install_root,
        target_version="bundle-v1.2.0",
        skip_backup=True,
        fetch=_upgrade_fetch_seam(release_fixture),
        boot=lambda boot_argv, *, api_port: 0,
    )
    assert result.snapshot is None
    marker = json.loads((data_dir / upgrade_module.UPGRADE_MARKER_FILENAME).read_text(encoding="utf-8"))
    assert marker["pre_upgrade_snapshot"] is None


def test_skip_backup_with_unresolvable_heads_refuses_blind(tmp_path, monkeypatch, patched_trust_store):
    """Finding 6: a skip-backup run whose live probe reads "unknown" REFUSES -
    never a silent 'unknown == known' pass-through."""
    if os.name != "posix":
        pytest.skip("posix symlinks only (TODO(P3) windows seam)")
    monkeypatch.setattr(upgrade_module, "assert_upgrade_platform", lambda: None)
    monkeypatch.setattr(upgrade_module, "stop_running_stack", lambda data_dir, **kwargs: None)
    monkeypatch.setattr(upgrade_module, "no_live_process_inside", lambda _root: None)
    monkeypatch.setattr(upgrade_module, "_bundle_alembic_revisions", lambda _bundle: {"3ab2c1d"})
    monkeypatch.setattr(upgrade_module, "_alembic_heads", lambda: ["unknown"])
    data_dir = tmp_path / "data"
    install_root = tmp_path / "install-root"
    _upgrade_seed_data_dir(data_dir)
    _upgrade_seed_install_root(install_root, previous_version="1.2.0")
    staging = tmp_path / "releases"
    staging.mkdir()
    release_fixture = _upgrade_release_fixture(staging)
    with pytest.raises(upgrade_module.UpgradeError, match="cannot verify the database"):
        upgrade_module.perform_upgrade(
            data_dir,
            install_root=install_root,
            target_version="bundle-v1.2.0",
            skip_backup=True,
            fetch=_upgrade_fetch_seam(release_fixture),
            boot=lambda boot_argv, *, api_port: 0,
        )


def test_prune_versions_never_deletes_the_resolved_current_after_swap(tmp_path):
    """Finding 9: post-swap, `current` is RE-RESOLVED at prune time and its
    (new) target never deleted - even when the pre-swap bookkeeping names a
    different dir AND the new target is old enough to fall outside retention."""
    if os.name != "posix":
        pytest.skip("posix symlinks only (TODO(P3) windows seam)")
    install_root = tmp_path
    versions = install_root / "versions"
    versions.mkdir()
    for name in ("1.0.0", "1.1.0", "1.2.0", "1.3.0", "0.9.9"):
        (versions / name).mkdir()
    stale_bookkeeping = versions / "1.0.0"  # PRE-swap incumbent (state.json heir)
    current_link = install_root / "current"
    current_link.symlink_to("versions/1.0.0")
    # ...then the swap re-points it (exactly as perform_upgrade does) - the
    # re-run/older-version dir sorts BELOW the retention bound:
    current_link.unlink()
    current_link.symlink_to("versions/0.9.9")
    pruned = upgrade_module.prune_versions(install_root, current_target=stale_bookkeeping)
    assert current_link.resolve(strict=True) == (versions / "0.9.9").resolve(strict=True)
    assert (versions / "0.9.9").is_dir(), (
        "retention must never delete the dir `current` now serves (post-swap resolution)"
    )
    assert "0.9.9" not in pruned
    assert "1.0.0" not in pruned


@pytest.mark.skipif(os.name != "posix", reason="posix symlinks only (TODO(P3) windows seam)")
def test_reupgrade_of_the_same_version_keeps_the_prior_bytes_for_restore(tmp_path, monkeypatch, patched_trust_store):
    """Finding 13: a re-run over the SAME version keeps the old binary at
    versions/<v>.prev-<ts>; the printed restore command then points at the
    SURVIVING dir, never at the fresh copy."""
    monkeypatch.setattr(upgrade_module, "assert_upgrade_platform", lambda: None)
    monkeypatch.setattr(upgrade_module, "stop_running_stack", lambda data_dir, **kwargs: None)
    monkeypatch.setattr(upgrade_module, "no_live_process_inside", lambda _root: None)
    monkeypatch.setattr(upgrade_module, "_bundle_alembic_revisions", lambda _bundle: {"3ab2c1d"})

    def _dump(data_dir, *, bin_dir=None):
        return _upgrade_dump_seam(data_dir)(data_dir)

    monkeypatch.setattr(upgrade_module, "pre_upgrade_dump", _dump)
    data_dir = tmp_path / "data"
    install_root = tmp_path / "install-root"
    _upgrade_seed_data_dir(data_dir)
    _upgrade_seed_install_root(install_root, previous_version="1.2.0")
    staging = tmp_path / "releases"
    staging.mkdir()
    release_fixture = _upgrade_release_fixture(staging)

    def _poisoned(boot_argv, *, api_port):
        raise upgrade_module.UpgradeError("the re-run boot FAILED at the health gate")

    with pytest.raises(upgrade_module.UpgradeError) as caught:
        upgrade_module.perform_upgrade(
            data_dir,
            install_root=install_root,
            target_version="bundle-v1.2.0",
            fetch=_upgrade_fetch_seam(release_fixture),
            boot=_poisoned,
        )
    refusal = str(caught.value)
    survivors = [entry.name for entry in (install_root / "versions").iterdir() if ".prev-" in entry.name]
    assert survivors, "the prior same-version bytes must survive the re-run"
    restore_line = _upgrade_restore_head(refusal)
    assert restore_line.startswith(f"ln -sfn versions/{survivors[0]}")
    assert not restore_line.startswith("ln -sfn versions/1.2.0 ")


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_run_boot_timeout_kills_the_whole_process_group():
    """Finding 11 (teardown half): a timed-out boot child is terminated
    WITH its group - the bundled service children are never orphaned."""
    import time as time_module

    child_argv = ["/bin/sh", "-c", "sleep 1 & wait; exec sleep 30"]
    started = time_module.monotonic()
    with pytest.raises(upgrade_module.UpgradeError, match="health"):
        upgrade_module.run_boot(child_argv, api_port=1, timeout=1.0, poll_interval=0.1)
    elapsed = time_module.monotonic() - started
    assert elapsed < 30, "the teardown must never hang on an undrained child"


def test_run_boot_failing_child_reports_clean_upgrade_error(tmp_path):
    """A child that exits immediately produces a clean UpgradeError (and
    the group teardown tolerates an already-dead child)."""
    if os.name != "posix":
        pytest.skip("POSIX-only boot argv")
    failing = tmp_path / "failing-launcher"
    failing.write_text("#!/bin/sh\necho boom >&2\nexit 1\n", encoding="utf-8")
    failing.chmod(0o755)
    with pytest.raises(upgrade_module.UpgradeError, match="did not reach a healthy /healthz"):
        upgrade_module.run_boot([str(failing), "start"], api_port=1, timeout=1.0, poll_interval=0.05)
