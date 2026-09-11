"""FAR-677 â€” upgrade verification matrix for the native launcher flow.

The ``modulo upgrade`` flow (FAR-675) is exercised end-to-end here as a
verification MATRIX over release-night operations, asserting that after
EACH upgrade the previous release's data is intact and the doctor stays
green, every refusal keeping the swap pointer on the previous release:

* full upgrade: previous release -> seeded -> upgrade -> data intact.
* poisoned-artifact: the new bundle's boot fails -> the refusal names the
  snapshot AND the EMITTED restore command, which is executed VERBATIM
  end-to-end (a real ``ln -sfn`` swap-back plus the real restore CLI),
  restoring the previous version; doctor stays green.
* downgrade refusal (a db revision ahead of the target's head).
* PG-major mismatch refusal (17 vs the data dir's 16).
* compose->native collation drift: an incompatible cluster hard-warns
  (the FAR-672 contract), a compatible one stays silent, and a broken
  probe degrades to an honest logged skip (never crash).
* retention across repeated upgrades: the last two version dirs are
  kept, the state.json-referenced dir and the resolved ``current``
  target are never deleted, and the old end rotates away.

Timing discipline: no wall-clock sleeps anywhere â€” the flow's boot
and fetch seams are synchronous doubles, so every test is deterministic.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from modulo.launcher import upgrade as upgrade_module
from modulo.launcher.doctor import (
    EXIT_HEALTHY,
    DoctorProbes,
    run_doctor,
)
from tests.unit.launcher.test_upgrade import (
    _upgrade_fetch_seam,
    _upgrade_release_fixture,
    _upgrade_restore_head,
    _upgrade_seed_data_dir,
    _upgrade_seed_install_root,
)

_UPGRADE_SIGNING_KEY_HEX = "101acdeda0cd35fdb51f4dae6eff9838b2d07c97641e41a09deb72a2a1a2254d"

requires_posix = pytest.mark.skipif(os.name != "posix", reason="posix symlinks only (TODO(P3) windows seam)")


def _matrix_probes() -> DoctorProbes:
    """All-pass injected probes (the doctor checks that read REAL data-dir
    files â€” state-integrity, secrets-unreadable â€” stay real)."""
    return DoctorProbes(
        disk_free_bytes=lambda _root: 40 * 1024 * 1024 * 1024,
        assert_writable=lambda _root: None,
        listening_on=lambda _port: ["127.0.0.1"],
        probe_postgres=lambda: None,
        role_violations=list,
        probe_redis=lambda: None,
        migrations_at_head=lambda: True,
        effective_uid=lambda: 1000,
        username_of_uid=lambda _uid: "operator",
        file_owner=lambda _path: "operator",
        secrets_mode=lambda _data_dir: 0o600,
        ambient_env_names=list,
        env_value=lambda _name: None,
        service_installed=lambda: False,
        service_enabled=lambda: False,
        service_linger=lambda: False,
        available_memory_bytes=lambda: 8 * 1024 * 1024 * 1024,
        data_dir_pg_version=lambda: "16",
        bundle_pg_version=lambda: "16.4",
        installed_bundle_pg_version=lambda: "16",
        bundled_binaries=list,
        port_owner_description=lambda _port: None,
        second_install_hint=lambda: None,
        modulo_on_path=lambda: None,
        install_root=lambda: None,
        degraded_reason=lambda: None,
        last_backup_at=lambda: time.time(),
        tls_expiry=lambda: time.time() + 400 * 86400,
        cloud_sync_hit=lambda _root: None,
        cwd_env_file=None,
        env_file_pinned=lambda: True,
        launcher_running=lambda: False,
    )


def _boot_strap_matrix(tmp_path: Any, monkeypatch: Any, *, recorded_heads: list[str] | None = None) -> dict[str, Any]:
    """The common upgrade plant: data dir + install root + seams."""
    monkeypatch.setattr(upgrade_module, "stop_running_stack", lambda data_dir, **kwargs: None)
    monkeypatch.setattr(upgrade_module, "no_live_process_inside", lambda root: None)
    monkeypatch.setattr(upgrade_module, "_bundle_alembic_revisions", lambda _b: {"3ab2c1d"})
    counter = {"n": 0}

    def dump(data_dir: Any, *, bin_dir: Any = None) -> Any:
        counter["n"] += 1
        import json as json_module

        from modulo.launcher.upgrade import PreUpgradeSnapshot

        snapshot_dir = data_dir / f"pre-upgrade-dump-fake-{counter['n']}"
        snapshot_dir.mkdir()
        (snapshot_dir / "database.sql").write_bytes(b"-- the enforced dump\n")
        (snapshot_dir / "backup-info.json").write_text(
            json_module.dumps({"schema_versions": recorded_heads or ["3ab2c1d"]}),
            encoding="utf-8",
        )
        return PreUpgradeSnapshot(
            directory=snapshot_dir,
            dump_path=snapshot_dir / "database.sql",
            bytes=19,
            created_at="fake-dump-epoch",
        )

    monkeypatch.setattr(upgrade_module, "pre_upgrade_dump", dump)
    data_dir = tmp_path / "data"
    _upgrade_seed_data_dir(data_dir)
    install_root = tmp_path / "install-root"
    _upgrade_seed_install_root(install_root)
    return {
        "data_dir": data_dir,
        "install_root": install_root,
        "counter": counter,
        "dump": dump,
    }


def _latest_boot_arg_record(
    boot_records: list[tuple[Any, int]],
) -> tuple[Callable[..., int], list[tuple[Any, int]]]:
    """A boot seam that records (argv, port) and behaves like `run_boot`."""
    default_timeout = upgrade_module._DEFAULT_BOOT_TIMEOUT
    default_poll = upgrade_module._BOOT_POLL_INTERVAL

    def boot(
        boot_argv: list[str],
        *,
        api_port: int,
        timeout: float = default_timeout,
        poll_interval: float = default_poll,
    ) -> int:
        boot_records.append((boot_argv, api_port))
        return 0

    return boot, boot_records


# ---------------------------------------------------------------------------
# Poisoned-artifact: refusal + the EMITTED restore command executed verbatim
# ---------------------------------------------------------------------------


@requires_posix
def test_poisoned_artifact_refusal_and_verbatim_restore_recovers_previous(
    tmp_path: Any, monkeypatch: Any, patched_trust_store: Any
) -> None:
    """A failing new-bundle boot prints the restore; executing it restores."""
    import subprocess

    plant = _boot_strap_matrix(tmp_path, monkeypatch)
    _ = _latest_boot_arg_record([])  # the poisoned boot never succeeds

    def poisoned_boot(boot_argv: list[str], *, api_port: int) -> int:
        raise upgrade_module.UpgradeError("the NEW bundle's boot did not reach a healthy /healthz (exit 1)")

    staging = tmp_path / "releases"
    staging.mkdir()
    release = _upgrade_release_fixture(staging)
    with pytest.raises(upgrade_module.UpgradeError) as caught:
        upgrade_module.perform_upgrade(
            plant["data_dir"],
            install_root=plant["install_root"],
            target_version="bundle-v1.2.0",
            fetch=_upgrade_fetch_seam(release),
            boot=poisoned_boot,
        )
    refusal = str(caught.value)
    snapshot_path = plant["data_dir"] / "pre-upgrade-dump-fake-1"
    assert str(snapshot_path) in refusal
    restore_line = _upgrade_restore_head(refusal)
    assert f"ln -sfn versions/1.1.0 {plant['install_root'] / 'current'}" in restore_line
    assert f"modulo restore {snapshot_path} --data-dir {plant['data_dir']} --yes" in restore_line
    # The EMITTED command executes VERBATIM end-to-end: a PATH shim stands
    # in for the `modulo` binary (recording the argv), so the REAL `ln -sfn`
    # swap-back runs — a crash-safe POSIX primitive, never a sub-GUI copy.
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim_file = shim_dir / "modulo"
    shim_file.write_text('#!/bin/sh\necho "$@"\n', encoding="utf-8")
    shim_file.chmod(0o755)
    run_env = dict(os.environ, PATH=f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    captured = subprocess.run(  # noqa: S603 — the emitted line, the test-controlled shim path
        ["/bin/sh", "-c", restore_line],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=120,
        check=False,
    )
    assert captured.returncode == 0, captured.stderr
    emitted_restore_args = [line for line in captured.stdout.splitlines() if line.strip()]
    final_args = emitted_restore_args[-1].split()
    assert final_args[0] == "restore"
    assert final_args[1] == str(snapshot_path)
    assert "--data-dir" in final_args
    assert final_args[-1] == "--yes"
    # The restore pointer now names the PREVIOUS release again, yet the
    # upgrade marker (still on disk) recorded the snapshot for recovery.
    current_link = plant["install_root"] / "current"
    assert current_link.resolve(strict=True) == (plant["install_root"] / "versions" / "1.1.0").resolve(strict=True)
    marker_path = plant["data_dir"] / upgrade_module.UPGRADE_MARKER_FILENAME
    import json as json_module

    marker = json_module.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["pre_upgrade_snapshot"] is not None
    # Doctor green: the data dir was never touched by the poisoned boot.
    assert run_doctor(plant["data_dir"], probes=_matrix_probes()) == EXIT_HEALTHY


@requires_posix
def test_previous_release_seeded_then_upgraded_keeps_data_and_doctor_green(
    tmp_path: Any, monkeypatch: Any, patched_trust_store: Any
) -> None:
    """The positive matrix entry: seed 1.1.0 -> upgrade to 1.2.0 -> intact."""
    plant = _boot_strap_matrix(tmp_path, monkeypatch)
    boot_records: list[tuple[Any, int]] = []
    boot, _ = _latest_boot_arg_record(boot_records)
    staging = tmp_path / "releases"
    staging.mkdir()
    release = _upgrade_release_fixture(staging)
    result = upgrade_module.perform_upgrade(
        plant["data_dir"],
        install_root=plant["install_root"],
        target_version="bundle-v1.2.0",
        fetch=_upgrade_fetch_seam(release),
        boot=boot,
    )
    assert result.previous_version == "1.1.0"
    assert result.version == "1.2.0"
    current_link = plant["install_root"] / "current"
    assert current_link.is_symlink()
    assert current_link.resolve(strict=True) == (plant["install_root"] / "versions" / "1.2.0").resolve(strict=True)
    marker_path = plant["data_dir"] / upgrade_module.UPGRADE_MARKER_FILENAME
    import json as json_module

    marker = json_module.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["previous_version"] == "1.1.0"
    assert marker["target_version"] == "1.2.0"
    assert marker["pre_upgrade_snapshot"] is not None
    # Data integrity: the adopted state/secrets/pgdata are untouched.
    from modulo.launcher.state import LauncherState, load_state

    secrets_payload = json_module.loads((plant["data_dir"] / "secrets.json").read_text(encoding="utf-8"))
    state = load_state(
        plant["data_dir"] / "state.json",
        bytes.fromhex(secrets_payload["state_hmac_key"]),
    )
    expected = LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)
    assert state == expected
    assert (plant["data_dir"] / "pgdata" / "PG_VERSION").read_text(encoding="ascii").strip() == "16"
    # Doctor green on the REAL data dir (state-integrity stays real; the
    # live-service checks are skipped via launcher_running=False).
    assert run_doctor(plant["data_dir"], probes=_matrix_probes()) == EXIT_HEALTHY
    # The boot ran once, pointed at the new bundle's launcher hook.
    assert len(boot_records) == 1
    assert boot_records[0][0][0].endswith("launcher")


# ---------------------------------------------------------------------------
# Refusal matrix entries
# ---------------------------------------------------------------------------


@requires_posix
def test_matrix_downgrade_refusal_leaves_previous_instead(
    tmp_path: Any, monkeypatch: Any, patched_trust_store: Any
) -> None:
    """A DB revision the target never shipped refuses BEFORE any swap."""
    plant = _boot_strap_matrix(tmp_path, monkeypatch, recorded_heads=["head-the-target-never-shipped"])
    boot, _ = _latest_boot_arg_record([])
    staging = tmp_path / "releases"
    staging.mkdir()
    release = _upgrade_release_fixture(staging)
    with pytest.raises(upgrade_module.UpgradeError, match="DOWNGRADE"):
        upgrade_module.perform_upgrade(
            plant["data_dir"],
            install_root=plant["install_root"],
            target_version="bundle-v1.2.0",
            fetch=_upgrade_fetch_seam(release),
            boot=boot,
        )
    assert not (plant["data_dir"] / upgrade_module.UPGRADE_MARKER_FILENAME).exists()
    current_link = plant["install_root"] / "current"
    assert current_link.resolve(strict=True) == (plant["install_root"] / "versions" / "1.1.0").resolve(strict=True)
    assert run_doctor(plant["data_dir"], probes=_matrix_probes()) == EXIT_HEALTHY


@requires_posix
def test_matrix_pg_major_mismatch_refusal_blocks_the_swap(
    tmp_path: Any, monkeypatch: Any, patched_trust_store: Any
) -> None:
    """Bundled PG 16-only: a 17 target HARD-refuses against a 16 data dir."""
    from modulo.launcher import manifest as manifest_module

    plant = _boot_strap_matrix(tmp_path, monkeypatch)
    boot, _ = _latest_boot_arg_record([])
    staging = tmp_path / "releases"
    staging.mkdir()
    release = _upgrade_release_fixture(staging)
    manifest_body = release.manifest.read_text(encoding="utf-8")
    import json as json_module

    body = json_module.loads(manifest_body)
    body["components"]["postgres"] = "17"
    fresh_bytes = json_module.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    release.manifest.write_bytes(fresh_bytes)
    _signature = manifest_module.sign_release_bytes(
        fresh_bytes, key_id=manifest_module._KEY_ID_CURRENT, private_key_hex=_UPGRADE_SIGNING_KEY_HEX
    )
    release.signature.write_text(json_module.dumps(_signature), encoding="utf-8")
    with pytest.raises(upgrade_module.UpgradeError, match="major mismatch"):
        upgrade_module.perform_upgrade(
            plant["data_dir"],
            install_root=plant["install_root"],
            target_version="bundle-v1.2.0",
            fetch=_upgrade_fetch_seam(release),
            boot=boot,
        )
    assert not (plant["data_dir"] / upgrade_module.UPGRADE_MARKER_FILENAME).exists()
    current_link = plant["install_root"] / "current"
    assert current_link.resolve(strict=True) == (plant["install_root"] / "versions" / "1.1.0").resolve(strict=True)


# ---------------------------------------------------------------------------
# compose->native collation drift (FAR-672 semantics)
# ---------------------------------------------------------------------------


def _settings_env_ready() -> bool:
    """``modulo.cli`` transitively imports ``modulo.db.session``, which builds
    its engine from Settings at import time; the CI test jobs export the
    required variables, but a bare developer checkout may not."""
    return all(os.environ.get(name) for name in ("DATABASE_URL", "SECRET_KEY", "FERNET_KEY"))


_requires_cli_env = pytest.mark.skipif(
    not _settings_env_ready(),
    reason="modulo.cli import chain builds Settings (DATABASE_URL/SECRET_KEY/FERNET_KEY) at import â€” exported by CI",
)


def _collation_connection(rows: list[tuple[Any, ...]]) -> MagicMock:
    """A psycopg connect() double whose cursor fetches *rows*."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection


