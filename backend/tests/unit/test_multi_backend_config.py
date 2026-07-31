"""Tests for Settings model custom validators — legacy URL rewriting and invalid backend detection."""

import pytest

from modulo.settings import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    monkeypatch.setenv("DATABASE_URL", overrides.pop("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo"))
    monkeypatch.setenv("SECRET_KEY", overrides.pop("SECRET_KEY", "a" * 32))
    monkeypatch.setenv("FERNET_KEY", overrides.pop("FERNET_KEY", "a" * 32))
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)


class TestMultiBackendCustomValidators:
    def test_legacy_asyncmy_url_uses_safe_driver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(
            monkeypatch,
            DATABASE_URL="mysql+asyncmy://modulo:modulo@localhost:3306/modulo",
            MODULO_DB="mariadb",
        )
        settings = Settings()
        assert settings.database_url == "mysql+aiomysql://modulo:modulo@localhost:3306/modulo"

    def test_invalid_backend_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, MODULO_DB="oracle")
        with pytest.raises(ValueError, match="MODULO_DB must be"):
            Settings()

    def test_sqlite_mode_auto_sets_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, MODULO_DB="sqlite")
        settings = Settings()
        assert settings.database_url == "sqlite+aiosqlite:///./modulo.db"

    def test_mariadb_mode_auto_sets_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, MODULO_DB="mariadb")
        settings = Settings()
        assert settings.database_url == "mysql+aiomysql://modulo:modulo@localhost:5435/modulo"

    def test_mysql_mode_auto_sets_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, MODULO_DB="mysql")
        settings = Settings()
        assert settings.database_url == "mysql+aiomysql://modulo:modulo@localhost:5435/modulo"

    def test_postgres_protocol_rewritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, DATABASE_URL="postgres://modulo:modulo@localhost:5432/modulo")
        settings = Settings()
        assert settings.database_url.startswith("postgresql+asyncpg://")

    def test_ssl_mode_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, DATABASE_URL="postgresql+asyncpg://modulo:modulo@localhost:5432/modulo?sslmode=disable")
        settings = Settings()
        assert "sslmode=disable" not in settings.database_url

    def test_modulo_db_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, MODULO_DB="SQLite")
        settings = Settings()
        assert settings.modulo_db == "sqlite"

    def test_modulo_db_mysql_uppercase(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, MODULO_DB="MYSQL")
        settings = Settings()
        assert settings.modulo_db == "mysql"

    def test_postgres_mode_preserves_given_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(
            monkeypatch,
            DATABASE_URL="postgresql+asyncpg://custom:pass@remote-host:5432/mydb",
            MODULO_DB="postgres",
        )
        settings = Settings()
        assert "custom" in settings.database_url
        assert "remote-host" in settings.database_url
        assert "mydb" in settings.database_url
