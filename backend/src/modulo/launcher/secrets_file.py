"""0600 secrets file inside the data dir (ADR 031 Decision 8).

ALL bundled-service credentials are generated at first boot with the stdlib
``secrets`` module - no credential constants exist in any artifact. The file
is written with owner-only (0o600) permissions (TODO(P3): on Windows the
0o600 mode is best-effort and must be backed by explicit ACLs - refuse-loud
hardening lands with the P3 delivery), atomically (temp file + rename), and
contains exactly the launcher-owned secrets:

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
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

SECRETS_FILENAME = "secrets.json"

_FIELDS = ("postgres_password", "redis_password", "state_hmac_key")
_HMAC_KEY_BYTES = 32
_PRIVATE_MODE = 0o600

# The launcher-owned public surface (consumed by the slice-2/3 entry and
# doctor; vulture's dead-code gate special-cases __all__).
__all__ = [
    "LauncherSecrets",
    "SecretsFileError",
    "load_or_create",
]


class SecretsFileError(RuntimeError):
    """Raised when the secrets file is missing fields or unreadable."""


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


def _write_private(path: Path, content: bytes) -> None:
    """Atomically write *content* with owner-only permissions (0o600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.tmp-{os.getpid()}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    try:
        os.write(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp_path.replace(path)
    if os.name == "posix":
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _parse(data: bytes) -> LauncherSecrets:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise SecretsFileError(f"secrets file is not valid JSON: {exc}") from exc
    missing = [field for field in _FIELDS if not payload.get(field)]
    if missing:
        raise SecretsFileError(f"secrets file is missing generated field(s): {missing}")
    try:
        hmac_key = bytes.fromhex(payload["state_hmac_key"])
    except (ValueError, AttributeError) as exc:
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


def load_or_create(path: Path) -> LauncherSecrets:
    """Load the secrets file, generating all credentials on first boot.

    Idempotent: an existing file is parsed and returned unchanged (never
    regenerated - that would orphan the bundled services' credentials).
    """
    if path.exists():
        if os.name == "posix" and (path.stat().st_mode & 0o777) != _PRIVATE_MODE:
            _log.warning("launcher.secrets_file_mode_tightened path=%s", path)
            path.chmod(_PRIVATE_MODE)
        return _parse(path.read_bytes())
    generated = _generate()
    payload = {
        "postgres_password": generated.postgres_password,
        "redis_password": generated.redis_password,
        "state_hmac_key": generated.state_hmac_key_hex,
    }
    _write_private(path, json.dumps(payload, indent=2, sort_keys=True).encode())
    # Belt-and-braces mode enforcement (umask variations must not widen it).
    if os.name == "posix":
        path.chmod(_PRIVATE_MODE)
        mode = path.stat().st_mode & 0o777
        if mode != _PRIVATE_MODE:
            raise SecretsFileError(f"secrets file mode could not be pinned to 0600 (got {oct(mode)})")
    return generated