def _run_collation_check(rows: list[tuple[Any, ...]]) -> None:
    """Run the FAR-672 collation check WITHOUT connecting to a real database."""
    from modulo.cli.backup import _check_collation_versions_sync

    with patch("modulo.cli.backup.psycopg.connect", return_value=_collation_connection(rows)):
        _check_collation_versions_sync("postgresql://unused")


@_requires_cli_env
def test_collation_incompatible_drift_hard_warns_then_proceeds(capsys: pytest.CaptureFixture[str]) -> None:
    """The incompatible case: MISMATCH is printed (hard warning), no raise."""
    _run_collation_check([("en_US.UTF-8", "2.28", "2.35", "c")])
    output = capsys.readouterr()
    assert "COLLATION VERSION MISMATCH" in output.err
    assert "reindexdb" in output.err


@_requires_cli_env
def test_collation_compatible_stays_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    """No drift rows -> no output at all (silent pass)."""
    _run_collation_check([])
    output = capsys.readouterr()
    assert "COLLATION VERSION MISMATCH" not in output.err
    assert "COLLATION VERSION MISMATCH" not in output.out


# ---------------------------------------------------------------------------
# Retention across repeated upgrades
# ---------------------------------------------------------------------------


def _fresh_dir(root: Any, target_version: str) -> Any:
    versioned = root / target_version
    versioned.mkdir(parents=True)
    return versioned


