"""Unit tests for the doctor-lite module (FAR-671 slice 3) and the
full-doctor extension (FAR-676).

Each check is a pure function over injected probes: lock pass/fail paths per
check, fault cases with actionable detail (redis stopped, wrong port,
unwritable dir, foreign bind), the never-crash contract (a probe exception
becomes a failed check), and the documented exit-code table (0 healthy,
1 unhealthy, 2 degraded, 3 uninitialized) with a deterministic fault recipe
per code.
"""

import asyncio
import contextlib
import json
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from modulo.launcher import doctor as doctor_module
from modulo.launcher.doctor import (
    EXIT_DEGRADED,
    EXIT_HEALTHY,
    EXIT_UNHEALTHY,
    EXIT_UNINITIALIZED,
    DoctorProbes,
    apply_fixes,
    check_ambient_pg_env,
    check_bundle_versions,
    check_bundled_binaries,
    check_cloud_sync_root,
    check_cwd_env_influence,
    check_data_dir,
    check_degraded,
    check_install_shadows,
    check_memory_headroom,
    check_migrations,
    check_port_collisions,
    check_ports,
    check_postgres,
    check_privileges,
    check_provider_reachability,
    check_redis,
    check_secrets_permissions,
    check_service_identity,
    check_settings_source,
    check_stale_backup,
    check_tls_expiry,
    default_probes,
    run_doctor,
)
from modulo.launcher.entry import PGDATA_DIRNAME
from modulo.launcher.secrets_file import LauncherSecrets, _parse
from modulo.launcher.state import LauncherState, load_state, save_state
from modulo.model_backends.base import HealthResult, ModelBackendBase
from tests.unit.launcher.test_launcher_shared import all_pass_probes


def _probes(**overrides: object) -> DoctorProbes:
    """The all-pass injection — the behavior-identical canonical factory
    lives in the shared launcher-test module."""
    return all_pass_probes(**overrides)


def _state() -> LauncherState:
    return LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)


def _write_state_secrets(tmp_path: Path) -> LauncherSecrets:
    secrets = LauncherSecrets(postgres_password="pg-pw", redis_password="redis-pw", state_hmac_key=bytes(range(32)))
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {
                "postgres_password": secrets.postgres_password,
                "redis_password": secrets.redis_password,
                "state_hmac_key": secrets.state_hmac_key_hex,
            }
        ),
        encoding="utf-8",
    )
    save_state(_state(), tmp_path / "state.json", secrets.state_hmac_key)
    return secrets


# ---------------------------------------------------------------------------
# check 1: data dir
# ---------------------------------------------------------------------------


def test_data_dir_pass_reports_disk_and_writability(tmp_path: Path) -> None:
    result = check_data_dir(tmp_path, _state(), _probes())
    assert result.ok is True
    assert "GiB free" in result.detail


def test_data_dir_fail_below_disk_floor(tmp_path: Path) -> None:
    result = check_data_dir(tmp_path, _state(), _probes(disk_free_bytes=lambda _root: 128 * 1024 * 1024))
    assert result.ok is False
    assert "MiB free" in result.detail
    assert "floor" in result.detail


def test_data_dir_fail_unwritable_dir(tmp_path: Path) -> None:
    def refuse(_root: Path) -> None:
        raise OSError("data dir is not writable by this user")

    result = check_data_dir(tmp_path, _state(), _probes(assert_writable=refuse))
    assert result.ok is False
    assert "writable" in result.detail


# ---------------------------------------------------------------------------
# check 2: ports + loopback bind
# ---------------------------------------------------------------------------


def test_ports_fail_state_unreadable() -> None:
    result = check_ports(Path("/uninitialized"), None, _probes())
    assert result.ok is False
    assert "state.json" in result.detail


def test_ports_skipped_when_launcher_not_running(tmp_path: Path) -> None:
    result = check_ports(tmp_path, _state(), _probes(launcher_running=lambda: False))
    assert result.ok is True
    assert "skipped" in result.detail


def test_ports_fail_missing_listeners(tmp_path: Path) -> None:
    result = check_ports(tmp_path, _state(), _probes(listening_on=lambda _port: []))
    assert result.ok is False
    assert "postgres is NOT listening on configured port 15432" in result.detail


def test_ports_fail_redis_wrong_port(tmp_path: Path) -> None:
    result = check_ports(tmp_path, _state(), _probes(listening_on=lambda _port: []))
    assert result.ok is False
    assert "redis is NOT listening on configured port 16379" in result.detail


def test_ports_fail_foreign_loopback_binding(tmp_path: Path) -> None:
    foreign_bind = ["0.0.0.0"]  # noqa: S104 — simulated foreign bind

    result = check_ports(tmp_path, _state(), _probes(listening_on=lambda _port: foreign_bind))
    assert result.ok is False
    assert "bound outside loopback" in result.detail


def test_ports_pass_loopback_listeners(tmp_path: Path) -> None:
    result = check_ports(tmp_path, _state(), _probes())
    assert result.ok is True


# ---------------------------------------------------------------------------
# check 3: postgres reachable + role posture
# ---------------------------------------------------------------------------


def test_postgres_skipped_when_launcher_not_running() -> None:
    result = check_postgres(Path(), _state(), _probes(launcher_running=lambda: False))
    assert result.ok is True
    assert "skipped" in result.detail


def test_postgres_fail_service_down_with_detail() -> None:
    def down() -> None:
        raise RuntimeError("connection refused")

    result = check_postgres(Path(), _state(), _probes(probe_postgres=down))
    assert result.ok is False
    assert "connection refused" in result.detail


def test_postgres_fail_role_posture_violations() -> None:
    result = check_postgres(Path(), _state(), _probes(role_violations=lambda: ["app role modulo_app has BYPASSRLS"]))
    assert result.ok is False
    assert "posture" in result.detail
    assert "BYPASSRLS" in result.detail


def test_postgres_pass_posture_holds() -> None:
    result = check_postgres(Path(), _state(), _probes())
    assert result.ok is True


# ---------------------------------------------------------------------------
# check 4: redis ping + auth
# ---------------------------------------------------------------------------


def test_redis_fail_when_stopped() -> None:
    def stopped() -> None:
        raise RuntimeError("Connection refused")

    result = check_redis(Path(), _state(), _probes(probe_redis=stopped))
    assert result.ok is False
    assert "Connection refused" in result.detail


def test_redis_fail_bad_auth() -> None:
    def bad_auth() -> None:
        raise RuntimeError("NOAUTH Authentication required")

    result = check_redis(Path(), _state(), _probes(probe_redis=bad_auth))
    assert result.ok is False
    assert "NOAUTH" in result.detail


def test_redis_pass() -> None:
    result = check_redis(Path(), _state(), _probes())
    assert result.ok is True


# ---------------------------------------------------------------------------
# check 5: migrations
# ---------------------------------------------------------------------------


def test_migrations_fail_behind_head() -> None:
    result = check_migrations(Path(), _state(), _probes(migrations_at_head=lambda: False))
    assert result.ok is False
    assert "alembic head" in result.detail


def test_migrations_pass() -> None:
    result = check_migrations(Path(), _state(), _probes())
    assert result.ok is True


# ---------------------------------------------------------------------------
# check 6: CWD .env influence refusal
# ---------------------------------------------------------------------------


def test_env_influence_pass_no_cwd_env() -> None:
    result = check_cwd_env_influence(Path(), _state(), _probes(cwd_env_file=None))
    assert result.ok is True


