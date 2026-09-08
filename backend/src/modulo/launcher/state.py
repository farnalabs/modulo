"""Data-dir-scoped launcher state.json (ADR 031 Decision 6).

Contract, all locked by tests:

* Lives INSIDE the kept data dir; written ATOMICALLY (temp file + fsync +
  ``os.replace``; the directory entry is fsynced on POSIX).
* CREDENTIAL-FREE — only endpoint facts (ports) plus the schema version.
  Secrets live exclusively in the 0600 secrets file next to it.
* HMAC-INTEGRITY-PROTECTED: the envelope is
  ``{"payload": {...}, "mac": "<hex>"}`` with HMAC-SHA256 over the canonical
  JSON of the payload, keyed by ``state_hmac_key`` from the secrets file. A
  tampered file fails verification with :class:`StateIntegrityError`.
* ``schema_version`` stamped in the payload; loading a state written by a
  NEWER launcher (downgrade) or an unknown OLDER one is refused — there are
  no migration shims in v1.
* Ports are NON-DEFAULT high ports (bundled services must never collide with
  a developer's own Postgres/Redis), scanned for availability at first boot
  and persisted, with earlier allocations excluded from later scans so the
  three bundled services can never collide with each other.
* Written 0o600 on POSIX. TODO(P3): on Windows the 0o600 mode is a silent
  no-op (default ACLs apply) — acceptable here because the payload is
  credential-free, and the secrets file next to it refuses Windows loudly.
"""

import hashlib
import hmac
import json
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STATE_FILENAME = "state.json"
DEFAULT_POSTGRES_PORT = 15432
DEFAULT_REDIS_PORT = 16379
DEFAULT_API_PORT = 18000
_MIN_PORT = 1024
_MAX_PORT = 65535
_ALLOWED_PAYLOAD_KEYS = frozenset({"schema_version", "postgres_port", "redis_port", "api_port"})

_ENVELOPE_PAYLOAD_KEY = "payload"
_ENVELOPE_MAC_KEY = "mac"

# The launcher-owned public surface (consumed by the slice-2 entry; vulture's
# dead-code gate special-cases __all__).
__all__ = [
    "DEFAULT_API_PORT",
    "DEFAULT_POSTGRES_PORT",
    "DEFAULT_REDIS_PORT",
    "SCHEMA_VERSION",
    "STATE_FILENAME",
    "LauncherState",
    "StateIntegrityError",
    "StateVersionError",
    "find_free_port",
    "initial_state",
    "load_state",
    "save_state",
]


class StateIntegrityError(RuntimeError):
    """Raised when state.json fails HMAC verification or structurally."""


class StateVersionError(RuntimeError):
    """Raised when state.json's schema_version cannot be honoured."""


