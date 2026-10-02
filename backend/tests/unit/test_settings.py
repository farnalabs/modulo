"""Settings validation tests — SAQ runtime knobs (dist/runtime-cutover)."""

import logging
import socket

import pytest
from pydantic import ValidationError

from modulo.settings import MAX_NODE_TIMEOUT_SECONDS, Settings, resolve_instance_identity

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
# nodeless in-flight floor — DERIVED from the grace, and an explicit pin is
# REJECTED when it no longer covers grace + max node timeout (FAR-1088 F2)
# ---------------------------------------------------------------------------


def test_nodeless_in_flight_floor_derives_from_the_default_grace() -> None:
    """Unset -> derived: ``saq_setup_grace_seconds + MAX_NODE_TIMEOUT_SECONDS``
    (600 + 3300 = 3900), the value the pre-FAR-1088 constant hard-coded."""
    settings = _make()
    assert settings.saq_setup_grace_seconds == 600
    assert settings.nodeless_in_flight_floor_seconds == 600 + MAX_NODE_TIMEOUT_SECONDS
    assert settings.nodeless_in_flight_floor_seconds == 3900


def test_raising_the_setup_grace_widens_the_derived_floor() -> None:
    """Fails without F2: the floor stayed 3900 while the grace rose, silently
    re-opening the claim-a-live-attempt bug (the shield expired before the
    node deadline on a 900s-grace deployment)."""
    settings = _make(SAQ_SETUP_GRACE_SECONDS="900")

    assert settings.nodeless_in_flight_floor_seconds == 4200


def test_nodeless_in_flight_floor_keeps_an_adequate_explicit_pin() -> None:
    """An explicit floor that still covers grace + max node timeout is honoured
    (the operator widened it further, e.g. 4500 on a 900s-grace deployment)."""
    settings = _make(SAQ_SETUP_GRACE_SECONDS="900", NODELESS_IN_FLIGHT_FLOOR_SECONDS="4500")

    assert settings.nodeless_in_flight_floor_seconds == 4500


def test_nodeless_in_flight_floor_below_the_requirement_refuses_to_load() -> None:
    """FAIL (never warn): a pin that no longer covers grace + max node timeout
    would let the nodeless backstop claim an attempt whose node deadline has
    not provably passed — so Settings raises at load with the arithmetic."""
    with pytest.raises(ValidationError, match="NODELESS_IN_FLIGHT_FLOOR_SECONDS"):
        _make(SAQ_SETUP_GRACE_SECONDS="900", NODELESS_IN_FLIGHT_FLOOR_SECONDS="3900")


def test_nodeless_in_flight_floor_equal_to_the_requirement_is_accepted() -> None:
    """The shipped default IS exactly grace + max node timeout (3900 = 600 +
    3300), so equality must not be rejected — the guard is ``>=`` the
    requirement, matching the shield's own boundary semantics."""
    settings = _make(NODELESS_IN_FLIGHT_FLOOR_SECONDS="3900")

    assert settings.nodeless_in_flight_floor_seconds == 3900


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


# ---------------------------------------------------------------------------
# resolve_instance_identity — platform-neutral identity (ADR 043 / FAR-1158)
# ---------------------------------------------------------------------------


def test_instance_identity_uses_hostname_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOSTNAME (the generic process hostname) is the primary source."""
    monkeypatch.setenv("HOSTNAME", "pod-abc123")
    assert resolve_instance_identity() == "pod-abc123"


def test_instance_identity_falls_back_to_socket_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """No HOSTNAME env -> socket.gethostname(); never 'unknown' when a
    socket hostname exists (FAR-1158: identity must resolve on every
    platform with nothing declared)."""
    monkeypatch.delenv("HOSTNAME", raising=False)
    assert resolve_instance_identity() == socket.gethostname()


def test_instance_identity_never_reads_platform_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """FLY_MACHINE_ID / MODULO_RUNNER_MACHINE_ID are NOT identity sources:
    the former is a platform variable, the latter is a DEPLOYMENT-identity
    label (docs/configuration-reference.md) — neither may leak into
    instance identity (ADR 043 Decision 5)."""
    monkeypatch.setenv("FLY_MACHINE_ID", "fly-machine-xyz")
    monkeypatch.setenv("MODULO_RUNNER_MACHINE_ID", "runner-deployment-label")
    monkeypatch.delenv("HOSTNAME", raising=False)
    assert resolve_instance_identity() == socket.gethostname()
    assert resolve_instance_identity() != "fly-machine-xyz"
    assert resolve_instance_identity() != "runner-deployment-label"


def test_instance_identity_reads_env_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero module-scope os.environ reads (ADR 043 Decision 5): the resolver
    observes env changes made AFTER import, proving the read happens per
    call, not at module scope."""
    import modulo.settings as settings_mod

    monkeypatch.setenv("HOSTNAME", "first-host")
    assert settings_mod.resolve_instance_identity() == "first-host"
    monkeypatch.setenv("HOSTNAME", "second-host")
    assert settings_mod.resolve_instance_identity() == "second-host"


# ---------------------------------------------------------------------------
# FAR-1104 HITL review cancel-grace transition
# ---------------------------------------------------------------------------


def test_hitl_cancel_grace_falls_back_to_deprecated_env(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When only the deprecated ``HITL_GATE_CANCEL_GRACE_SECONDS`` is set, its
    value is used and a deprecation warning is emitted."""
    monkeypatch.delenv("HITL_REVIEW_CANCEL_GRACE_SECONDS", raising=False)
    monkeypatch.setenv("HITL_GATE_CANCEL_GRACE_SECONDS", "4242")

    with caplog.at_level(logging.WARNING):
        settings = _make()

    assert settings.hitl_review_cancel_grace_seconds == 4242
    assert "HITL_GATE_CANCEL_GRACE_SECONDS is deprecated" in caplog.text


def test_hitl_cancel_grace_new_env_wins_over_deprecated(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The new ``HITL_REVIEW_CANCEL_GRACE_SECONDS`` takes precedence and no
    deprecation warning is emitted when both are present."""
    monkeypatch.setenv("HITL_REVIEW_CANCEL_GRACE_SECONDS", "900")
    monkeypatch.setenv("HITL_GATE_CANCEL_GRACE_SECONDS", "4242")

    with caplog.at_level(logging.WARNING):
        settings = _make()

    assert settings.hitl_review_cancel_grace_seconds == 900
    assert "deprecated" not in caplog.text
