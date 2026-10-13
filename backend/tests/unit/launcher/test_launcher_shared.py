"""Shared launcher-test helpers (FAR-676 dedup sweep).

Single home for the duplicated DoctorProbes factories, the upgrade-flow
helper seams, the deterministic monotonic clock helper, and the bodies the
per-file autouse reset/bypass fixtures delegate to. Every naming rule:

- ``all_pass_probes`` — the full injection used by the doctor check files
  (test_doctor.py, test_doctor_coverage_extra.py, upgrade-matrix wrapper).
- ``minimal_probes`` — only the ten required probes supply all-pass values;
  every optional probe keeps its dataclass default (used by the
  fault-injection and helpers files).
- ``upgrade_matrix_probes`` — the matrix variant (real ``"16"`` versions,
  nothing installed, launcher not running).
- ``_upgrade_*`` — the staged-release / seed / fetch / dump / restore
  helpers imported back by test_upgrade.py and test_upgrade_matrix.py.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from modulo.launcher.doctor import DoctorProbes

_UPGRADE_SIGNING_KEY_HEX = "101acdeda0cd35fdb51f4dae6eff9838b2d07c97641e41a09deb72a2a1a2254d"


def all_pass_probes(**overrides: object) -> DoctorProbes:
    """The all-pass DoctorProbes injection (exact behavior-identical port)."""
    probes = DoctorProbes(
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
        data_dir_pg_version=lambda: "16.4",
        bundle_pg_version=lambda: "16.4",
        installed_bundle_pg_version=lambda: None,
        bundled_binaries=list,
        port_owner_description=lambda _port: None,
        second_install_hint=lambda: None,
        modulo_on_path=lambda: None,
        install_root=lambda: str(Path("/install")),
        degraded_reason=lambda: None,
        last_backup_at=lambda: time.time(),
        tls_expiry=lambda: time.time() + 400 * 86400,
        cloud_sync_hit=lambda _root: None,
        cwd_env_file=None,
        env_file_pinned=lambda: True,
        launcher_running=lambda: True,
    )
    for key, value in overrides.items():
        setattr(probes, key, value)
    return probes


def minimal_probes(**overrides: object) -> DoctorProbes:
    """Only the ten required probes carry all-pass values; the optional
    probes keep their dataclass defaults. Identity: u=op, owner=op."""
    probes = DoctorProbes(
        disk_free_bytes=lambda _r: 40 * 1024 * 1024 * 1024,
        assert_writable=lambda _r: None,
        listening_on=lambda _p: ["127.0.0.1"],
        probe_postgres=lambda: None,
        role_violations=list,
        probe_redis=lambda: None,
        migrations_at_head=lambda: True,
        effective_uid=lambda: 1000,
        username_of_uid=lambda _u: "op",
        file_owner=lambda _p: "op",
        launcher_running=lambda: True,
    )
    for key, value in overrides.items():
        setattr(probes, key, value)
    return probes


def upgrade_matrix_probes() -> DoctorProbes:
    """All-pass injected probes (the upgrade-matrix deltas: versions "16",
    nothing installed, launcher NOT running)."""
    return all_pass_probes(
        data_dir_pg_version=lambda: "16",
        bundle_pg_version=lambda: "16",
        installed_bundle_pg_version=lambda: "16",
        install_root=lambda: None,
        launcher_running=lambda: False,
    )


def clock_steps(*samples: float) -> Callable[[], float]:
    """A time.monotonic replacement: serve *samples* in order, then hold
    the last. Never wall-clock and never exhausts (a future call-count
    change cannot land StopIteration mid-test)."""
    values = list(samples)
    index = {"i": 0}

    def _now() -> float:
        current = values[min(index["i"], len(values) - 1)]
        index["i"] += 1
        return current

    return _now


def launcher_state_reset() -> Iterator[None]:
    """Snapshot boundaries for the pinned env file / first-boot guard and
    the get_settings cache (yield from inside a per-file autouse fixture)."""
    import modulo.settings as settings_module
    from modulo.settings import get_settings

    get_settings.cache_clear()
    yield
    settings_module._pinned_env_file = None
    settings_module._first_boot_guard = None
    get_settings.cache_clear()


def bypass_platform_seams(monkeypatch: pytest.MonkeyPatch, *module_names: str) -> None:
    """Patch ``assert_supported_platform`` to a no-op on the given launcher
    seam modules (Windows only — the refusal never fires elsewhere)."""
    if sys.platform != "win32":
        return
    import importlib

    for module_name in module_names:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "assert_supported_platform", lambda: None)


def _upgrade_release_fixture(staging: Path, target_version: str = "1.2.0") -> SimpleNamespace:
    """A staged target release: tarball + SIGNED manifest + the dev-key .sig."""
    import hashlib
    import json as json_module
    import tarfile as tarfile_module

    from modulo.launcher import manifest as manifest_module

    bundle_root = staging / f"modulo-{target_version}-linux-amd64"
    (bundle_root / "backend" / "src" / "modulo" / "db" / "migrations").mkdir(parents=True)
    (bundle_root / "VERSION").write_text(f"bundle-v{target_version}\n", encoding="utf-8")
    (bundle_root / "launcher").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for extra in ("pg/bin/postgres", "redis/redis-server"):
        extra_path = bundle_root / extra
        extra_path.parent.mkdir(parents=True, exist_ok=True)
        extra_path.write_bytes(b"bundled binary bytes\n")
    artifact_names = ("launcher", "VERSION", "pg/bin/postgres", "redis/redis-server")
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
    signature_path.write_text(json_module.dumps(signature), encoding="utf-8")
    return SimpleNamespace(tarball=tarball_path, manifest=manifest_path, signature=signature_path)


def _upgrade_fetch_seam(release_fixture: SimpleNamespace) -> Callable[[str, Path], None]:
    """The flow's fetch seam (the staged release copies to the download paths)."""
    import shutil

    def _fetch(url: str, target: Path) -> None:
        mapping = {
            release_fixture.tarball.name: release_fixture.tarball,
            release_fixture.manifest.name: release_fixture.manifest,
            "RELEASE_MANIFEST.json.sig": release_fixture.signature,
        }
        shutil.copyfile(mapping[url.rsplit("/", 1)[-1]], target)

    return _fetch


def _upgrade_dump_seam(data_dir: Path, *, recorded_heads: list[str] | None = None) -> Callable[..., Any]:
    """The ENFORCED pre-upgrade dump seam (a verified, non-empty snapshot)."""
    import json as json_module

    from modulo.launcher.upgrade import PreUpgradeSnapshot

    def _dump(_data_dir: Path, *, bin_dir: Any = None) -> PreUpgradeSnapshot:
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


def _upgrade_restore_head(text: str) -> str:
    """The 'Exact manual restore command:' line's full payload."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("Exact manual restore command:"):
            for subsequent in lines[i + 1 :]:
                if subsequent.strip():
                    return subsequent.strip()
    raise ValueError("no restore command found in refusal text")


def _upgrade_seed_install_root(install_root: Path, previous_version: str = "1.1.0") -> None:
    """An existing install: versions/<previous> + a resolvable `current` link."""
    previous_dir = install_root / "versions" / previous_version
    (previous_dir / "pg" / "bin").mkdir(parents=True)
    (previous_dir / "launcher").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (install_root / "current").symlink_to(f"versions/{previous_version}")


def _upgrade_seed_data_dir(data_dir: Path) -> None:
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