def test_env_influence_fail_ambient_urls_unpinned(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://foreign/db", encoding="utf-8")
    result = check_cwd_env_influence(tmp_path, _state(), _probes(cwd_env_file=env_file, env_file_pinned=lambda: False))
    assert result.ok is False
    assert "DATABASE_URL" in result.detail


def test_env_influence_pass_ambient_urls_pinned(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://foreign/db", encoding="utf-8")
    result = check_cwd_env_influence(tmp_path, _state(), _probes(cwd_env_file=env_file, env_file_pinned=lambda: True))
    assert result.ok is True


# ---------------------------------------------------------------------------
# check 7: privileges
# ---------------------------------------------------------------------------


def test_privileges_fail_root(tmp_path: Path) -> None:
    result = check_privileges(tmp_path, _state(), _probes(effective_uid=lambda: 0, username_of_uid=lambda _uid: "root"))
    assert result.ok is False
    assert "root" in result.detail


def test_privileges_fail_ownership_mismatch(tmp_path: Path) -> None:
    result = check_privileges(
        tmp_path,
        _state(),
        _probes(effective_uid=lambda: 1000, username_of_uid=lambda _uid: "operator", file_owner=lambda _p: "root"),
    )
    assert result.ok is False
    assert "owned by 'root'" in result.detail


def test_privileges_pass_unprivileged_consistent(tmp_path: Path) -> None:
    result = check_privileges(tmp_path, _state(), _probes())
    assert result.ok is True


def test_privileges_honest_skip_without_uid(tmp_path: Path) -> None:
    result = check_privileges(tmp_path, _state(), _probes(effective_uid=lambda: None))
    assert result.ok is True
    assert "TODO(P3)" in result.detail


# ---------------------------------------------------------------------------
# run_doctor orchestration: exit codes, never-crash, JSON
# ---------------------------------------------------------------------------


def test_state_secrets_roundtrip(tmp_path: Path) -> None:
    secrets = _write_state_secrets(tmp_path)
    loaded = load_state(tmp_path / "state.json", secrets.state_hmac_key)
    assert loaded.postgres_port == _state().postgres_port
    parsed = _parse((tmp_path / "secrets.json").read_bytes())
    assert isinstance(parsed.state_hmac_key, bytes)


def test_run_doctor_healthy_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)
    code = run_doctor(tmp_path, probes=_probes())
    assert code == 0
    assert "healthy: all checks passed" in capsys.readouterr().out


def test_run_doctor_failing_check_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)
    code = run_doctor(
        tmp_path,
        probes=_probes(role_violations=lambda: ["app role modulo_app has BYPASSRLS"]),
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "unhealthy" in out
    assert "postgres" in out


def test_run_doctor_state_unavailable_reports_distinct_failed_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_doctor(tmp_path, probes=_probes())
    # FAR-676: an uninitialized data dir has its own documented exit code (3).
    assert code == EXIT_UNINITIALIZED
    out = capsys.readouterr().out
    for name in ("ports", "postgres", "redis", "migrations"):
        assert f"[FAIL] {name}" in out
    assert "[ok  ] data-dir" in out


def test_run_doctor_crashing_probe_becomes_failed_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)

    def exploding_disk(_root: Path) -> int:
        raise ValueError("probe went sideways")

    code = run_doctor(tmp_path, probes=_probes(disk_free_bytes=exploding_disk))
    assert code == 1
    out = capsys.readouterr().out
    assert "probe went sideways" in out
    assert "[FAIL] data-dir" in out


def test_run_doctor_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)
    run_doctor(tmp_path, as_json=True, probes=_probes())
    payload = json.loads(capsys.readouterr().out)
    assert payload["healthy"] is True
    assert payload["data_dir"] == str(tmp_path)
    names = {item["name"] for item in payload["checks"]}
    assert names >= {"data-dir", "ports", "postgres", "redis", "migrations"}


def test_run_doctor_never_raises_on_any_crash(tmp_path: Path) -> None:
    _write_state_secrets(tmp_path)

    def crashing_disk(_root: Path) -> int:
        raise RuntimeError("disk probe crashed")

    def stopped_redis() -> None:
        raise RuntimeError("redis stopped")

    code = run_doctor(
        tmp_path,
        probes=_probes(disk_free_bytes=crashing_disk, probe_redis=stopped_redis),
    )
    assert code == 1


# ---------------------------------------------------------------------------
# FAR-676 extended checks
# ---------------------------------------------------------------------------


