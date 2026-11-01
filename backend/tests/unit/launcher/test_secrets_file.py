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
import logging
import os
import sys
import types
from pathlib import Path

import pytest

import modulo.launcher.secrets_file as secrets_file_module
from modulo.launcher.secrets_file import (
    _HMAC_KEY_BYTES,
    LauncherSecrets,
    SecretsFileError,
    load_or_create,
)
from tests.unit.launcher.test_launcher_shared import bypass_platform_seams


@pytest.fixture(autouse=True)
def _bypass_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logic tests run on any OS: bypass the Windows-refusal seam."""
    bypass_platform_seams(monkeypatch, "modulo.launcher.secrets_file")


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
    real_exists = Path.exists

    def _target_absent(self: Path) -> bool:
        """False for the target only; every other path keeps the real stat."""
        return real_exists(self) if self != path else False

    monkeypatch.setattr(Path, "exists", _target_absent)
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
    real_exists = Path.exists

    def _target_absent(self: Path) -> bool:
        """False for the target only; every other path keeps the real stat."""
        return real_exists(self) if self != path else False

    monkeypatch.setattr(Path, "exists", _target_absent)
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


def test_adopt_exhaustion_refuses_the_boot_with_attempts_in_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file that stays unparsable for the whole adopt window fails the boot.

    Retry-forever is the wrong default: the exhaustion error must name the
    attempt count (the operator-visible retry budget) and carry the LAST
    parse error, and the loser's own draw must never be written over the
    winner's file.

    The number in the message is interpolated from ``_ADOPT_ATTEMPTS`` — the
    same constant patched below — so the string assertion alone could never
    disagree with the budget. The counting ``_parse`` seam pins the MAGNITUDE
    instead: the number of actually observed parse attempts must equal the
    configured budget exactly, so an off-by-one in the adopt loop fails here.
    """
    path = tmp_path / "secrets.json"
    # The file never becomes valid across the whole retry window.
    path.write_bytes(b'{"state_hmac_key": "truncated"')
    attempts = 20
    monkeypatch.setattr(secrets_file_module, "_ADOPT_ATTEMPTS", attempts)
    monkeypatch.setattr(secrets_file_module, "_ADOPT_RETRY_DELAY_SECONDS", 0.0)
    # exists() misses so the loser reaches the EEXIST branch (adopt path).
    real_exists = Path.exists

    def _target_absent(self: Path) -> bool:
        """False for the target only; every other path keeps the real stat."""
        return real_exists(self) if self != path else False

    monkeypatch.setattr(Path, "exists", _target_absent)
    real_parse = secrets_file_module._parse
    parse_calls = {"n": 0}

    def _counting_parse(data: bytes) -> LauncherSecrets:
        parse_calls["n"] += 1
        return real_parse(data)  # always refuses on the truncated payload

    monkeypatch.setattr(secrets_file_module, "_parse", _counting_parse)
    with pytest.raises(SecretsFileError) as excinfo:
        load_or_create(path)
    message = str(excinfo.value)
    # The observed retry count is exactly the configured budget — a mutation
    # of the loop's trip count by +-1 changes this and fails the test.
    assert parse_calls["n"] == attempts
    assert f"after {attempts} attempts" in message
    assert "not valid JSON" in message
    # No orphaned draw was written over the winner's (broken) file.
    assert path.read_bytes() == b'{"state_hmac_key": "truncated"'


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
    # (no monkeypatch.undo() - the fixture restores everything)
    assert not path.exists()
    leftovers = list(tmp_path.iterdir())
    assert not leftovers


def test_create_fails_closed_when_mode_cannot_be_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CRITICAL: a fresh file whose 0600 pin cannot be enforced is deleted.

    The exclusive create + chmod pair is a security pin (ADR 031 Decision 8):
    the file MUST end up owner-only. Force stat() to report 0o644 for the
    target file only — the real create is already 0o600 on POSIX, so the
    widened value can only be simulated — and assert load_or_create refuses
    AND unlinks rather than handing credentials back inside a
    world-readable file.

    Discriminating power: if the ``mode != _PRIVATE_MODE`` raise is removed,
    load_or_create returns the generated credentials and pytest.raises fails.
    """
    if os.name != "posix":
        pytest.skip("0o600 mode enforcement is POSIX-only (TODO(P3) Windows ACLs)")
    path = tmp_path / "secrets.json"
    real_stat = Path.stat

    def _widened_stat(self: Path, *args: object, **kwargs: object) -> object:
        # Only the freshly created target reports a widened mode; every other
        # path (and every pre-create exists() probe) uses the real stat. The
        # existence check goes through the REAL stat, never Path.exists(), so
        # the patched method cannot recurse into itself.
        if self == path:
            try:
                real_stat(self, *args, **kwargs)
            except OSError:
                return real_stat(self, *args, **kwargs)  # absent -> real FileNotFoundError
            return types.SimpleNamespace(st_mode=0o644)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _widened_stat)
    with pytest.raises(SecretsFileError, match="could not be pinned"):
        load_or_create(path)
    # (no monkeypatch.undo() - the fixture restores everything)
    assert not path.exists()  # the partially-created file was cleaned up


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


# ---------------------------------------------------------------------------
# POSIX-security-bits seam (FAR-2229): every branch of the mode pin, driven
# through the seam so the POSIX AND the non-POSIX arms both execute on any host
# (CI is Linux; a Windows developer host must still cover the POSIX arm).
# ---------------------------------------------------------------------------


def _mode_of_a_0600_create(directory: Path) -> int:
    """The mode a ``0o600`` exclusive create actually yields on this host.

    POSIX applies the umask (``0o600`` under ordinary umasks); Windows ignores
    the requested mode entirely. Deriving it means the 0600 pin can be
    exercised as an equality on both hosts instead of only on POSIX.
    """
    probe = directory / "mode-probe"
    fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return probe.stat().st_mode & 0o777


def test_load_existing_skips_the_mode_check_without_posix_security_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No security bits: the mode is never inspected or tightened."""
    path = tmp_path / "secrets.json"
    winner = load_or_create(path)
    chmod_calls: list[int] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode, *args, **kwargs: chmod_calls.append(mode))
    monkeypatch.setattr(secrets_file_module, "supports_posix_security_bits", lambda: False)

    assert load_or_create(path) == winner
    assert not chmod_calls