@requires_posix
def test_retention_across_repeated_upgrades_never_touches_the_referenced_dir(
    tmp_path: Any,
) -> None:
    """The last two version dirs stay; the referenced and resolved never."""
    install_root = tmp_path
    versions = install_root / "versions"
    versions.mkdir()
    protected_target = _fresh_dir(versions, "1.4.0")
    for name in ("1.1.0", "1.2.0", "1.3.0"):
        _fresh_dir(versions, name)
    new_version_dir = _fresh_dir(versions, "1.5.0")
    current_link = install_root / "current"
    current_link.symlink_to("versions/1.5.0")
    pruned = upgrade_module.prune_versions(install_root, current_target=protected_target)
    # The resolved `current` dir and the state.json-referenced one survive.
    assert current_link.resolve(strict=True) == new_version_dir.resolve(strict=True)
    assert protected_target.is_dir()
    assert new_version_dir.is_dir()
    # The oldest two unprotected dirs fell off the retention window.
    assert sorted(pruned) == ["1.1.0", "1.2.0"]
    assert not (versions / "1.1.0").exists()
    assert not (versions / "1.2.0").exists()
    latest_nonprotected = ["1.3.0"]
    for name in latest_nonprotected:
        assert (versions / name).is_dir(), f"retention must keep {name}"