def test_secrets_permissions_fail_world_readable(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")
    result = check_secrets_permissions(tmp_path, None, _probes(secrets_mode=lambda _d: 0o644))
    assert result.ok is False
    assert "chmod 600" in result.detail


def test_secrets_permissions_pass(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")
    result = check_secrets_permissions(tmp_path, None, _probes())
    assert result.ok is True


def test_secrets_permissions_honest_skip_without_mode(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")
    result = check_secrets_permissions(tmp_path, None, _probes(secrets_mode=lambda _d: None))
    assert result.ok is True
    assert "TODO(P3)" in result.detail


def test_ambient_pg_env_warns_on_pg_vars() -> None:
    result = check_ambient_pg_env(Path(), None, _probes(ambient_env_names=lambda: ["PGHOST", "PGPASSWORD"]))
    assert result.ok is True
    assert result.warning is True
    assert "PGHOST" in result.detail


def test_ambient_pg_env_pass_clean() -> None:
    assert check_ambient_pg_env(Path(), None, _probes()).ok is True


def test_settings_source_warns_modulo_db_not_postgres() -> None:
    probes = _probes(env_value=lambda name: {"MODULO_DB": "sqlite"}.get(name))
    result = check_settings_source(Path(), _state(), probes)
    assert result.ok is True
    assert result.warning is True
    assert "MODULO_DB" in result.detail


def test_settings_source_warns_database_url_port_conflict() -> None:
    probes = _probes(
        env_value=lambda name: {"DATABASE_URL": "postgresql://u:p@foreign-host:9999/db"}.get(name),
    )
    result = check_settings_source(Path(), _state(), probes)
    assert result.warning is True
    assert "9999" in result.detail
    assert "15432" in result.detail


def test_settings_source_pass_clean() -> None:
    result = check_settings_source(Path(), _state(), _probes())
    assert result.ok is True
    assert result.warning is False


def test_cloud_sync_root_warns_on_vendor_match(tmp_path: Path) -> None:
    result = check_cloud_sync_root(
        tmp_path,
        None,
        _probes(cloud_sync_hit=lambda _root: "/home/x/Dropbox (matched marker)"),
    )
    assert result.ok is True
    assert result.warning is True
    assert "Dropbox" in result.detail


def test_cloud_sync_root_none_marker(tmp_path: Path) -> None:
    result = check_cloud_sync_root(tmp_path, None, _probes())
    assert result.ok is True
    assert result.warning is False


def test_service_not_installed_is_graceful_pass() -> None:
    result = check_service_identity(Path(), None, _probes(service_installed=lambda: False))
    assert result.ok is True
    assert "not installed" in result.detail


def test_service_installed_but_not_enabled_fails() -> None:
    result = check_service_identity(
        Path(),
        None,
        _probes(service_installed=lambda: True, service_enabled=lambda: False, service_linger=lambda: True),
    )
    assert result.ok is False
    assert "enable" in result.detail


def test_service_installed_without_linger_fails() -> None:
    result = check_service_identity(
        Path(),
        None,
        _probes(service_installed=lambda: True, service_enabled=lambda: True, service_linger=lambda: False),
    )
    assert result.ok is False
    assert "linger" in result.detail


def test_service_installed_pass() -> None:
    result = check_service_identity(
        Path(), None, _probes(service_installed=lambda: True, service_enabled=lambda: True, service_linger=lambda: True)
    )
    assert result.ok is True


def test_memory_fail_below_floor() -> None:
    result = check_memory_headroom(Path(), None, _probes(available_memory_bytes=lambda: 512 * 1024 * 1024))
    assert result.ok is False
    assert "floor" in result.detail


def test_memory_warn_below_comfortable() -> None:
    result = check_memory_headroom(Path(), None, _probes(available_memory_bytes=lambda: 1.5 * 1024 * 1024 * 1024))
    assert result.ok is True
    assert result.warning is True


def test_memory_pass() -> None:
    assert check_memory_headroom(Path(), None, _probes()).ok is True


def test_bundle_version_fail_minor_drift() -> None:
    result = check_bundle_versions(Path(), None, _probes(data_dir_pg_version=lambda: "16.2"))
    assert result.ok is False
    assert "16.2" in result.detail
    assert "16.4" in result.detail


def test_bundle_version_warn_upgrade() -> None:
    result = check_bundle_versions(Path(), None, _probes(installed_bundle_pg_version=lambda: "16.2"))
    assert result.ok is True
    assert result.warning is True
    assert "upgrade" in result.detail


def test_bundle_version_pass_match() -> None:
    assert check_bundle_versions(Path(), None, _probes()).ok is True


def test_bundle_version_pass_major_only_pg_version() -> None:
    # initdb writes PG_VERSION as MAJOR-ONLY ("16"); the bundled binary reports
    # "16.4". A healthy install must not be flagged as drift (regression: the
    # data-dir probe feeds the REAL PG_VERSION content, not an injected "16.4").
    result = check_bundle_versions(
        Path(), None, _probes(data_dir_pg_version=lambda: "16", bundle_pg_version=lambda: "16.4")
    )
    assert result.ok is True


def test_bundle_version_skip_uninitialized() -> None:
    result = check_bundle_versions(Path(), None, _probes(data_dir_pg_version=lambda: None))
    assert result.ok is True


def test_bundled_binaries_fail_zero_byte(tmp_path: Path) -> None:
    quarantine = tmp_path / "postgres"
    quarantine.write_bytes(b"")
    result = check_bundled_binaries(tmp_path, None, _probes(bundled_binaries=lambda: [quarantine]))
    assert result.ok is False
    assert "ZERO bytes" in result.detail


def test_bundled_binaries_fail_not_executable(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("Windows reports the exec bit implicitly; POSIX-only seam (TODO(P3) quarantine detection)")
    binary = tmp_path / "postgres"
    binary.write_bytes(b"#!/bin/sh\n")
    import stat

    binary.chmod(stat.S_IRUSR | stat.S_IWUSR)  # no exec bit
    result = check_bundled_binaries(tmp_path, None, _probes(bundled_binaries=lambda: [binary]))
    assert result.ok is False
    assert "NOT executable" in result.detail


def test_bundled_binaries_pass(tmp_path: Path) -> None:
    import stat

    binary = tmp_path / "postgres"
    binary.write_bytes(b"#!/bin/sh\n")
    if sys.platform == "win32":
        pytest.skip("exec-bit positive case is POSIX-stat-driven")
    binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    result = check_bundled_binaries(tmp_path, None, _probes(bundled_binaries=lambda: [binary]))
    assert result.ok is True


def test_binaries_real_machine_only_note(tmp_path: Path) -> None:
    """No bundled binaries resolved -> honest skip (real-machine-only audit)."""
    result = check_bundled_binaries(tmp_path, None, _probes())
    assert result.ok is True
    assert "real-machine-only" in result.detail


def test_port_collisions_fail_with_attribution() -> None:
    probes = _probes(
        port_owner_description=lambda port: f"system postgres service (pid 42) on port {port}",
        launcher_running=lambda: True,
    )
    result = check_port_collisions(Path(), _state(), probes)
    assert result.ok is False
    assert "system postgres" in result.detail
    assert "15432" in result.detail


def test_port_collisions_skip_when_not_running() -> None:
    result = check_port_collisions(Path(), _state(), _probes(launcher_running=lambda: False))
    assert result.ok is True
    assert "skipped" in result.detail


def test_port_collisions_pass_clean() -> None:
    result = check_port_collisions(Path(), _state(), _probes())
    assert result.ok is True


def test_install_shadows_warn_path_shadowing() -> None:
    result = check_install_shadows(
        Path("/data/modulo/data"), None, _probes(modulo_on_path=lambda: "/usr/local/bin/modulo")
    )
    assert result.ok is True
    assert result.warning is True
    assert "shadow" in result.detail


def test_install_shadows_warn_second_install(tmp_path: Path) -> None:
    hint = f"second native install at {tmp_path}/other"
    result = check_install_shadows(Path(), None, _probes(second_install_hint=lambda: hint))
    assert result.ok is True
    assert result.warning is True


def test_install_shadows_pass() -> None:
    assert check_install_shadows(Path("/data/modulo/data"), None, _probes()).ok is True


def test_degraded_fail_reason_surfaced() -> None:
    result = check_degraded(Path(), None, _probes(degraded_reason=lambda: "child 'postgres' crashed 5 times"))
    assert result.ok is False
    assert "degraded state: child 'postgres' crashed" in result.detail


def test_degraded_pass_clean() -> None:
    assert check_degraded(Path(), None, _probes()).ok is True


def test_tls_fail_expired() -> None:
    result = check_tls_expiry(Path(), None, _probes(tls_expiry=lambda: time.time() - 100))
    assert result.ok is False
    assert "EXPIRED" in result.detail


def test_tls_warn_near_expiry() -> None:
    result = check_tls_expiry(Path(), None, _probes(tls_expiry=lambda: time.time() + 10 * 86400))
    assert result.ok is True
    assert result.warning is True


def test_tls_pass_far_expiry() -> None:
    result = check_tls_expiry(Path(), None, _probes())
    assert result.ok is True
    assert result.warning is False


def test_tls_skip_absent_keypair_explains_no_generator() -> None:
    """MINOR 3: the no-keypair skip is an honest 'feature not shipped' note,
    not a silent pass — it documents that no keypair generator has landed yet."""
    result = check_tls_expiry(Path(), None, _probes(tls_expiry=lambda: None))
    assert result.ok is True
    assert "keypair" in result.detail
    assert "generator" in result.detail


def test_stale_backup_warn_old(tmp_path: Path) -> None:
    result = check_stale_backup(tmp_path, None, _probes(last_backup_at=lambda: time.time() - 30 * 86400))
    assert result.ok is True
    assert result.warning is True
    assert "30" in result.detail


def test_stale_backup_missing_timestamp_is_honest_skip(tmp_path: Path) -> None:
    """No last_backup_at ever recorded -> honest skip (schema v1)."""
    result = check_stale_backup(tmp_path, None, _probes(last_backup_at=lambda: None))
    assert result.ok is True
    assert "schema v1" in result.detail


def test_stale_backup_pass_recent(tmp_path: Path) -> None:
    result = check_stale_backup(tmp_path, None, _probes())
    assert result.ok is True
    assert result.warning is False


# ---------------------------------------------------------------------------
# check 22: provider reachability (FAR-2316)
#
# The probe must drive the configured backend's OWN health check, so these
# tests mock only the HTTP transport (never the probe helper) and then assert
# the real endpoint and auth header the doctor reached. Body-free reporting and
# the credential paths are covered separately.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _mock_provider_clients(handler: Callable[[httpx.Request], httpx.Response]) -> Iterator[None]:
    """Route the provider transports to *handler*, keeping the real URL/auth code.

    Patches the pinned-client factories used by backend construction and by the
    shared health check, plus ``ChatOpenAI`` (the OpenAI-compatible adapter's
    LangChain client), so no socket is opened while the production request
    shape is still built and can be asserted.
    """
    requested: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return handler(request)

    def _factory(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_record))

    with (
        patch("modulo.model_backends.module.pinned_async_client_sync", side_effect=_factory),
        patch("modulo.model_backends.base.pinned_async_client_sync", side_effect=_factory),
        patch("modulo.model_backends.module.ChatOpenAI"),
    ):
        yield


def _provider_row(provider: str, *, name: str = "primary", model_id: str = "model-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        provider=provider,
        model_id=model_id,
        default_params={},
    )


class _FakeSecretsBackend:
    """Minimal SecretsBackend double: returns (or raises) one stored payload."""

    def __init__(self, payload: str | Exception) -> None:
        self._payload = payload

    async def get_secret(self, key: str) -> str:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_provider_reachability_skipped_when_launcher_not_running(tmp_path: Path) -> None:
    _write_state_secrets(tmp_path)
    state = _state()
    result = check_provider_reachability(tmp_path, state, _probes(launcher_running=lambda: False))
    assert result.ok is True
    assert "skipped" in result.detail


def test_provider_reachability_skipped_when_state_unreadable(tmp_path: Path) -> None:
    result = check_provider_reachability(tmp_path, None, _probes())
    assert result.ok is True
    assert result.warning is True
    assert "state.json unreadable" in result.detail


def test_provider_reachability_skipped_when_probe_not_wired(tmp_path: Path) -> None:
    _write_state_secrets(tmp_path)

    result = check_provider_reachability(tmp_path, _state(), _probes())
    assert result.ok is True
    assert "probe not wired" in result.detail


def test_default_probes_wires_the_provider_reachability_probe(tmp_path: Path) -> None:
    """Production wiring: the check must not silently skip in a real doctor run."""
    assert default_probes(tmp_path, _state()).provider_reachability is not None


def test_provider_reachability_reports_probe_failure_instead_of_passing(tmp_path: Path) -> None:
    _write_state_secrets(tmp_path)

    def crashing_probe() -> list[dict[str, object]]:
        raise RuntimeError("probe crashed")

    result = check_provider_reachability(tmp_path, _state(), _probes(provider_reachability=crashing_probe))
    assert result.ok is False
    assert "reachability probe failed: RuntimeError" in result.detail


async def test_togetherai_backend_probes_the_together_endpoint() -> None:
    """Finding 1: the DB enum value ``togetherai`` must reach the together host."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    with _mock_provider_clients(_handler):
        result = await doctor_module._probe_backend_row(
            _provider_row("togetherai", model_id="mixtral"),
            _FakeSecretsBackend(json.dumps({"api_key": "sk-together"})),
        )

    assert result["status"] == "ok"
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.together.xyz/v1/models"
    assert seen[0].headers["authorization"] == "Bearer sk-together"


async def test_unknown_provider_is_skipped_without_probing_openai() -> None:
    """Finding 1: a provider with no adapter must never fall back to api.openai.com."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    with _mock_provider_clients(_handler):
        result = await doctor_module._probe_backend_row(
            _provider_row("replicate"),
            _FakeSecretsBackend(json.dumps({"api_key": "sk-replicate"})),
        )

    assert result["skipped"] is True
    assert result["status"] == doctor_module.SKIPPED_NO_PROBE
    assert not seen


async def test_unbuildable_backend_is_a_failure_not_a_silent_skip() -> None:
    """Finding 4: a backend that cannot be built from its config must fail.

    ``azure_openai`` without ``azure_endpoint`` raises while the hub builds the
    adapter. Reporting that as ``skipped (no probe)`` would let the doctor
    report healthy while every run using the backend fails.
    """
    result = await doctor_module._probe_backend_row(
        _provider_row("azure_openai"),
        _FakeSecretsBackend(json.dumps({"api_key": "sk-azure"})),
    )

    assert result["ok"] is False
    assert result["skipped"] is False
    assert result["status"] == "error configuration (ValueError)"
    summary = doctor_module._summarise_provider_results([result])
    assert summary.ok is False
    assert "error configuration" in summary.detail


async def test_anthropic_backend_probes_models_not_health() -> None:
    """Finding 2: anthropic's real probe is ``/models`` with its own auth headers."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    with _mock_provider_clients(_handler), patch("modulo.model_backends.anthropic.ChatAnthropic"):
        result = await doctor_module._probe_backend_row(
            _provider_row("anthropic", model_id="claude-3"),
            _FakeSecretsBackend(json.dumps({"api_key": "sk-ant-1"})),
        )

    assert result["status"] == "ok"
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.anthropic.com/models"
    assert "/health" not in str(seen[0].url)
    assert seen[0].headers["x-api-key"] == "sk-ant-1"
    assert seen[0].headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in seen[0].headers


async def test_healthy_backends_summarise_to_ok_not_failed() -> None:
    """Finding 2: a healthy deployment must not be reported unhealthy."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    with _mock_provider_clients(_handler), patch("modulo.model_backends.anthropic.ChatAnthropic"):
        secrets_backend = _FakeSecretsBackend(json.dumps({"api_key": "sk-live"}))
        results = [
            await doctor_module._probe_backend_row(_provider_row("openai", name="gpt"), secrets_backend),
            await doctor_module._probe_backend_row(
                _provider_row("anthropic", name="claude", model_id="claude-3"), secrets_backend
            ),
        ]

    summary = doctor_module._summarise_provider_results(results)
    assert summary.ok is True
    assert summary.warning is False
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("response_status", "expected_status"),
    [(401, "error HTTP 401"), (404, "error HTTP 404"), (503, "error HTTP 503")],
)
async def test_health_check_maps_http_status_without_echoing_the_body(
    response_status: int, expected_status: str
) -> None:
    secret = "sk-SECRET-DO-NOT-PRINT"

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(response_status, text=f"provider said: bad key {secret}")

    with _mock_provider_clients(_handler):
        result = await doctor_module._probe_backend_row(
            _provider_row("openai"),
            _FakeSecretsBackend(json.dumps({"api_key": secret})),
        )

    assert result["ok"] is False
    assert result["status"] == expected_status
    assert secret not in result["status"]
    summary = doctor_module._summarise_provider_results([result])
    assert summary.ok is False
    assert secret not in summary.detail


async def test_health_check_maps_timeout() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    with _mock_provider_clients(_handler):
        result = await doctor_module._probe_backend_row(
            _provider_row("openai"),
            _FakeSecretsBackend(json.dumps({"api_key": "sk-live"})),
        )

    assert result["ok"] is False
    assert result["status"] == "timeout"


async def test_health_check_maps_connection_failure() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _mock_provider_clients(_handler):
        result = await doctor_module._probe_backend_row(
            _provider_row("openai"),
            _FakeSecretsBackend(json.dumps({"api_key": "sk-live"})),
        )

    assert result["ok"] is False
    assert result["status"] == "unreachable"


async def test_backend_without_a_health_probe_is_skipped() -> None:
    """Finding: a backend that only has the base inference ping must not be probed."""

    class _NoProbeBackend(ModelBackendBase):
        backend_id = "test/no-probe"

        async def invoke(self, messages: list[object], output_schema: object = None, **kwargs: object) -> object:
            raise NotImplementedError

        def stream(self, messages: list[object], tools: object = None, **kwargs: object) -> object:
            raise NotImplementedError

    result = await doctor_module._run_backend_health_check(_NoProbeBackend(), "bedrock/bedrock")
    assert result["skipped"] is True
    assert result["status"] == doctor_module.SKIPPED_NO_PROBE
    assert result["ok"] is False


async def test_credential_decrypt_failure_is_surfaced_not_swallowed() -> None:
    """Finding 4: a credential failure is reported, never silently an empty dict."""
    with _mock_provider_clients(lambda _request: httpx.Response(200)):
        result = await doctor_module._probe_backend_row(
            _provider_row("openai"),
            _FakeSecretsBackend(ValueError("bad ciphertext")),
        )

    assert result["ok"] is False
    assert result["status"] == "error credentials (ValueError)"
    summary = doctor_module._summarise_provider_results([result])
    assert summary.ok is False


async def test_unparseable_secret_is_reported_as_unresolved() -> None:
    with _mock_provider_clients(lambda _request: httpx.Response(200)):
        result = await doctor_module._probe_backend_row(
            _provider_row("openai"),
            _FakeSecretsBackend("not-json"),
        )

    assert result["status"] == "error credentials (unresolved)"


async def test_clock_drift_is_read_from_the_date_header() -> None:
    server_time = datetime.now(UTC) - timedelta(seconds=30)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"date": format_datetime(server_time, usegmt=True)})

    with _mock_provider_clients(_handler):
        result = await doctor_module._probe_backend_row(
            _provider_row("openai"),
            _FakeSecretsBackend(json.dumps({"api_key": "sk-live"})),
        )

    assert result["ok"] is True
    assert result["drift"] is not None
    assert result["drift"] >= 25
    assert doctor_module._summarise_provider_results([result]).warning is True


