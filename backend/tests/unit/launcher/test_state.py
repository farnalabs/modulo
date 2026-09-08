"""Unit tests for the launcher state.json (FAR-671 / ADR 031 Decision 6).

Locks: atomic writes (no partial/torn state, tmp cleaned on failure), HMAC
integrity (tamper + wrong-key refusal), the schema_version downgrade refusal,
credential-free payloads, and the non-default high-port allocation.
"""

import json
from pathlib import Path
from typing import Self

import pytest

from modulo.launcher.state import (
    DEFAULT_API_PORT,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
    SCHEMA_VERSION,
    LauncherState,
    StateIntegrityError,
    StateVersionError,
    find_free_port,
    initial_state,
    load_state,
    save_state,
)


def _key() -> bytes:
    return bytes(range(32))


def _state() -> LauncherState:
    return LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(_state(), path, _key())
    loaded = load_state(path, _key())
    assert loaded == _state()


def test_payload_is_credential_free() -> None:
    payload = _state().to_payload()
    expected_keys = {"schema_version", "postgres_port", "redis_port", "api_port"}
    assert set(payload) == expected_keys
    for name in payload:
        lowered = name.lower()
        assert "password" not in lowered
        assert "secret" not in lowered
        assert "key" not in lowered


def test_envelope_contains_payload_and_mac_only(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(_state(), path, _key())
    envelope = json.loads(path.read_text())
    assert set(envelope) == {"payload", "mac"}


def test_hmac_tamper_detected(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(_state(), path, _key())
    envelope = json.loads(path.read_text())
    envelope["payload"]["postgres_port"] = 5432  # attacker rewrote a port
    path.write_text(json.dumps(envelope))
    with pytest.raises(StateIntegrityError, match="HMAC"):
        load_state(path, _key())


def test_hmac_tampered_mac_detected(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(_state(), path, _key())
    envelope = json.loads(path.read_text())
    envelope["mac"] = "0" * 64
    path.write_text(json.dumps(envelope))
    with pytest.raises(StateIntegrityError, match="HMAC"):
        load_state(path, _key())


def test_wrong_hmac_key_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(_state(), path, _key())
    with pytest.raises(StateIntegrityError, match="HMAC"):
        load_state(path, bytes(reversed(range(32))))


def test_schema_version_downgrade_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = _state()
    payload = state.to_payload()
    payload["schema_version"] = SCHEMA_VERSION + 1  # written by a NEWER launcher
    from modulo.launcher.state import _mac_for

    envelope = {"payload": payload, "mac": _mac_for(payload, _key())}
    path.write_text(json.dumps(envelope))
    with pytest.raises(StateVersionError, match="Downgrade refused"):
        load_state(path, _key())


def test_schema_version_older_than_supported_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = _state().to_payload()
    payload["schema_version"] = SCHEMA_VERSION - 1  # no migration path exists in v1
    from modulo.launcher.state import _mac_for

    envelope = {"payload": payload, "mac": _mac_for(payload, _key())}
    path.write_text(json.dumps(envelope))
    with pytest.raises(StateVersionError, match="predates"):
        load_state(path, _key())


def test_missing_schema_version_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = _state().to_payload()
    payload.pop("schema_version")
    from modulo.launcher.state import _mac_for

    envelope = {"payload": payload, "mac": _mac_for(payload, _key())}
    path.write_text(json.dumps(envelope))
    with pytest.raises(StateVersionError, match="schema_version"):
        load_state(path, _key())


def test_unknown_payload_field_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = _state().to_payload()
    payload["postgres_password"] = "oops-a-credential"  # not part of the contract
    from modulo.launcher.state import _mac_for

    envelope = {"payload": payload, "mac": _mac_for(payload, _key())}
    path.write_text(json.dumps(envelope))
    with pytest.raises(StateIntegrityError, match="unknown field"):
        load_state(path, _key())


def test_corrupt_json_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(StateIntegrityError, match="unreadable"):
        load_state(path, _key())


def test_invalid_port_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = _state().to_payload()
    payload["postgres_port"] = 80  # privileged/well-known port — impossible from the allocator
    from modulo.launcher.state import _mac_for

    envelope = {"payload": payload, "mac": _mac_for(payload, _key())}
    path.write_text(json.dumps(envelope))
    with pytest.raises(StateIntegrityError, match="not a valid port"):
        load_state(path, _key())


def test_save_is_atomic_no_tmp_leftovers(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(_state(), path, _key())
    save_state(_state(), path, _key())  # overwrite path
    leftovers = [entry for entry in tmp_path.iterdir() if ".tmp-" in entry.name]
    assert not leftovers


def test_failed_replace_leaves_previous_state_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    save_state(_state(), path, _key())
    original = path.read_bytes()

    import modulo.launcher.state as state_module

    def _broken_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(state_module.os, "replace", _broken_replace)
    with pytest.raises(OSError, match="disk full"):
        save_state(LauncherState(postgres_port=15500, redis_port=16500, api_port=18500), path, _key())
    monkeypatch.undo()
    # The previous state survived untouched and no tmp debris remains.
    assert path.read_bytes() == original
    leftovers = [entry for entry in tmp_path.iterdir() if ".tmp-" in entry.name]
    assert not leftovers


def test_initial_state_uses_non_default_high_ports() -> None:
    state = initial_state()
    for port, default in (
        (state.postgres_port, DEFAULT_POSTGRES_PORT),
        (state.redis_port, DEFAULT_REDIS_PORT),
        (state.api_port, DEFAULT_API_PORT),
    ):
        assert 1024 <= port <= 65535
        assert port >= default  # allocation starts AT the non-default high port
    assert DEFAULT_POSTGRES_PORT != 5432
    assert DEFAULT_REDIS_PORT != 6379
    assert DEFAULT_API_PORT != 8000


def test_find_free_port_bumps_when_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.launcher.state as state_module

    class _FakeSocket:
        def __init__(self, _family: int, _type: int) -> None:
            self.bound: int | None = None

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def bind(self, addr: tuple[str, int]) -> None:
            port = addr[1]
            if port == 15432:
                raise OSError("address already in use")
            self.bound = port

    monkeypatch.setattr(state_module.socket, "socket", _FakeSocket)
    result = find_free_port(15432)
    assert result == 15433


def test_find_free_port_returns_preferred_when_free(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.launcher.state as state_module

    class _FakeSocket:
        def __init__(self, _family: int, _type: int) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def bind(self, _addr: tuple[str, int]) -> None:
            return None

    monkeypatch.setattr(state_module.socket, "socket", _FakeSocket)
    assert find_free_port(15432) == 15432


def test_state_round_trip_preserves_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(_state(), path, _key())
    envelope = json.loads(path.read_text())
    assert envelope["payload"]["schema_version"] == SCHEMA_VERSION
    assert load_state(path, _key()).schema_version == SCHEMA_VERSION
