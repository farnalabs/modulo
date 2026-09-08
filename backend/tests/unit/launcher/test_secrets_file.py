"""Unit tests for the launcher secrets file (FAR-671 / ADR 031 Decision 8).

Locks: first-boot generation (stdlib secrets - no constants), idempotent
reload (values never regenerated), the 0600 owner-only mode (POSIX), the
mode-tighten-on-reload behaviour, HMAC key shape, and malformed-file refusal.
"""

import json
import sys
from pathlib import Path

import pytest

from modulo.launcher.secrets_file import (
    _HMAC_KEY_BYTES,
    LauncherSecrets,
    SecretsFileError,
    load_or_create,
)


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
