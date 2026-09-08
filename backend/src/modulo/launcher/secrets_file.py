"""0600 secrets file inside the data dir (ADR 031 Decision 8).

ALL bundled-service credentials are generated at first boot with the stdlib
``secrets`` module - no credential constants exist in any artifact. The file
is written with owner-only (0o600) permissions, created EXCLUSIVELY
(``O_CREAT|O_EXCL``, plus ``O_NOFOLLOW`` on POSIX) so two concurrent first
boots can never both "win": the loser adopts the winner's file instead of
orphaning its credentials (a last-writer-wins rename would brick the bundled
Postgres password and the state.json HMAC key). Write/fsync failures unlink
what was created. Windows is refused loudly (mirrors
``initdb.assert_supported_platform``) instead of silently writing secrets
with default ACLs; the file contains exactly the launcher-owned secrets:

* ``postgres_password`` - the bundled Postgres superuser/bootstrap password
  (fed to initdb's --pwfile; scram-sha-256).
* ``redis_password`` - the bundled Redis ``requirepass``.
* ``state_hmac_key`` - hex-encoded 32-byte key used by ``state.py`` to
  HMAC-protect state.json.

On reload a file with a too-permissive mode is tightened back to 0o600 with
a warning (the launcher owns the data dir; nothing else should read it).
"""

import json
import logging
import os
import secrets as _secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

SECRETS_FILENAME = "secrets.json"

_FIELDS = ("postgres_password", "redis_password", "state_hmac_key")
_HMAC_KEY_BYTES = 32
_PRIVATE_MODE = 0o600
# Adopt-on-EEXIST retries: a concurrent creator is between open() and
# write() for only microseconds, but a bounded retry closes the window
# instead of parsing a half-written file.
_ADOPT_ATTEMPTS = 20
_ADOPT_RETRY_DELAY_SECONDS = 0.05
_RESTORE_REMEDY = "delete the file and restart the launcher to regenerate credentials"

# The launcher-owned public surface (consumed by the slice-2/3 entry and
# doctor; vulture's dead-code gate special-cases __all__).
__all__ = [
    "LauncherSecrets",
    "SecretsFileError",
    "assert_supported_platform",
    "load_or_create",
]


class SecretsFileError(RuntimeError):
    """Raised when the secrets file is missing fields or unreadable."""


def assert_supported_platform() -> None:
    """Refuse Windows until the P3 delivery (0600/ACL enforcement).

    A silent 0o600 no-op would write launcher secrets with default ACLs —
    refuse loudly instead (same seam shape as ``initdb.assert_supported_platform``).
    """
    if sys.platform == "win32":
        raise SecretsFileError("The launcher secrets file is not supported on Windows yet (TODO(P3) ACL work)")


@dataclass(frozen=True)
class LauncherSecrets:
    """The launcher-owned credentials (never persisted anywhere but the 0600 file)."""

    postgres_password: str
    redis_password: str
    state_hmac_key: bytes

    @property
    def state_hmac_key_hex(self) -> str:
        return self.state_hmac_key.hex()


def _generate() -> LauncherSecrets:
    return LauncherSecrets(
        postgres_password=_secrets.token_urlsafe(32),
        redis_password=_secrets.token_urlsafe(32),
        state_hmac_key=_secrets.token_bytes(_HMAC_KEY_BYTES),
    )


def _parse(data: bytes) -> LauncherSecrets:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise SecretsFileError(f"secrets file is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SecretsFileError(f"secrets file is not a JSON object — {_RESTORE_REMEDY}")
    missing = [field for field in _FIELDS if not payload.get(field)]
    if missing:
        raise SecretsFileError(f"secrets file is missing generated field(s): {missing}")
    hmac_key_raw = payload["state_hmac_key"]
    if not isinstance(hmac_key_raw, str):
        raise SecretsFileError(f"secrets file state_hmac_key is not a string — {_RESTORE_REMEDY}")
    try:
        hmac_key = bytes.fromhex(hmac_key_raw)
    except ValueError as exc:
        raise SecretsFileError("secrets file state_hmac_key is not valid hex") from exc
    if len(hmac_key) != _HMAC_KEY_BYTES:
        raise SecretsFileError(
            f"secrets file state_hmac_key must decode to {_HMAC_KEY_BYTES} bytes; got {len(hmac_key)}"
        )
    return LauncherSecrets(
        postgres_password=payload["postgres_password"],
        redis_password=payload["redis_password"],
        state_hmac_key=hmac_key,
    )


def _load_existing(path: Path) -> LauncherSecrets:
    """Parse an existing secrets file, tightening an over-permissive mode first."""
    if os.name == "posix" and (path.stat().st_mode & 0o777) != _PRIVATE_MODE:
        _log.warning("launcher.secrets_file_mode_tightened path=%s", path)
        path.chmod(_PRIVATE_MODE)
    return _parse(path.read_bytes())


def _adopt_existing(path: Path) -> LauncherSecrets:
    """Read+validate an existing secrets file (the EEXIST race loser's path).

    A concurrent first boot may be between create and write; retry briefly on
    a parse failure before refusing, so the loser always ends up with the
    winner's credentials (never its own orphaned draw).
    """
    last_error: SecretsFileError | None = None
    for _ in range(_ADOPT_ATTEMPTS):
        try:
            return _load_existing(path)
        except SecretsFileError as exc:
            last_error = exc
            time.sleep(_ADOPT_RETRY_DELAY_SECONDS)
    assert last_error is not None
    raise last_error


def load_or_create(path: Path) -> LauncherSecrets:
    """Load the secrets file, generating all credentials on first boot.

    Idempotent: an existing file is parsed and returned unchanged (never
    regenerated - that would orphan the bundled services' credentials). The
    first boot creates the file exclusively (``O_CREAT|O_EXCL``): when the
    create loses a race against a concurrent boot, the existing file is
    adopted (read+validated) so both processes share one set of credentials.
    """
    assert_supported_platform()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return _load_existing(path)
    generated = _generate()
    payload = {
        "postgres_password": generated.postgres_password,
        "redis_password": generated.redis_password,
        "state_hmac_key": generated.state_hmac_key_hex,
    }
    content = json.dumps(payload, indent=2, sort_keys=True).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags, _PRIVATE_MODE)
    except FileExistsError:
        return _adopt_existing(path)
    try:
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        # Belt-and-braces mode enforcement (umask variations must not widen it).
        if os.name == "posix":
            path.chmod(_PRIVATE_MODE)
            mode = path.stat().st_mode & 0o777
            if mode != _PRIVATE_MODE:
                raise SecretsFileError(f"secrets file mode could not be pinned to 0600 (got {oct(mode)})")
    except (OSError, SecretsFileError):
        # Never leave a partially-written or unenforceable credential file behind.
        path.unlink(missing_ok=True)
        raise
    return generated
