"""Launcher Settings source: env > state.json(+secrets) > defaults (FAR-671).

Two delivery mechanisms share ONE composition core:

* :func:`write_pinned_env_file` writes the composed config as a 0600 dotenv
  file inside the data dir; the entry pins it via ``modulo.settings
  .pin_env_file`` so every ``get_settings()`` call (the API, the SAQ
  children's settings, the CLI) sees the bundled endpoints through the
  standard dotenv source — real environment variables outrank it and field
  defaults sit below it, which is exactly the env > state.json > defaults
  priority.
* :class:`LauncherConfigSource` is the pydantic-settings source-protocol
  implementation for DIRECT ``Settings(**source())`` construction: it
  returns only the composed keys whose environment variable is NOT already
  set, so the init-kwargs layer (which outranks the env source) can never
  shadow an explicit operator environment variable.

IMPORT HYGIENE (locked by tests): this module must NOT import
``modulo.settings`` at module import time — the source is installed ONLY by
the native launcher, after the environment scrub and the config pin.

The composed values contain credentials (the generated passwords embedded
in the URLs); the file lands in the data dir at 0600 via the promoted
exclusive-create writer (``modulo.db.bootstrap._write_env_file``), which
also refuses a symlink squatting on the contract path.
"""

import os
from pathlib import Path
from typing import Any

from modulo.db.url_utils import derive_system_database_url
from modulo.launcher.secrets_file import LauncherSecrets
from modulo.launcher.state import STATE_FILENAME, LauncherState, load_state

CONFIG_ENV_FILENAME = "config.env"
POSTGRES_HOST = "127.0.0.1"
APP_DB_NAME = "modulo"
_REDIS_DB = "0"

# Composed env-name -> Settings field name (only real Settings fields).
_COMPOSED_FIELDS: dict[str, str] = {
    "DATABASE_URL": "database_url",
    "REDIS_URL": "redis_url",
    "MODULO_SYSTEM_DATABASE_URL": "modulo_system_database_url",
}

# The launcher-owned public surface (consumed by the entry; vulture's
# dead-code gate special-cases __all__).
__all__ = [
    "CONFIG_ENV_FILENAME",
    "LauncherConfigError",
    "LauncherConfigSource",
    "compose_config",
    "load_launcher_config_inputs",
    "write_pinned_env_file",
]


class LauncherConfigError(RuntimeError):
    """Raised when the launcher config source cannot read its inputs."""


def compose_config(state: LauncherState, secrets: LauncherSecrets) -> dict[str, str]:
    """Derive the bundled-service config values (credentials INCLUDED).

    Pure mapping from the credential-free state.json (ports) + the 0600
    secrets file (generated passwords) to the Settings env names. The
    ``postgresql+asyncpg://`` scheme is pinned explicitly (the async driver
    the engine requires); every asyncpg-level consumer downgrades it the way
    the container path does.
    """
    database_url = (
        f"postgresql+asyncpg://modulo:{secrets.postgres_password}@{POSTGRES_HOST}:{state.postgres_port}/{APP_DB_NAME}"
    )
    system_url = derive_system_database_url(database_url)
    return {
        "DATABASE_URL": database_url,
        "DATABASE_ADMIN_URL": database_url,
        "REDIS_URL": f"redis://:{secrets.redis_password}@{POSTGRES_HOST}:{state.redis_port}/{_REDIS_DB}",
        "MODULO_SYSTEM_DATABASE_URL": system_url or "",
    }


def load_launcher_config_inputs(data_dir: Path) -> tuple[LauncherState, LauncherSecrets]:
    """Load (state.json, secrets.json) for the composed config — fail fast."""
    from modulo.launcher.secrets_file import load_or_create

    secrets = load_or_create(data_dir / "secrets.json")
    state_path = data_dir / STATE_FILENAME
    if not state_path.exists():
        raise LauncherConfigError(f"no state.json at {state_path} — the data dir is not bootstrapped")
    return load_state(state_path, secrets.state_hmac_key), secrets


def render_env_file(composed: dict[str, str]) -> str:
    """Render the composed values as dotenv lines (sorted, stable)."""
    return "".join(f"{key}={composed[key]}\n" for key in sorted(composed))


def write_pinned_env_file(data_dir: Path, state: LauncherState, secrets: LauncherSecrets) -> Path:
    """Write ``<datadir>/config.env`` (0600) and return its path.

    The writer is the promoted exclusive-create implementation (random temp
    sibling + fsync + atomic rename + symlink rejection), so a concurrent
    boot or a symlink squatting on the contract path can never leak or
    hijack the credential-bearing config.
    """
    from modulo.db.bootstrap import _write_env_file

    composed = compose_config(state, secrets)
    path = data_dir / CONFIG_ENV_FILENAME
    _write_env_file(str(path), render_env_file(composed))
    return path


class LauncherConfigSource:
    """pydantic-settings source: env > state.json(+secrets) > defaults.

    ``__call__`` returns ONLY the composed keys whose environment variable
    is not already set (the env wins; the init-kwargs layer this dict feeds
    outranks the env source, so the filtering is what preserves the
    priority). Keys absent from both sources fall through to field
    defaults.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def __call__(self) -> dict[str, Any]:
        state, secrets = load_launcher_config_inputs(self.data_dir)
        composed = compose_config(state, secrets)
        resolved: dict[str, Any] = {}
        for env_name, field_name in _COMPOSED_FIELDS.items():
            value = composed.get(env_name, "")
            if not value:
                continue
            if os.environ.get(env_name):
                continue  # the operator's environment variable wins
            resolved[field_name] = value
        return resolved
