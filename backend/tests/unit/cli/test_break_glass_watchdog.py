"""Unit tests for the break-glass Settings validation matrix + boot watchdog.

Covers: ENABLED defaulting from secret presence; ENABLED=true with both secrets
empty -> boot finding (fatal in fail mode, warning in warn mode); SECRET ==
STANDBY -> construction error; minimum-length bound; TTL < 1 / MAX < MIN /
TTL > MAX -> construction error regardless of ENABLED; URL empty when ENABLED
-> finding (fail vs warn); and the warn|fail mode semantics.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modulo.settings import Settings, break_glass_boot_findings, validate_break_glass_boot

_SECRET = "p" * 32
_STANDBY = "s" * 32
_BG_URL = "postgresql+asyncpg://modulo_breakglass:bgpass@localhost:5432/modulo"


def _make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://modulo:modulo@localhost:5432/modulo",
        "secret_key": "a" * 32,
        "fernet_key": "a" * 32,
        "modulo_admin_password": "test",
        "redis_url": "",
    }
    base.update(overrides)
    return Settings(**base)


class TestEnabledDefaulting:
    def test_enabled_defaults_from_primary_presence(self) -> None:
        settings = _make_settings(modulo_break_glass_secret=_SECRET)
        assert settings.modulo_break_glass_enabled is True

    def test_enabled_defaults_from_standby_presence(self) -> None:
        settings = _make_settings(modulo_break_glass_standby_secret=_STANDBY)
        assert settings.modulo_break_glass_enabled is True

    def test_enabled_false_when_no_secrets(self) -> None:
        settings = _make_settings()
        assert settings.modulo_break_glass_enabled is False

    def test_enabled_explicit_override_respected(self) -> None:
        settings = _make_settings(
            modulo_break_glass_enabled=False,
            modulo_break_glass_secret=_SECRET,
        )
        assert settings.modulo_break_glass_enabled is False


class TestConstructionErrors:
    def test_secrets_must_differ(self) -> None:
        with pytest.raises(ValueError, match="must differ"):
            _make_settings(modulo_break_glass_secret=_SECRET, modulo_break_glass_standby_secret=_SECRET)

    def test_secrets_may_be_empty(self) -> None:
        settings = _make_settings(modulo_break_glass_secret="", modulo_break_glass_standby_secret="")
        assert settings.modulo_break_glass_secret == ""

    def test_minimum_length_primary(self) -> None:
        with pytest.raises(ValueError, match="at least 24 characters"):
            _make_settings(modulo_break_glass_secret="short")

    def test_minimum_length_standby(self) -> None:
        with pytest.raises(ValueError, match="at least 24 characters"):
            _make_settings(modulo_break_glass_standby_secret="short")

    def test_ttl_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_settings(modulo_break_glass_ttl_minutes=0)

    def test_max_ttl_cap_4320(self) -> None:
        with pytest.raises(ValidationError):
            _make_settings(modulo_break_glass_max_ttl_minutes=4321)

    def test_ttl_above_max_rejected(self) -> None:
        with pytest.raises(
            ValueError,
            match=r"MODULO_BREAK_GLASS_TTL_MINUTES must be <= MODULO_BREAK_GLASS_MAX_TTL_MINUTES",
        ):
            _make_settings(modulo_break_glass_ttl_minutes=2000, modulo_break_glass_max_ttl_minutes=500)

    def test_invalid_boot_failure_mode(self) -> None:
        with pytest.raises(ValueError, match=r"warn.*fail"):
            _make_settings(modulo_break_glass_boot_failure_mode="ignore")

    def test_ttl_bounds_checked_regardless_of_enabled(self) -> None:
        with pytest.raises(
            ValueError,
            match=r"MODULO_BREAK_GLASS_TTL_MINUTES must be <= MODULO_BREAK_GLASS_MAX_TTL_MINUTES",
        ):
            _make_settings(
                modulo_break_glass_enabled=False,
                modulo_break_glass_ttl_minutes=2000,
                modulo_break_glass_max_ttl_minutes=500,
            )


class TestBootFindings:
    def test_clean_when_configured(self) -> None:
        settings = _make_settings(
            modulo_break_glass_enabled=True,
            modulo_break_glass_secret=_SECRET,
            modulo_break_glass_standby_secret=_STANDBY,
            modulo_break_glass_database_url=_BG_URL,
        )
        assert break_glass_boot_findings(settings) == []

    def test_enabled_with_both_secrets_empty_is_finding(self) -> None:
        settings = _make_settings(
            modulo_break_glass_enabled=True,
            modulo_break_glass_secret="",
            modulo_break_glass_standby_secret="",
        )
        findings = break_glass_boot_findings(settings)
        assert any("both MODULO_BREAK_GLASS_SECRET" in message for _blocking, message in findings)
        assert any(blocking for blocking, _message in findings)

    def test_enabled_with_empty_url_is_finding(self) -> None:
        settings = _make_settings(
            modulo_break_glass_enabled=True,
            modulo_break_glass_secret=_SECRET,
            modulo_break_glass_standby_secret=_STANDBY,
            modulo_break_glass_database_url="",
        )
        findings = break_glass_boot_findings(settings)
        assert any("MODULO_BREAK_GLASS_DATABASE_URL is empty" in message for _blocking, message in findings)
        assert any(blocking for blocking, _message in findings)

    def test_disabled_with_empty_url_is_non_blocking_warn(self) -> None:
        settings = _make_settings(
            modulo_break_glass_enabled=False,
            modulo_break_glass_database_url="",
        )
        findings = break_glass_boot_findings(settings)
        assert any("DATABASE_URL is empty" in message for _blocking, message in findings)
        assert all(not blocking for blocking, _message in findings)

    def test_one_secret_missing_warns(self) -> None:
        settings = _make_settings(
            modulo_break_glass_enabled=True,
            modulo_break_glass_secret=_SECRET,
            modulo_break_glass_standby_secret="",
        )
        findings = break_glass_boot_findings(settings)
        assert any("rotation path is degraded" in message for _blocking, message in findings)


class TestValidateBreakGlassBoot:
    def test_fail_mode_raises(self) -> None:
        settings = _make_settings(
            modulo_break_glass_enabled=True,
            modulo_break_glass_secret="",
            modulo_break_glass_standby_secret="",
            modulo_break_glass_database_url="",
            modulo_break_glass_boot_failure_mode="fail",
        )
        with pytest.raises(RuntimeError, match="Break-glass boot config assertion FAILED"):
            validate_break_glass_boot(settings)

    def test_warn_mode_logs_and_continues(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(
            modulo_break_glass_enabled=True,
            modulo_break_glass_secret="",
            modulo_break_glass_standby_secret="",
            modulo_break_glass_database_url="",
            modulo_break_glass_boot_failure_mode="warn",
        )
        validate_break_glass_boot(settings)
        assert any("break_glass.boot_config" in rec.message for rec in caplog.records)

    def test_clean_passes_silently(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(
            modulo_break_glass_enabled=True,
            modulo_break_glass_secret=_SECRET,
            modulo_break_glass_standby_secret=_STANDBY,
            modulo_break_glass_database_url=_BG_URL,
            modulo_break_glass_boot_failure_mode="fail",
        )
        validate_break_glass_boot(settings)
        assert not caplog.records

    def test_disabled_unconfigured_is_warn_not_fail(self) -> None:
        settings = _make_settings(modulo_break_glass_boot_failure_mode="fail")
        # Fully unconfigured (disabled) never fails boot even in fail mode — the
        # URL/secret-presence findings are non-blocking warns when disabled.
        validate_break_glass_boot(settings)
