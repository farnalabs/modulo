"""Unit tests for env.py's migration-target resolution (FAR-2183 F5).

Before the fix, ``alembic.ini`` hardcoded
``postgresql+asyncpg://modulo:modulo@localhost:5434/modulo`` and env.py read
ONLY ``os.environ``. ``alembic upgrade heads`` therefore ignored
``backend/.env`` and silently migrated the hardcoded database - correct only by
coincidence on a default local host, and a silent wrong-database migration for
any custom or remote ``DATABASE_URL``.

These tests pin the replacement contract:

* the env file the application reads is honoured;
* no target at all fails loudly (the hardcoded default is gone);
* an explicitly configured target is never diverted by an ambient
  ``DATABASE_URL`` (an integration test pinned to its own database must not be
  migrated onto a developer's ``.env`` database), and the divergence is logged;
* ``DATABASE_ADMIN_URL`` remains the documented owner-role override;
* logged/refusal text never leaks a password.

They call :func:`modulo.db.migrations.env.resolve_migration_url` directly with
an injected environment mapping, so neither a database nor a mutation of the
process environment is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config

from modulo.db.migrations import env as migration_env

# tests/unit/db/ -> tests/unit/ -> tests/ -> backend/.
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"

_DATABASE_URL = "postgresql+asyncpg://modulo_app:app-secret@db.internal:5434/modulo"
_ADMIN_URL = "postgresql+asyncpg://modulo:owner-secret@db.internal:5434/modulo"
_ENV_FILE_URL = "postgresql+asyncpg://modulo_app:env-secret@env-file-host:5555/env_file_db"


def _config() -> Config:
    return Config(str(_ALEMBIC_INI))


def test_alembic_ini_carries_no_default_url() -> None:
    """The hardcoded target is gone; a URL here would resurrect the trap."""
    configured = _config().get_main_option(migration_env._CONFIG_KEY_SQLALCHEMY_URL)
    assert not configured


def test_env_file_url_is_honoured(tmp_path: Path) -> None:
    """``backend/.env`` is read exactly as the application reads it."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={_ENV_FILE_URL}\n", encoding="utf-8")
    config = _config()

    resolved = migration_env.resolve_migration_url(config, env_file_paths=[env_file], environ={})

    # Sync driver translation is expected; the host/database must be the .env one.
    assert "env-file-host:5555" in resolved
    assert "env_file_db" in resolved
    assert resolved == config.get_main_option(migration_env._CONFIG_KEY_SQLALCHEMY_URL)


def test_env_file_does_not_override_exported_database_url(tmp_path: Path) -> None:
    """The process environment stays authoritative over the env file."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={_ENV_FILE_URL}\n", encoding="utf-8")
    environ = {"DATABASE_URL": _DATABASE_URL}

    resolved = migration_env.resolve_migration_url(_config(), env_file_paths=[env_file], environ=environ)

    assert "db.internal:5434" in resolved
    assert "env-file-host" not in resolved


def test_no_configured_target_refuses() -> None:
    """No env var, no env file, no caller URL -> loud refusal, never a default."""
    with pytest.raises(RuntimeError) as err:
        migration_env.resolve_migration_url(_config(), env_file_paths=[], environ={})

    message = str(err.value)
    assert "Refusing to run migrations" in message
    assert "DATABASE_URL" in message


def test_caller_configured_url_wins_over_ambient_database_url() -> None:
    """An explicitly configured target is never diverted by an ambient URL.

    This is the integration-test shape: the harness pins its own database on
    the Config while ``DATABASE_URL`` still points at a shared one. Migrating
    the shared database instead would be a silent wrong-database run.
    """
    config = _config()
    config.set_main_option(
        migration_env._CONFIG_KEY_SQLALCHEMY_URL,
        "postgresql+psycopg://someone:elsewhere@other-host:5432/other_db",
    )

    resolved = migration_env.resolve_migration_url(
        config,
        env_file_paths=[],
        environ={"DATABASE_URL": _DATABASE_URL},
    )

    assert "other-host:5432" in resolved
    assert "db.internal:5434" not in resolved


def test_caller_override_is_logged_with_redacted_targets(caplog: pytest.LogCaptureFixture) -> None:
    """A divergence is never silent, and the log carries no credentials."""
    caplog.set_level("INFO", logger="modulo.db.migrations.env")
    config = _config()
    config.set_main_option(
        migration_env._CONFIG_KEY_SQLALCHEMY_URL,
        "postgresql+psycopg://someone:other-secret@other-host:5432/other_db",
    )

    migration_env.resolve_migration_url(
        config,
        env_file_paths=[],
        environ={"DATABASE_URL": _DATABASE_URL},
    )

    assert "alembic.migration_url_caller_configured" in caplog.text
    assert "other-host:5432" in caplog.text
    assert "db.internal:5434" in caplog.text
    assert "app-secret" not in caplog.text
    assert "other-secret" not in caplog.text


def test_matching_configured_url_is_accepted() -> None:
    """The same target expressed async (env) and sync (configured) is not divergence."""
    config = _config()
    config.set_main_option(
        migration_env._CONFIG_KEY_SQLALCHEMY_URL,
        migration_env._to_sync_url(_DATABASE_URL),
    )

    resolved = migration_env.resolve_migration_url(
        config,
        env_file_paths=[],
        environ={"DATABASE_URL": _DATABASE_URL},
    )

    assert resolved == migration_env._to_sync_url(_DATABASE_URL)


def test_caller_configured_url_is_honoured_without_ambient_env() -> None:
    """Test harnesses / ops tooling that set the URL on the Config keep working."""
    config = _config()
    config.set_main_option(
        migration_env._CONFIG_KEY_SQLALCHEMY_URL,
        "postgresql+psycopg://harness:harness-secret@testcontainer:5432/test_db",
    )

    resolved = migration_env.resolve_migration_url(config, env_file_paths=[], environ={})

    assert "testcontainer:5432" in resolved


def test_admin_url_is_the_owner_override() -> None:
    """DATABASE_ADMIN_URL deliberately wins (migrations run as the owner role)."""
    environ = {"DATABASE_URL": _DATABASE_URL, "DATABASE_ADMIN_URL": _ADMIN_URL}

    resolved = migration_env.resolve_migration_url(_config(), env_file_paths=[], environ=environ)

    assert resolved == migration_env._to_sync_url(_ADMIN_URL)