async def test_small_clock_drift_is_reported_without_a_warning() -> None:
    server_time = datetime.now(UTC) - timedelta(seconds=1)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"date": format_datetime(server_time, usegmt=True)})

    with _mock_provider_clients(_handler):
        result = await doctor_module._probe_backend_row(
            _provider_row("openai"),
            _FakeSecretsBackend(json.dumps({"api_key": "sk-live"})),
        )

    assert result["ok"] is True
    assert result["drift"] is not None
    assert result["drift"] < doctor_module.PROVIDER_CLOCK_DRIFT_WARNING_SECONDS
    summary = doctor_module._summarise_provider_results([result])
    assert summary.ok is True
    assert summary.warning is False
    assert "(drift " in summary.detail


async def test_missing_date_header_does_not_fail_the_check() -> None:
    with _mock_provider_clients(lambda _request: httpx.Response(200, headers={})):
        result = await doctor_module._probe_backend_row(
            _provider_row("openai"),
            _FakeSecretsBackend(json.dumps({"api_key": "sk-live"})),
        )

    assert result["ok"] is True
    assert result["drift"] is None
    summary = doctor_module._summarise_provider_results([result])
    assert summary.ok is True
    assert summary.warning is False


async def test_proxy_response_header_is_a_warning() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"via": "1.1 proxy.internal"})

    with _mock_provider_clients(_handler):
        result = await doctor_module._probe_backend_row(
            _provider_row("openai"),
            _FakeSecretsBackend(json.dumps({"api_key": "sk-live"})),
        )

    assert result["proxy"] == "1.1 proxy.internal"
    summary = doctor_module._summarise_provider_results([result])
    assert summary.ok is True
    assert summary.warning is True
    assert "proxy: 1.1 proxy.internal" in summary.detail


