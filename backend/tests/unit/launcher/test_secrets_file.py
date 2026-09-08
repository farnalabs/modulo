"""Unit tests for the launcher secrets file (FAR-671 / ADR 031 Decision 8).

Locks: first-boot generation (stdlib secrets - no constants), idempotent
reload (values never regenerated), the 0600 owner-only mode (POSIX), the
mode-tighten-on-reload behaviour, HMAC key shape, malformed-file refusal, the
exclusive-create race semantics (EEXIST → adopt the winner's file, never
orphan credentials), and the unlink-on-failure cleanup. Windows is refused by
the module itself; logic tests bypass the platform guard exactly like the
initdb tests (the win32 refusal itself is locked in test_secrets_file_platform.py).
"""

import json
import sys
from pathlib import Path

import pytest

import modulo.launcher.secrets_file as secrets_file_module
from modulo.launcher.secrets_file import (
    _HMAC_KEY_BYTES,
    LauncherSecrets,
    SecretsFileError,
    load_or_create,
)


@pytest.fixture(autouse=True)
def _bypass_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logic tests run on any OS: bypass the Windows-refusal seam."""
    if sys.platform == "win32":
        monkeypatch.setattr(secrets_file_module, "assert_supported_platform", lambda: None)


def test_first_boot_generates_all_credentials(tmp_path: Path) -> None:
    secrets = load_or_create(tmp_path / "secrets.json")
    assert secrets.postgres_password
    assert secrets.redis_password
    assert secrets.postgres_password != secrets.redis_password  # independent draws
    assert len(secrets.state_hmac_key) == _HMAC_KEY_BYTES


def test_no_hardcoded_constants_generated_values_differ_per_instance(tmp_path: Path) -> None:
    first = load_or_create(tmp_path / "a" / "secrets.json")
    second = load_or_create(tmp_path / "b" / "secrets.json")
    assert first.postgres_password != second.postgres_password
    assert first.redis_password != second.redis_password
    assert first.state_hmac_key != second.state_hmac_key


def test_reload_is_idempotent_never_regenerates(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    first = load_or_create(path)
    second = load_or_create(path)
    assert first == second


def test_file_is_owner_only_0600_posix(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    load_or_create(path)
    if sys.platform == "win32":
        pytest.skip("0o600 mode semantics are POSIX-only (TODO(P3) Windows ACLs)")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_write_is_atomic_no_tmp_leftovers(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    load_or_create(path)
    load_or_create(path)  # second call parses; no rewrite
    leftovers = [entry for entry in tmp_path.iterdir() if ".tmp-" in entry.name]
    assert not leftovers


def test_missing_field_refused(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"postgres_password": "x", "redis_password": "y"}))
    with pytest.raises(SecretsFileError, match="missing generated field"):
        load_or_create(path)


def test_invalid_hex_hmac_key_refused(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"postgres_password": "x", "redis_password": "y", "state_hmac_key": "not-hex!"}))
    with pytest.raises(SecretsFileError, match="valid hex"):
        load_or_create(path)


def test_wrong_hmac_key_length_refused(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"postgres_password": "x", "redis_password": "y", "state_hmac_key": "aabb"}))
    with pytest.raises(SecretsFileError, match="32 bytes"):
        load_or_create(path)


def test_invalid_json_refused(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text("{nope")
    with pytest.raises(SecretsFileError, match="valid JSON"):
        load_or_create(path)


def test_non_object_json_refused_with_restore_remedy(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(SecretsFileError, match="not a JSON object"):
        load_or_create(path)
    with pytest.raises(SecretsFileError, match="restart the launcher"):
        load_or_create(path)


def test_non_string_hmac_key_refused_with_restore_remedy(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"postgres_password": "x", "redis_password": "y", "state_hmac_key": 12345}))
    with pytest.raises(SecretsFileError, match="state_hmac_key is not a string"):
        load_or_create(path)


def test_existing_file_is_adopted_never_regenerated_under_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exclusive-create loser adopts the winner's file (no orphaned draw)."""
    path = tmp_path / "secrets.json"
    winner = load_or_create(path)
    # Simulate the race: exists() misses (file created concurrently) so the
    # loser reaches the exclusive create — which hits EEXIST and must adopt.
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(secrets_file_module, "_ADOPT_RETRY_DELAY_SECONDS", 0.0)
    loser = load_or_create(path)
    assert loser == winner
    # The file content was never overwritten by the loser's draw.
    assert json.loads(path.read_text())["postgres_password"] == winner.postgres_password


def test_adopt_retries_until_the_winner_finishes_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial read during adoption retries instead of failing the boot."""
    path = tmp_path / "secrets.json"
    winner = load_or_create(path)
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    monkeypatch.setattr(secrets_file_module, "_ADOPT_RETRY_DELAY_SECONDS", 0.0)
    real_parse = secrets_file_module._parse
    calls = {"n": 0}

    def _flaky_parse(data: bytes) -> LauncherSecrets:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SecretsFileError("simulated partial read")
        return real_parse(data)

    monkeypatch.setattr(secrets_file_module, "_parse", _flaky_parse)
    loser = load_or_create(path)
    assert loser == winner
    assert calls["n"] == 2


def test_write_failure_leaves_no_credential_file_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "secrets.json"

    def _broken_write(_fd: int, _data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(secrets_file_module.os, "write", _broken_write)
    with pytest.raises(OSError, match="disk full"):
        load_or_create(path)
    monkeypatch.undo()
    assert not path.exists()
    leftovers = list(tmp_path.iterdir())
    assert not leftovers


def test_created_file_is_parseable_and_matches_generated_values(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    secrets = load_or_create(path)
    parsed = json.loads(path.read_text())
    assert parsed["postgres_password"] == secrets.postgres_password
    assert parsed["redis_password"] == secrets.redis_password
    assert parsed["state_hmac_key"] == secrets.state_hmac_key_hex


@pytest.mark.skipif(sys.platform == "win32", reason="chmod tightening is POSIX-only (TODO(P3))")
def test_too_permissive_file_is_tightened_on_reload(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    load_or_create(path)
    path.chmod(0o644)
    assert (path.stat().st_mode & 0o777) == 0o644
    load_or_create(path)  # reload tightens + warns
    assert (path.stat().st_mode & 0o777) == 0o600


def test_hmac_key_hex_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    secrets = load_or_create(path)
    payload = json.loads(path.read_text())
    assert payload["state_hmac_key"] == secrets.state_hmac_key_hex
    assert bytes.fromhex(payload["state_hmac_key"]) == secrets.state_hmac_key


def test_launcher_secrets_dataclass_is_frozen(tmp_path: Path) -> None:
    secrets = load_or_create(tmp_path / "secrets.json")
    assert isinstance(secrets, LauncherSecrets)
    with pytest.raises(AttributeError):
        secrets.postgres_password = "mutated"  # type: ignore[misc]
