"""Settings validation tests — SAQ runtime knobs (dist/runtime-cutover)."""

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
# saq_run_timeout
# ---------------------------------------------------------------------------


def test_saq_run_timeout_default() -> None:
    assert _make().saq_run_timeout == 7200


def test_saq_run_timeout_env_alias() -> None:
    assert _make(SAQ_RUN_TIMEOUT="3600").saq_run_timeout == 3600


def test_saq_run_timeout_rejects_below_min() -> None:
    with pytest.raises(ValidationError):
        _make(SAQ_RUN_TIMEOUT="299")


def test_saq_run_timeout_rejects_above_max() -> None:
    with pytest.raises(ValidationError):
        _make(SAQ_RUN_TIMEOUT="90000")


# ---------------------------------------------------------------------------
# saq_setup_grace_seconds vs run_claim_stale_seconds — WARN only, never raise
# ---------------------------------------------------------------------------


def test_saq_setup_grace_gte_claim_stale_warns(caplog: pytest.LogCaptureFixture) -> None:
    """grace >= stale warns (no raise) — the currently deployed config (600 >= 450)."""
    with caplog.at_level(logging.WARNING, logger="modulo.settings"):
        settings = _make(SAQ_SETUP_GRACE_SECONDS="600", RUN_CLAIM_STALE_SECONDS="450")
    assert settings.saq_setup_grace_seconds == 600
    assert settings.run_claim_stale_seconds == 450
    assert any("saq_setup_grace_ge_claim_stale" in r.message for r in caplog.records)


def test_saq_setup_grace_below_claim_stale_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """grace < stale is compliant — no warning is emitted."""
    with caplog.at_level(logging.WARNING, logger="modulo.settings"):
        settings = _make(SAQ_SETUP_GRACE_SECONDS="300", RUN_CLAIM_STALE_SECONDS="450")
    assert settings.saq_setup_grace_seconds == 300
    assert settings.run_claim_stale_seconds == 450
    assert not any("saq_setup_grace_ge_claim_stale" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# saq_nodeless_early_detect_minutes vs saq_claimed_nodeless_minutes — WARN only
# ---------------------------------------------------------------------------


def test_nodeless_early_detect_gte_full_window_warns(caplog: pytest.LogCaptureFixture) -> None:
    """early-detect >= full nodeless window silently disables the FAR-873
    branch — warn (no raise) so the misconfiguration is visible at load."""
    with caplog.at_level(logging.WARNING, logger="modulo.settings"):
        settings = _make(SAQ_NODELESS_EARLY_DETECT_MINUTES="100", SAQ_CLAIMED_NODELESS_MINUTES="35")
    assert settings.saq_nodeless_early_detect_minutes == 100
    assert settings.saq_claimed_nodeless_minutes == 35
    assert any("nodeless_early_detect_disabled" in r.message for r in caplog.records)


def test_nodeless_early_detect_below_full_window_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """early-detect < full nodeless window is compliant — no warning."""
    with caplog.at_level(logging.WARNING, logger="modulo.settings"):
        settings = _make(SAQ_NODELESS_EARLY_DETECT_MINUTES="15", SAQ_CLAIMED_NODELESS_MINUTES="35")
    assert settings.saq_nodeless_early_detect_minutes == 15
    assert settings.saq_claimed_nodeless_minutes == 35
    assert not any("nodeless_early_detect_disabled" in r.message for r in caplog.records)
