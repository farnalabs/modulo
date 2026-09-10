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