def test_load_existing_leaves_an_owner_only_file_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Security bits present and the mode already private: no chmod, no warning."""
    path = tmp_path / "secrets.json"
    winner = load_or_create(path)
    private_mode = path.stat().st_mode & 0o777
    chmod_calls: list[int] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode, *args, **kwargs: chmod_calls.append(mode))
    monkeypatch.setattr(secrets_file_module, "supports_posix_security_bits", lambda: True)
    monkeypatch.setattr(secrets_file_module, "_PRIVATE_MODE", private_mode)

    assert load_or_create(path) == winner
    assert not chmod_calls


def test_load_existing_tightens_and_warns_on_an_over_permissive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Security bits present and a wider mode: chmod back to the pinned mode."""
    path = tmp_path / "secrets.json"
    winner = load_or_create(path)
    actual_mode = path.stat().st_mode & 0o777
    tightened_mode = actual_mode ^ 0o001
    chmod_calls: list[int] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode, *args, **kwargs: chmod_calls.append(mode))
    monkeypatch.setattr(secrets_file_module, "supports_posix_security_bits", lambda: True)
    monkeypatch.setattr(secrets_file_module, "_PRIVATE_MODE", tightened_mode)

    with caplog.at_level(logging.WARNING, logger=secrets_file_module.__name__):
        assert load_or_create(path) == winner

    assert chmod_calls == [tightened_mode]
    assert "launcher.secrets_file_mode_tightened" in caplog.text


def test_create_marks_the_file_no_follow_and_pins_the_mode_when_security_bits_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX arm of both create-time guards: O_NOFOLLOW plus the 0600 re-pin."""
    path = tmp_path / "secrets.json"
    private_mode = _mode_of_a_0600_create(tmp_path)
    monkeypatch.setattr(secrets_file_module, "is_windows", lambda: False)
    monkeypatch.setattr(secrets_file_module, "supports_posix_security_bits", lambda: True)
    monkeypatch.setattr(secrets_file_module, "_PRIVATE_MODE", private_mode)
    chmod_calls: list[int] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode, *args, **kwargs: chmod_calls.append(mode))
    open_flags: list[int] = []
    real_open = os.open

    def _recording_open(target: str, flags: int, mode: int = 0o777) -> int:
        open_flags.append(flags)
        return real_open(target, flags, mode)

    monkeypatch.setattr(secrets_file_module.os, "open", _recording_open)

    generated = load_or_create(path)

    assert path.exists()
    assert json.loads(path.read_text())["postgres_password"] == generated.postgres_password
    # The POSIX arm adds the platform's O_NOFOLLOW (0 where the flag is absent).
    assert open_flags == [os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)]
    assert chmod_calls == [private_mode]


def test_create_skips_no_follow_and_the_mode_pin_without_security_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-POSIX arm: neither the O_NOFOLLOW bit nor the chmod pin is applied."""
    path = tmp_path / "secrets.json"
    monkeypatch.setattr(secrets_file_module, "is_windows", lambda: False)
    monkeypatch.setattr(secrets_file_module, "supports_posix_security_bits", lambda: False)
    chmod_calls: list[int] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode, *args, **kwargs: chmod_calls.append(mode))
    open_flags: list[int] = []
    real_open = os.open

    def _recording_open(target: str, flags: int, mode: int = 0o777) -> int:
        open_flags.append(flags)
        return real_open(target, flags, mode)

    monkeypatch.setattr(secrets_file_module.os, "open", _recording_open)

    generated = load_or_create(path)

    assert path.exists()
    assert json.loads(path.read_text())["postgres_password"] == generated.postgres_password
    assert open_flags == [os.O_WRONLY | os.O_CREAT | os.O_EXCL]
    assert not chmod_calls