def test_summary_with_no_backends_is_an_honest_skip() -> None:
    summary = doctor_module._summarise_provider_results([])
    assert summary.ok is True
    assert "no model backends configured" in summary.detail


def test_summary_treats_skipped_backends_as_not_failed() -> None:
    summary = doctor_module._summarise_provider_results(
        [doctor_module._provider_result("bedrock/primary", status=doctor_module.SKIPPED_NO_PROBE, skipped=True)]
    )
    assert summary.ok is True
    assert summary.warning is False


class _FailingSession:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("db down")


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


async def test_engine_is_disposed_when_the_database_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 4: the engine is disposed in a ``finally``, not only on success."""
    engine = _FakeEngine()
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.async_sessionmaker", lambda *_a, **_k: _FailingSession)
    monkeypatch.setattr(
        doctor_module,
        "_build_composed_config",
        lambda *_a, **_k: {
            "DATABASE_ADMIN_URL": "postgresql+asyncpg://u:p@h/db",
            "FERNET_KEY": "unused-in-this-path",
        },
    )

    with pytest.raises(RuntimeError, match="db down"):
        await doctor_module._probe_provider_reachability(Path("/data"), _state())

    assert engine.disposed is True


# ---------------------------------------------------------------------------
# provider reachability (FAR-2316): DB path, secrets fallback, probe errors
# ---------------------------------------------------------------------------


def test_provider_reachability_propagates_cancellation(tmp_path: Path) -> None:
    """A cancelled probe must cancel the doctor, never be reported as a check."""
    _write_state_secrets(tmp_path)

    def _cancel() -> list[dict[str, object]]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        check_provider_reachability(tmp_path, _state(), _probes(provider_reachability=_cancel))


def test_provider_reachability_returns_the_folded_probe_results(tmp_path: Path) -> None:
    _write_state_secrets(tmp_path)
    rows = [doctor_module._provider_result("openai/gpt", status="ok", ok=True)]

    result = check_provider_reachability(tmp_path, _state(), _probes(provider_reachability=lambda: rows))

    assert result.ok is True
    assert result.detail == "openai/gpt: ok"


def test_sync_probe_runs_the_async_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    seen: list[tuple[Path, object]] = []

    async def _fake(data_dir: Path, probe_state: object) -> list[dict[str, object]]:
        seen.append((data_dir, probe_state))
        return [{"name": "openai/gpt", "ok": True, "status": "ok", "skipped": False}]

    monkeypatch.setattr(doctor_module, "_probe_provider_reachability", _fake)

    results = doctor_module._provider_reachability_probe(Path("/data"), state)

    assert results == [{"name": "openai/gpt", "ok": True, "status": "ok", "skipped": False}]
    assert seen == [(Path("/data"), state)]


class _QueryRows:
    """A session result: iterable rows for a raw query, ``.scalars()`` for an ORM one."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[object]:
        return iter(self._rows)

    def scalars(self) -> list[object]:
        return self._rows


class _ScriptedSession:
    """Yields one canned result per ``execute`` call, in order."""

    def __init__(self, results: list[_QueryRows]) -> None:
        self._results = list(results)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, *_args: object, **_kwargs: object) -> _QueryRows:
        return self._results.pop(0)


def _wire_probe_db(monkeypatch: pytest.MonkeyPatch, session: _ScriptedSession) -> _FakeEngine:
    engine = _FakeEngine()
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.async_sessionmaker", lambda *_a, **_k: lambda: session)
    monkeypatch.setattr(
        doctor_module,
        "_build_composed_config",
        lambda *_a, **_k: {
            "DATABASE_ADMIN_URL": "postgresql+asyncpg://u:p@h/db",
            "FERNET_KEY": "fernet-key",
        },
    )
    return engine