@dataclass(frozen=True)
class LauncherState:
    """Credential-free launcher endpoint state persisted in the data dir."""

    postgres_port: int
    redis_port: int
    api_port: int
    schema_version: int = SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "postgres_port": self.postgres_port,
            "redis_port": self.redis_port,
            "api_port": self.api_port,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LauncherState":
        unknown = set(payload) - _ALLOWED_PAYLOAD_KEYS
        if unknown:
            raise StateIntegrityError(f"state.json payload has unknown field(s): {sorted(unknown)}")
        missing = _ALLOWED_PAYLOAD_KEYS - set(payload)
        if missing:
            raise StateIntegrityError(f"state.json payload is missing field(s): {sorted(missing)}")
        for key in ("postgres_port", "redis_port", "api_port"):
            value = payload[key]
            if not isinstance(value, int) or isinstance(value, bool) or not _MIN_PORT <= value <= _MAX_PORT:
                raise StateIntegrityError(f"state.json field {key} is not a valid port: {value!r}")
        version = payload["schema_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise StateIntegrityError(f"state.json schema_version is not an integer: {version!r}")
        return cls(
            postgres_port=payload["postgres_port"],
            redis_port=payload["redis_port"],
            api_port=payload["api_port"],
            schema_version=version,
        )


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _mac_for(payload: dict[str, Any], hmac_key: bytes) -> str:
    return hmac.new(hmac_key, _canonical_json(payload), hashlib.sha256).hexdigest()


def find_free_port(preferred: int, exclude: set[int] | frozenset[int] | None = None) -> int:
    """Return *preferred* if bindable and not excluded, else the next free port above it.

    Scans upward from the non-default high port so two bundled services never
    collide with each other or with a developer's own listeners. *exclude*
    lets :func:`initial_state` keep sibling allocations distinct when a bump
    lands one service on another's preferred port.
    """
    excluded = exclude if exclude is not None else frozenset()
    candidate = max(preferred, _MIN_PORT)
    while candidate <= _MAX_PORT:
        if candidate in excluded:
            candidate += 1
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                candidate += 1
                continue
        return candidate
    raise StateIntegrityError(f"No free port found at or above {preferred}")


def initial_state() -> LauncherState:
    """Build the first-boot state: non-default high ports, availability-scanned.

    Each allocation excludes the previous ones, so a bump that lands Postgres
    on (say) 16379 can never make Redis pick the same port.
    """
    postgres_port = find_free_port(DEFAULT_POSTGRES_PORT)
    redis_port = find_free_port(DEFAULT_REDIS_PORT, exclude={postgres_port})
    api_port = find_free_port(DEFAULT_API_PORT, exclude={postgres_port, redis_port})
    return LauncherState(postgres_port=postgres_port, redis_port=redis_port, api_port=api_port)


def save_state(state: LauncherState, path: Path, hmac_key: bytes) -> None:
    """Write the HMAC envelope atomically (temp + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {_ENVELOPE_PAYLOAD_KEY: state.to_payload(), _ENVELOPE_MAC_KEY: _mac_for(state.to_payload(), hmac_key)}
    tmp_path = path.parent / f"{path.name}.tmp-{os.getpid()}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(envelope, sort_keys=True, indent=2).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        tmp_path.replace(path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def load_state(path: Path, hmac_key: bytes) -> LauncherState:
    """Load and verify state.json (HMAC + schema version + structure)."""
    try:
        envelope = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # ValueError covers both json.JSONDecodeError and UnicodeDecodeError
        # (a truncated/torn binary write must refuse cleanly, not crash).
        raise StateIntegrityError(f"state.json at {path} is unreadable: {exc}") from exc
    if not isinstance(envelope, dict) or _ENVELOPE_PAYLOAD_KEY not in envelope or _ENVELOPE_MAC_KEY not in envelope:
        raise StateIntegrityError(f"state.json at {path} is not a valid HMAC envelope")
    payload = envelope[_ENVELOPE_PAYLOAD_KEY]
    if not isinstance(payload, dict):
        raise StateIntegrityError(f"state.json at {path} payload is not an object")
    expected_mac = _mac_for(payload, hmac_key)
    provided_mac = envelope[_ENVELOPE_MAC_KEY]
    if not isinstance(provided_mac, str) or not hmac.compare_digest(expected_mac, provided_mac):
        raise StateIntegrityError(f"state.json at {path} failed HMAC verification — tampered or truncated")
    version = payload.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise StateVersionError(f"state.json schema_version missing or invalid: {version!r}")
    if version > SCHEMA_VERSION:
        raise StateVersionError(
            f"Downgrade refused: state.json schema_version {version} was written by a NEWER launcher "
            f"(this launcher speaks {SCHEMA_VERSION}). Restore the matching binary or reset the data dir."
        )
    if version < SCHEMA_VERSION:
        raise StateVersionError(
            f"state.json schema_version {version} predates this launcher's schema ({SCHEMA_VERSION}); "
            "no migration path exists — restore a matching binary or reset the data dir."
        )
    return LauncherState.from_payload(payload)
