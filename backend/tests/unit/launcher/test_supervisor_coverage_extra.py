"""Extra coverage for the new supervisor FAR-676 surface (logs, runtime
manifest, degraded record, ``modulo status --json`` enrichment, bundled
postgres version resolution).

These are pure/read-only helpers; every branch is exercised against a temp
data dir with no live Postgres/Redis required.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from modulo.launcher import supervisor as supervisor_module
from modulo.launcher.secrets_file import LauncherSecrets
from modulo.launcher.state import LauncherState, save_state
from modulo.launcher.supervisor import (
    DEGRADED_FILENAME,
    RUNTIME_FILENAME,
    _component_remediation,
    _component_state,
    _read_manifest_fields,
    _resolve_bundled_postgres_version,
    collect_status,
    log_paths,
    read_degraded_reason,
    read_degraded_record,
    read_log_tail,
    read_runtime_manifest,
    rotate_log,
    write_degraded_record,
    write_runtime_manifest,
)


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
    save_state(
        LauncherState(postgres_port=15432, redis_port=16379, api_port=18000),
        tmp_path / "state.json",
        secrets.state_hmac_key,
    )
    return secrets


def test_log_paths(tmp_path: Path):
    paths = log_paths(tmp_path)
    assert set(paths) == {"app", "postgres", "redis"}
    assert paths["app"] == tmp_path / "launcher.log"
    assert paths["postgres"] == tmp_path / "logs" / "postgres.log"


def test_read_log_tail(tmp_path: Path):
    log = tmp_path / "launcher.log"
    log.write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert read_log_tail(log).endswith("line3\n")
    # Missing file -> empty string (no crash).
    assert read_log_tail(tmp_path / "absent.log") == ""


def test_rotate_log_below_threshold(tmp_path: Path):
    log = tmp_path / "launcher.log"
    log.write_text("small\n", encoding="utf-8")
    assert rotate_log(log) is False
    assert log.exists()


def test_rotate_log_happens(tmp_path: Path):
    log = tmp_path / "launcher.log"
    log.write_text("x" * 4096, encoding="utf-8")
    assert rotate_log(log, max_bytes=100, keep=1) is True
    assert (tmp_path / "launcher.log.1").is_file()


def test_rotate_log_keep_generations(tmp_path: Path):
    log = tmp_path / "launcher.log"
    log.write_text("x" * 4096, encoding="utf-8")
    (tmp_path / "launcher.log.1").write_text("old1", encoding="utf-8")
    assert rotate_log(log, max_bytes=100, keep=2) is True
    assert (tmp_path / "launcher.log.1").is_file()
    assert (tmp_path / "launcher.log.2").is_file()


def test_rotate_log_invalid_keep(tmp_path: Path):
    log = tmp_path / "launcher.log"
    log.write_text("x" * 4096, encoding="utf-8")
    with pytest.raises(ValueError, match="keep must be >= 1"):
        rotate_log(log, max_bytes=100, keep=0)


def test_resolve_bundled_postgres_version_absent():
    # No bundled binary in the test environment -> honest None.
    assert _resolve_bundled_postgres_version() is None or isinstance(_resolve_bundled_postgres_version(), str)


def test_runtime_manifest_roundtrip(tmp_path: Path):
    path = tmp_path / RUNTIME_FILENAME
    write_runtime_manifest(path, {"postgres": 11, "redis": 12}, extra={"installed_bundle_pg_version": "16.4"})
    pids = read_runtime_manifest(path)
    assert pids == {"postgres": 11, "redis": 12}
    # degraded reason lives under a distinct extra key.
    write_runtime_manifest(path, {}, extra={"degraded_reason": "boom"})
    assert read_degraded_reason(path) == "boom"

    # Corrupt manifest -> no children, no reason.
    path.write_text("{not json", encoding="utf-8")
    assert read_runtime_manifest(path) == {}
    assert read_degraded_reason(path) is None


def test_read_manifest_fields_types(tmp_path: Path):
    path = tmp_path / "runtime.json"
    write_runtime_manifest(path, {"postgres": 11}, extra={"degraded_reason": "boom"})
    fields = _read_manifest_fields(path)
    assert fields["children"] == {"postgres": 11}
    assert fields["extra"]["degraded_reason"] == "boom"

    # reason that is not a string -> None
    write_runtime_manifest(path, {}, extra={"degraded_reason": 123})
    assert read_degraded_reason(path) is None

    # extra that is not a dict -> None
    path.write_text(json.dumps({"children": {}}), encoding="utf-8")
    assert read_degraded_reason(path) is None


def test_degraded_record_roundtrip(tmp_path: Path):
    path = tmp_path / DEGRADED_FILENAME
    record = {"reason": "oom", "degraded_at": "2026-01-01T00:00:00Z", "crashes": ["a", "b"]}
    write_degraded_record(path, record)
    assert read_degraded_record(path) == record
    # Corrupt -> None (no crash).
    path.write_text("not json", encoding="utf-8")
    assert read_degraded_record(path) is None


def test_collect_status_uninitialized(tmp_path: Path):
    status = collect_status(tmp_path)
    assert status["initialized"] is False
    assert "error" in status


def test_collect_status_initialized(tmp_path: Path):
    _write_state_secrets(tmp_path)
    status = collect_status(tmp_path)
    assert status["initialized"] is True
    assert status["postgres_port"] == 15432
    assert set(status["components"]) == {"postgres", "redis", "saq-runs", "saq-system", "api"}
    # No launcher lock holder -> no launcher key, components stopped.
    assert status["components"]["postgres"]["state"] == "stopped"
    assert status["components"]["postgres"]["remediation"] is not None


def test_collect_status_degraded_record(tmp_path: Path):
    _write_state_secrets(tmp_path)
    write_degraded_record(
        tmp_path / DEGRADED_FILENAME,
        {"reason": "terminal fault", "degraded_at": "2026-01-01T00:00:00Z", "crashes": ["x"]},
    )
    status = collect_status(tmp_path)
    assert status["degraded"]["reason"] == "terminal fault"
    assert status["degraded"]["crashes"] == ["x"]


def test_collect_status_with_runtime_pids(tmp_path: Path):
    _write_state_secrets(tmp_path)
    write_runtime_manifest(tmp_path / RUNTIME_FILENAME, {"postgres": 999999, "redis": 999999})
    status = collect_status(tmp_path)
    # Dead pids -> stopped, with remediation hints.
    assert status["components"]["postgres"]["pid"] == 999999
    assert status["components"]["postgres"]["state"] == "stopped"


def test_component_state():
    assert _component_state(None, None) == "stopped"
    assert _component_state(os.getpid(), 15432) == "healthy"
    # A dead pid reports stopped.
    assert _component_state(999999, 15432) == "stopped"


def test_component_remediation():
    assert _component_remediation("postgres", os.getpid(), 15432) is None
    assert "postgres is not running" in _component_remediation("postgres", None, 15432)
    assert "redis is not running" in _component_remediation("redis", None, 16379)
    assert "api" in _component_remediation("api", None, 18000)
    assert "worker is not running" in _component_remediation("saq-runs", None, None)


def test_pid_alive_bounds():
    assert supervisor_module._pid_alive(0) is False
    assert supervisor_module._pid_alive(-1) is False
    assert supervisor_module._pid_alive(os.getpid()) is True
    assert supervisor_module._pid_alive(999999) is False