async def test_probe_requires_a_composed_admin_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_module, "_build_composed_config", lambda *_a, **_k: {})

    with pytest.raises(RuntimeError, match="DATABASE_ADMIN_URL"):
        await doctor_module._probe_provider_reachability(Path("/data"), _state())


async def test_probe_requires_a_composed_fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor_module,
        "_build_composed_config",
        lambda *_a, **_k: {"DATABASE_ADMIN_URL": "postgresql+asyncpg://u:p@h/db"},
    )

    with pytest.raises(RuntimeError, match="FERNET_KEY"):
        await doctor_module._probe_provider_reachability(Path("/data"), _state())


async def test_probe_returns_empty_when_no_organisation_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _ScriptedSession([_QueryRows([])])
    engine = _wire_probe_db(monkeypatch, session)

    results = await doctor_module._probe_provider_reachability(Path("/data"), _state())

    assert not results
    assert engine.disposed is True


async def test_probe_reads_every_org_and_probes_its_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    row_a = _provider_row("openai", name="a")
    row_b = _provider_row("openai", name="b")
    session = _ScriptedSession([_QueryRows([(org_a,), (org_b,)]), _QueryRows([row_a]), _QueryRows([row_b])])
    engine = _wire_probe_db(monkeypatch, session)

    rls_orgs: list[uuid.UUID] = []

    async def _set_rls(_session: object, org_id: uuid.UUID) -> None:
        rls_orgs.append(org_id)

    monkeypatch.setattr("modulo.db.rls.set_rls_org", _set_rls)

    secrets_backend = object()
    built_with: list[tuple[str, object]] = []

    def _build_secrets(fernet_key: str, sess: object) -> object:
        built_with.append((fernet_key, sess))
        return secrets_backend

    monkeypatch.setattr(doctor_module, "_build_doctor_secrets_backend", _build_secrets)

    probed: list[tuple[str, object]] = []

    async def _probe(row: object, backend: object) -> dict[str, object]:
        probed.append((row.name, backend))
        return doctor_module._provider_result(f"openai/{row.name}", status="ok", ok=True)

    monkeypatch.setattr(doctor_module, "_probe_backend_row", _probe)

    results = await doctor_module._probe_provider_reachability(Path("/data"), _state())

    assert [result["name"] for result in results] == ["openai/a", "openai/b"]
    assert rls_orgs == [org_a, org_b]
    assert built_with == [("fernet-key", session)]
    assert probed == [("a", secrets_backend), ("b", secrets_backend)]
    assert engine.disposed is True


def test_build_doctor_secrets_backend_returns_the_configured_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    calls: list[tuple[str, object]] = []

    def _create(*, fernet_key: str, session: object) -> object:
        calls.append((fernet_key, session))
        return sentinel

    monkeypatch.setattr("modulo.core.secrets_backend.create_secrets_backend", _create)
    session = MagicMock()

    assert doctor_module._build_doctor_secrets_backend("fernet-key", session) is sentinel
    assert calls == [("fernet-key", session)]


