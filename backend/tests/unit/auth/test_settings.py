"""Settings validation tests — SECRET_KEY and FERNET_KEY enforcement."""

import logging

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
    with caplog.at_level(logging.WARNING, logger="modulo.settings"):
        s = _make(modulo_admin_password="", modulo_users="")
    assert not s.modulo_admin_password
    assert not s.modulo_users
    assert any("no_auth_configured" in r.message or "login is disabled" in r.message for r in caplog.records)


def test_admin_password_set_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="modulo.settings"):
        _make(modulo_admin_password="hunter2_but_longer_than_needed")
    assert not any("login is disabled" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Sandbox agent runtime cost rate
# ---------------------------------------------------------------------------


def test_e2b_sandbox_usd_per_hour_default() -> None:
    """The E2B hourly rate defaults to 0.13 USD/hr when unset."""
    assert _make().e2b_sandbox_usd_per_hour == pytest.approx(0.13)


def test_e2b_sandbox_usd_per_hour_env_override() -> None:
    """The E2B hourly rate is configurable via E2B_SANDBOX_USD_PER_HOUR."""
    assert _make(E2B_SANDBOX_USD_PER_HOUR="0.25").e2b_sandbox_usd_per_hour == 0.25


def test_e2b_sandbox_usd_per_hour_rejects_negative() -> None:
    """A negative hourly rate is invalid (ge=0)."""
    with pytest.raises(ValidationError):
        _make(E2B_SANDBOX_USD_PER_HOUR="-1")
