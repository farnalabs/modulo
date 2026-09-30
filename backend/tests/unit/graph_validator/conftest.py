"""Shared fixtures for the GraphValidator unit suite.

The validator resolves the run-level transport ceiling (``MODULO_MAX_RUN_SECONDS``)
while checking a bound environment profile's wall-clock capability (FAR-1359), so
every graph-validation test needs a constructible Settings instance. Pin the
required env here rather than in each file, and clear the ``get_settings`` cache
either side of every test so no test inherits another's pinned environment.
"""

import pytest

from modulo.settings import get_settings


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch):
    """Give each GraphValidator test a clean, constructible Settings instance."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