def test_build_doctor_secrets_backend_falls_back_to_the_db_fernet_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured external backend that cannot initialise must not blank credentials."""
    from cryptography.fernet import Fernet

    from modulo.core.secrets_backend.fernet import FernetSecretsBackend

    def _broken(*, fernet_key: str, session: object) -> object:
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr("modulo.core.secrets_backend.create_secrets_backend", _broken)
    session = MagicMock()

    backend = doctor_module._build_doctor_secrets_backend(Fernet.generate_key().decode(), session)

    assert isinstance(backend, FernetSecretsBackend)


async def test_probe_backend_row_skips_a_provider_with_no_probe() -> None:
    result = await doctor_module._probe_backend_row(
        _provider_row("custom"), _FakeSecretsBackend(json.dumps({"api_key": "k"}))
    )

    assert result["skipped"] is True
    assert result["status"] == doctor_module.SKIPPED_NO_PROBE


async def test_probe_backend_row_propagates_credential_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _cancel(*_args: object, **_kwargs: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr("modulo.core.model_backend_hub._resolve_backend_credentials", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await doctor_module._probe_backend_row(_provider_row("openai"), _FakeSecretsBackend("{}"))


async def test_probe_backend_row_propagates_build_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _creds(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"api_key": "k"}

    def _cancel(*_args: object, **_kwargs: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr("modulo.core.model_backend_hub._resolve_backend_credentials", _creds)
    monkeypatch.setattr("modulo.core.model_backend_hub._instantiate_backend", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await doctor_module._probe_backend_row(_provider_row("openai"), _FakeSecretsBackend("{}"))


async def test_probe_backend_row_reports_an_unresolvable_endpoint_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _creds(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"api_key": "k"}

    def _unresolvable(*_args: object, **_kwargs: object) -> object:
        raise ValueError("DNS resolution failed for api.example.com")

    monkeypatch.setattr("modulo.core.model_backend_hub._resolve_backend_credentials", _creds)
    monkeypatch.setattr("modulo.core.model_backend_hub._instantiate_backend", _unresolvable)

    result = await doctor_module._probe_backend_row(_provider_row("openai"), _FakeSecretsBackend("{}"))

    assert result["ok"] is False
    assert result["skipped"] is False
    assert result["status"] == "unreachable (ValueError)"


class _ProbeBackend(ModelBackendBase):
    """A backend whose own health check is overridden (so it is probed, not skipped)."""

    backend_id = "test/probe"

    def __init__(
        self,
        *,
        raises: BaseException | None = None,
        result: HealthResult | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._raises = raises
        self._result = result
        self._close_error = close_error
        self.closed = False

    async def invoke(self, messages: list[object], output_schema: object = None, **kwargs: object) -> object:
        raise NotImplementedError

    def stream(self, messages: list[object], tools: object = None, **kwargs: object) -> object:
        raise NotImplementedError

    async def health_check(self) -> HealthResult:
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result

    async def aclose(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


async def test_health_check_maps_an_immediate_timeout() -> None:
    backend = _ProbeBackend(raises=TimeoutError("slow"))

    result = await doctor_module._run_backend_health_check(backend, "openai/gpt")

    assert result["ok"] is False
    assert result["status"] == "timeout"
    assert backend.closed is True


async def test_health_check_maps_a_raising_probe_to_unreachable() -> None:
    backend = _ProbeBackend(raises=RuntimeError("boom"))

    result = await doctor_module._run_backend_health_check(backend, "openai/gpt")

    assert result["ok"] is False
    assert result["status"] == "unreachable (RuntimeError)"
    assert backend.closed is True


async def test_health_check_propagates_cancellation() -> None:
    backend = _ProbeBackend(raises=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await doctor_module._run_backend_health_check(backend, "openai/gpt")

    assert backend.closed is True


async def test_health_check_close_failure_does_not_fail_the_result() -> None:
    backend = _ProbeBackend(result=HealthResult(ok=True), close_error=RuntimeError("close boom"))

    result = await doctor_module._run_backend_health_check(backend, "openai/gpt")

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert backend.closed is True


# ---------------------------------------------------------------------------
# state-integrity: DISTINCT corrupt vs hmac-mismatch (via run_doctor)
# ---------------------------------------------------------------------------


def test_state_corrupt_is_distinct_from_hmac_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "secrets.json").write_text(
        json.dumps({"postgres_password": "p", "redis_password": "r", "state_hmac_key": "0" * 64}),
        encoding="utf-8",
    )
    # corrupt: valid HMAC envelope missing entirely (torn) — write garbage
    (tmp_path / "state.json").write_text("}not json{", encoding="utf-8")
    assert run_doctor(tmp_path, probes=_probes()) == EXIT_UNHEALTHY
    out = capsys.readouterr().out
    assert "[FAIL] state-integrity" in out
    assert "CORRUPT" in out

    # tampered envelope (json parses) -> hmac-mismatch language
    keys = "0" * 64
    save_state(_state(), tmp_path / "state.json", bytes.fromhex(keys))
    envelope = json.loads((tmp_path / "state.json").read_text())
    envelope["mac"] = "ff" * 32
    (tmp_path / "state.json").write_text(json.dumps(envelope), encoding="utf-8")
    assert run_doctor(tmp_path, probes=_probes()) == EXIT_UNHEALTHY
    out = capsys.readouterr().out
    assert "HMAC verification" in out


# ---------------------------------------------------------------------------
# --fix
# ---------------------------------------------------------------------------


def test_apply_fix_sweeps_orphan_and_sweeps_debris(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    stale_pid = pgdata / "postmaster.pid"
    stale_pid.write_text("99999\n0\n-1\n", encoding="utf-8")  # dead PID => stale
    (pgdata.parent / ".redis-conf-deadbeef").write_text("requirepass x", encoding="utf-8")

    import modulo.launcher.supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "_is_postgres_process", lambda _pid: False)
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: False)
    actions = apply_fixes(tmp_path)
    assert any("removed_stale_postmaster_pid" in action for action in actions), actions
    assert not stale_pid.exists()
    assert not (pgdata.parent / ".redis-conf-deadbeef").exists()


def test_apply_fix_refuses_live_postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "postmaster.pid").write_text("99999\n0\n0\n", encoding="utf-8")

    import modulo.launcher.supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "_is_postgres_process", lambda _pid: True)
    monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: True)
    with pytest.raises(RuntimeError, match="refused"):
        apply_fixes(tmp_path)


# ---------------------------------------------------------------------------
# documented exit-code table: every code has a deterministic fault recipe
# ---------------------------------------------------------------------------


def _exit_recipe(code: int, tmp_path: Path) -> int:
    """Deterministic, CI-automatable fault recipe per documented exit code.

    (Every documented code is mapped here; the real-machine-only recipes —
    e.g. an actual AV-quarantined binary — are annotated on the checks
    themselves and never silently.)
    """
    if code == EXIT_HEALTHY:
        _write_state_secrets(tmp_path)
        return run_doctor(tmp_path, probes=_probes())
    if code == EXIT_UNHEALTHY:
        _write_state_secrets(tmp_path)
        probes = _probes(probe_redis=lambda: (_ for _ in ()).throw(RuntimeError("redis stopped")))
        return run_doctor(tmp_path, probes=probes)
    if code == EXIT_DEGRADED:
        _write_state_secrets(tmp_path)
        return run_doctor(
            tmp_path,
            probes=_probes(last_backup_at=lambda: time.time() - 30 * 86400),
        )
    if code == EXIT_UNINITIALIZED:
        # secrets.json + state.json absent -> uninitialized data dir
        return run_doctor(tmp_path, probes=_probes())
    raise AssertionError(f"undocumented doctor exit code: {code}")


@pytest.mark.parametrize(
    ("code", "recipe_name"),
    [
        pytest.param(EXIT_HEALTHY, "default probes back the happy path (CI-automatable)", id="exit-0-healthy"),
        pytest.param(EXIT_UNHEALTHY, "redis stopped (CI-automatable)", id="exit-1-unhealthy"),
        pytest.param(EXIT_DEGRADED, "stale backup warning (CI-automatable)", id="exit-2-degraded"),
        pytest.param(
            EXIT_UNINITIALIZED, "no state/secrets in the data dir (CI-automatable)", id="exit-3-uninitialized"
        ),
    ],
)
def test_documented_exit_codes_are_exhaustive(code: int, recipe_name: str, tmp_path: Path) -> None:
    assert code in {EXIT_HEALTHY, EXIT_UNHEALTHY, EXIT_DEGRADED, EXIT_UNINITIALIZED}
    assert run_exit_code_recipe(code, tmp_path) == code


def run_exit_code_recipe(code: int, tmp_path: Path) -> int:
    return _exit_recipe(code, tmp_path)


DETERMINISTIC_RECIPES = {
    EXIT_HEALTHY: "CI-automatable: healthy default probes",
    EXIT_UNHEALTHY: "CI-automatable: probe_redis raises (redis stopped)",
    EXIT_DEGRADED: "CI-automatable: last_backup_at 30d ago",
    EXIT_UNINITIALIZED: "CI-automatable: empty data dir",
}
REAL_MACHINE_ONLY_RECIPES: dict[int, str] = {}


def test_exit_code_table_backed_by_recipe_registry() -> None:
    """Every documented doctor code has a recipe annotation (test lives IN
    this suite so the exhaustiveness is enforced whenever the table moves)."""
    assert set(DETERMINISTIC_RECIPES.keys()) == {
        EXIT_HEALTHY,
        EXIT_UNHEALTHY,
        EXIT_DEGRADED,
        EXIT_UNINITIALIZED,
    }


# ---------------------------------------------------------------------------
# state via run_doctor orchestration with extended defaults
# ---------------------------------------------------------------------------


def test_run_doctor_degraded_only_warnings_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)
    code = run_doctor(tmp_path, probes=_probes(last_backup_at=lambda: time.time() - 30 * 86400))
    assert code == EXIT_DEGRADED
    assert "stale-backup" in capsys.readouterr().out


def test_run_doctor_json_includes_exit_code_and_warnings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state_secrets(tmp_path)
    run_doctor(tmp_path, as_json=True, probes=_probes(last_backup_at=lambda: time.time() - 30 * 86400))
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == EXIT_DEGRADED
    stale = next(c for c in payload["checks"] if c["name"] == "stale-backup")
    assert stale["warning"] is True
    assert stale["ok"] is True


def test_probe_migrations_at_head_translates_sslmode() -> None:
    """FAR-1440: the doctor's migration probe must translate a preserved
    ``sslmode`` into asyncpg's ``ssl`` connect arg rather than build a raw
    engine (which TypeErrors at first connect on a TLS deploy)."""

    composed = {"DATABASE_URL": "postgresql+asyncpg://u:p@h/db?sslmode=require"}
    (
        _probe_postgres,
        _role_violations,
        _probe_redis,
        migrations_at_head,
    ) = doctor_module._build_database_probes(composed, 5432, "redis://localhost:6379/0")

    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()
    with (
        patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=mock_engine) as create_engine,
        patch("modulo.db.health_checks.db_is_at_migration_head", AsyncMock(return_value=True)),
    ):
        assert migrations_at_head() is True

    assert create_engine.call_args.args[0] == "postgresql+asyncpg://u:p@h/db"
    assert create_engine.call_args.kwargs["connect_args"]["ssl"] == "require"


def test_probe_registry_names_and_order(tmp_path: Path) -> None:
    """The system-probe registry's names, set and order are the contract:
    ``_build_system_probes`` keys its bound probes by these names, and
    ``default_probes`` unpacks the returned tuple positionally. Registration
    is deferred out of import time, so the registry is read through the lazy
    accessor rather than the raw global."""
    names = [name for name, _ in doctor_module._registered_system_probes()]
    assert names == [
        "ambient_env_names",
        "available_memory_bytes",
        "data_dir_pg_version",
        "bundle_pg_version",
        "installed_bundle_pg_version",
        "bundled_binaries",
        "cloud_sync_hit",
        "modulo_on_path",
        "install_root",
        "second_install_hint",
    ]
    built = doctor_module._build_system_probes(tmp_path)
    assert len(built) == len(names)


def test_register_probe_rejects_duplicate_name() -> None:
    """A duplicate probe name fails loud: ``_build_system_probes`` keys its
    bound probes by name, so a duplicate would silently shadow its
    predecessor."""
    doctor_module._registered_system_probes()  # populate the lazy registry first
    with pytest.raises(ValueError, match="duplicate system-probe name"):
        doctor_module._register_probe("ambient_env_names", lambda _data_dir: None)


# ---------------------------------------------------------------------------
# Extracted module-level system probes (S3776 data-driven registry)
#
# The complexity refactor lifted each system probe out of ``_build_system_probes``
# into a module-level function with a uniform ``(data_dir, ...)`` signature. The
# check-level tests inject stub probes, so these exercise the real
# implementations — including their honest-``None`` failure arms — directly.
# ---------------------------------------------------------------------------


class _StubCompletedProcess:
    """Minimal stand-in for the only attribute ``_probe_bundle_pg_version`` reads."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _force_psutil_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the probe's ``import psutil`` raise, exercising the /proc path."""
    monkeypatch.setitem(sys.modules, "psutil", None)


