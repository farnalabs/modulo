"""Unit tests for the cost Settings knobs — ge-bounds, ordering, boot guards (§2.4/§2.5)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from modulo.settings import Settings

_REQUIRED = {
    "DATABASE_URL": "sqlite+aiosqlite:///./settings-test.db",
    "SECRET_KEY": "s" * 40,
    "FERNET_KEY": "f" * 44,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _REQUIRED.items():
        monkeypatch.setenv(key, value)


def test_defaults_pass() -> None:
    settings = Settings()
    assert str(settings.max_reportable_usd_min) == "0.000001"
    assert str(settings.max_self_reported_usd) == "10000.0"
    assert str(settings.max_reportable_band_usd) == "50.0"
    assert str(settings.max_rate_usd) == "100000.0"


def test_floor_below_min_fails_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_MAX_REPORTABLE_USD_MIN", "0.0000005")
    with pytest.raises(ValidationError):
        Settings()


def test_self_reported_below_min_fails_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_MAX_SELF_REPORTED_USD", "0.0000005")
    with pytest.raises(ValidationError):
        Settings()


def test_rate_negative_fails_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_MAX_RATE_USD", "-1")
    with pytest.raises(ValidationError):
        Settings()


def test_ordering_floor_gte_clamp_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_MAX_REPORTABLE_USD_MIN", "20000")
    monkeypatch.setenv("MODULO_MAX_SELF_REPORTED_USD", "10000")
    with pytest.raises(ValidationError):
        Settings()


def test_floor_at_band_ceiling_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_MAX_REPORTABLE_USD_MIN", "50.0")
    with pytest.raises(ValidationError):
        Settings()


def test_floor_above_band_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_MAX_REPORTABLE_USD_MIN", "60.0")
    with pytest.raises(ValidationError):
        Settings()


def test_floor_below_band_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_MAX_REPORTABLE_USD_MIN", "49.99")
    settings = Settings()
    assert str(settings.max_reportable_usd_min) == "49.99"


def test_knob_below_band_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # band above the per-node clamp -> the out_of_band_high marker can never fire.
    monkeypatch.setenv("MODULO_MAX_REPORTABLE_BAND_USD", "20000")
    monkeypatch.setenv("MODULO_MAX_SELF_REPORTED_USD", "10000")
    with pytest.raises(ValidationError):
        Settings()


def test_effective_self_reported_min_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_MAX_SELF_REPORTED_USD", "1e9")
    settings = Settings()
    assert settings.effective_max_self_reported_usd == Decimal("99999999.999999")


def test_effective_rate_min_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_MAX_RATE_USD", "1e15")
    settings = Settings()
    assert settings.effective_max_rate_usd == Decimal("999999999999.999999")


def test_zero_env_value_fails_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_MAX_REPORTABLE_USD_MIN", "0")
    with pytest.raises(ValidationError):
        Settings()
