"""Settings validation tests — SECRET_KEY and FERNET_KEY enforcement."""

import pytest
from pydantic import ValidationError

from modulo.settings import Settings

_VALID_32 = "a" * 32
_VALID_KEY = "x" * 32

_BASE_ENV: dict[str, str] = {
    "database_url": "postgresql+asyncpg://localhost/test",
    "secret_key": _VALID_32,
    "fernet_key": _VALID_KEY,
}


def _make(**overrides: str) -> Settings:
    env = {**_BASE_ENV, **overrides}
    return Settings(**env)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SECRET_KEY validation
# ---------------------------------------------------------------------------


def test_secret_key_blocked_value_raises() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        _make(secret_key="changeme")


# ---------------------------------------------------------------------------
# Auth warning (no login configured)
# ---------------------------------------------------------------------------


def test_no_auth_configured_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    """Missing admin password + users should warn, not raise."""
    import logging

    with caplog.at_level(logging.WARNING, logger="modulo.settings"):
        s = _make(modulo_admin_password="", modulo_users="")
    assert not s.modulo_admin_password
    assert not s.modulo_users
    assert any("no_auth_configured" in r.message or "login is disabled" in r.message for r in caplog.records)


def test_admin_password_set_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="modulo.settings"):
        _make(modulo_admin_password="hunter2_but_longer_than_needed")
    assert not any("login is disabled" in r.message for r in caplog.records)
