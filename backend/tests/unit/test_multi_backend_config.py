"""Smoke test: Settings model multi-backend config parsing.

Verifies that the Settings model correctly handles MODULO_DB env var
for all three supported backends (postgres, sqlite, mariadb).
"""

import pytest

from modulo.settings import Settings


class TestMultiBackendConfig:
    def test_postgres_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv("MODULO_DB", "postgres")
        settings = Settings()
        assert settings.modulo_db == "postgres"
        assert "postgresql+asyncpg" in settings.database_url

    def test_sqlite_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv("MODULO_DB", "sqlite")
        settings = Settings()
        assert settings.modulo_db == "sqlite"
        assert "sqlite+aiosqlite" in settings.database_url

    def test_mariadb_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv("MODULO_DB", "mariadb")
        settings = Settings()
        assert settings.modulo_db == "mariadb"
        assert "mysql+asyncmy" in settings.database_url

    def test_backend_string_normalization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)

        monkeypatch.setenv("MODULO_DB", "POSTGRES")
        assert Settings().modulo_db == "postgres"

        monkeypatch.setenv("MODULO_DB", "SQLite")
        assert Settings().modulo_db == "sqlite"

        monkeypatch.setenv("MODULO_DB", "MariaDB")
        assert Settings().modulo_db == "mariadb"

    def test_invalid_backend_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv("MODULO_DB", "oracle")
        with pytest.raises(ValueError, match="MODULO_DB must be"):
            Settings()