def test_probe_available_memory_reads_proc_meminfo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """psutil absent -> MemAvailable is parsed from /proc/meminfo (kB -> bytes)."""
    _force_psutil_unavailable(monkeypatch)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 2048 kB\n", encoding="ascii")
    monkeypatch.setattr(doctor_module, "Path", lambda _arg: meminfo)
    assert doctor_module._probe_available_memory_bytes(tmp_path) == 2048 * 1024


def test_probe_available_memory_no_memavailable_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """psutil absent and /proc/meminfo has no MemAvailable line -> honest None."""
    _force_psutil_unavailable(monkeypatch)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1000 kB\n", encoding="ascii")
    monkeypatch.setattr(doctor_module, "Path", lambda _arg: meminfo)
    assert doctor_module._probe_available_memory_bytes(tmp_path) is None


def test_probe_available_memory_unreadable_proc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """psutil absent and /proc unreadable -> honest None, never a crash."""
    _force_psutil_unavailable(monkeypatch)
    monkeypatch.setattr(doctor_module, "Path", lambda _arg: tmp_path / "missing-meminfo")
    assert doctor_module._probe_available_memory_bytes(tmp_path) is None


def test_probe_data_dir_pg_version_read_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A present-but-unreadable PG_VERSION -> honest None, not a crash."""
    pgdata = tmp_path / PGDATA_DIRNAME
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("16\n", encoding="ascii")

    def _raise_oserror(self: Path, **_kwargs: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _raise_oserror)
    assert doctor_module._probe_data_dir_pg_version(tmp_path) is None


def _stub_bundle_bin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Create a bundled ``postgres`` binary and point resolve_bin_dir at it."""
    (tmp_path / "postgres").write_text("", encoding="ascii")
    (tmp_path / "postgres.exe").write_text("", encoding="ascii")
    import modulo.launcher.entry as entry_module

    monkeypatch.setattr(entry_module, "resolve_bin_dir", lambda *_a, **_k: tmp_path)


def test_probe_bundle_pg_version_parses_dotted_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bundled postgres --version output yields its dotted version token."""
    _stub_bundle_bin_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: _StubCompletedProcess("postgres (PostgreSQL) 16.4 (Ubuntu 22.04.3 LTS)"),
    )
    assert doctor_module._probe_bundle_pg_version(tmp_path) == "16.4"


def test_probe_bundle_pg_version_no_version_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Version output carrying no dotted token -> honest None."""
    _stub_bundle_bin_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _StubCompletedProcess("postgres (PostgreSQL)"))
    assert doctor_module._probe_bundle_pg_version(tmp_path) is None


def test_probe_bundle_pg_version_run_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bundled postgres failing to execute -> honest None."""
    _stub_bundle_bin_dir(monkeypatch, tmp_path)

    def _raise_oserror(*_a: object, **_k: object) -> object:
        raise OSError("cannot execute")

    monkeypatch.setattr(subprocess, "run", _raise_oserror)
    assert doctor_module._probe_bundle_pg_version(tmp_path) is None


def test_probe_installed_bundle_pg_version_reads_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A string ``installed_bundle_pg_version`` in the manifest's extra dict is returned."""
    import modulo.launcher.supervisor as supervisor_module

    monkeypatch.setattr(
        supervisor_module,
        "_read_manifest_fields",
        lambda _path: {"extra": {"installed_bundle_pg_version": "16.4"}},
    )
    assert doctor_module._probe_installed_bundle_pg_version(tmp_path) == "16.4"


def test_probe_installed_bundle_pg_version_non_string(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-string version field -> honest None."""
    import modulo.launcher.supervisor as supervisor_module

    monkeypatch.setattr(
        supervisor_module,
        "_read_manifest_fields",
        lambda _path: {"extra": {"installed_bundle_pg_version": 164}},
    )
    assert doctor_module._probe_installed_bundle_pg_version(tmp_path) is None


def test_probe_second_install_hint_none_when_no_siblings(tmp_path: Path) -> None:
    """No state.json/secrets.json sibling install -> honest None."""
    lone = tmp_path / "only-install"
    lone.mkdir()
    assert doctor_module._probe_second_install_hint(lone) is None


def test_probe_cloud_sync_hit_exhausts_ancestor_walk(tmp_path: Path) -> None:
    """A tree deeper than the 5-level walk with no markers -> honest None."""
    deep = tmp_path
    for name in ("a", "b", "c", "d", "e"):
        deep = deep / name
    deep.mkdir(parents=True)
    assert doctor_module._probe_cloud_sync_hit(tmp_path, deep) is None


def test_probe_cloud_sync_hit_stops_at_filesystem_root(tmp_path: Path) -> None:
    """The ancestor walk stops at the filesystem root (``parent == current``)
    instead of stepping past it -> honest None, exercising the ``break`` arm."""
    assert doctor_module._probe_cloud_sync_hit(tmp_path, Path("/")) is None


# ---------------------------------------------------------------------------
# A crashing check that is not a plain function
#
# `_CHECKS` is `tuple[Callable[[Path, Any, DoctorProbes], CheckResult], ...]`,
# so a check may be a callable object with no `__name__`. The crash handler read
# `check.__name__`, which made the HANDLER raise: the "check crashed" row was
# lost and the whole `modulo doctor` run aborted with an opaque AttributeError.
# ---------------------------------------------------------------------------


class _CallableObjectCheck:
    """A doctor check as a callable object — no ``__name__``."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self, _root: Path, _state: object, _probes: object) -> object:
        raise self._exc


def test_crash_handler_survives_a_callable_object_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_state_secrets(tmp_path)

    class _Result:
        ok = True

    monkeypatch.setattr(
        doctor_module,
        "_CHECKS",
        (
            _CallableObjectCheck(RuntimeError("callable-object check crashed")),
            doctor_module.check_data_dir,
        ),
    )

    emitted: list[str] = []
    code = run_doctor(tmp_path, sink=emitted.append)

    # The crash is reported rather than taking the whole run down...
    assert code == 1
    assert any("callable-object check crashed" in line for line in emitted)
    # ...and the surviving check still ran.
    assert any("data-dir" in line for line in emitted)
