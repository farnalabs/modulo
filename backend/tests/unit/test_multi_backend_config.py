"""Tests for Settings model custom validators — legacy URL rewriting and invalid backend detection."""

import pytest

from modulo.settings import Settings


class TestMultiBackendCustomValidators:
    def test_legacy_asyncmy_url_uses_safe_driver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "mysql+asyncmy://modulo:modulo@localhost:3306/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv("MODULO_DB", "mariadb")

        settings = Settings()

        assert settings.database_url == "mysql+aiomysql://modulo:modulo@localhost:3306/modulo"

    def test_invalid_backend_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv("MODULO_DB", "oracle")
        with pytest.raises(ValueError, match="MODULO_DB must be"):
            Settings()
