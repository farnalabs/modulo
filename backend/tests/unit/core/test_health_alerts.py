"""Unit tests for the readiness-degradation alert check (FAR-1446).

No real SMTP, no network, no real readiness probe: transitions are driven with
a fake health observer and a stub sender, and the dedup state lives in an
in-memory Redis double so persistence-across-invocations is observable.

Covers the four contract requirements:
* healthy -> unhealthy emails exactly once (and staying unhealthy emails none);
* unhealthy -> healthy emails a recovery (and staying healthy emails none);
* the quiet path: no SMTP/recipient config -> no send, no raise, low-rate log;
* the dedup state is read from the store, never held in memory.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core import health_alerts as ha
from modulo.settings import Settings


def _make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": "a" * 32,
        "fernet_key": "a" * 32,
        "modulo_admin_password": "test",
        "redis_url": "redis://localhost:6379/0",
        "smtp_host": "smtp.example.com",
        "alert_email_to": "ops@example.com, sre@example.com",
        "email_from": "modulo@example.com",
    }
    base.update(overrides)
    return Settings(**base)


def _healthy() -> ha.HealthObservation:
    return ha.HealthObservation(status="ok", checks={"database": ha.SubCheck(status="ok", detail="connected")})


def _unhealthy() -> ha.HealthObservation:
    return ha.HealthObservation(
        status="unavailable",
        checks={
            "database": ha.SubCheck(status="unavailable", detail="connection refused"),
            "redis": ha.SubCheck(status="degraded", detail="read timeout"),
            "break_glass": ha.SubCheck(status="ok", detail="config clean"),
        },
    )


class _FakeObserver:
    """Controllable health result — the test flips ``observation`` per tick."""

    def __init__(self) -> None:
        self.observation = _healthy()
        self.calls = 0

    async def __call__(self) -> ha.HealthObservation:
        self.calls += 1
        return self.observation


class _FakeSender:
    """Stub email sender recording every attempted send."""

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.sent: list[dict[str, Any]] = []

    async def __call__(
        self,
        settings: Settings,
        to: list[str],
        subject: str,
        body_html: str,
        body_text: str,
    ) -> bool:
        self.sent.append(
            {"settings": settings, "to": list(to), "subject": subject, "html": body_html, "text": body_text}
        )
        return self.result


class _FakeRedis:
    """In-memory Redis double covering get/set/aclose (the state store)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def aclose(self) -> None:
        return None


class _FailingRedis:
    """A store that is down — the tick must fail quiet, never raise or send."""

    async def get(self, key: str) -> str | None:
        raise RuntimeError("redis down")

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        raise RuntimeError("redis down")

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_disabled_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hourly disabled-notice limiter is per-process state — reset it so
    one test's log never suppresses another's."""
    monkeypatch.setattr(ha, "_last_disabled_log_at", None)


async def _tick(
    observer: _FakeObserver,
    sender: _FakeSender,
    redis: Any,
    settings: Settings,
    clock: dict[str, float],
) -> dict[str, Any]:
    return await ha.run_health_alert_check(
        observe=observer,
        sender=sender,
        redis_client=redis,
        settings=settings,
        now=lambda: clock["now"],
    )


async def test_alert_sent_once_on_confirmed_degradation_then_silent() -> None:
    """healthy -> unhealthy emails EXACTLY one alert; sustained unhealthy
    emails nothing further (one email per incident, not one per tick)."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    # Healthy baseline: nothing to report.
    for _ in range(3):
        result = await _tick(observer, sender, store, settings, clock)
    assert not sender.sent
    assert result["status"] == "healthy"

    # First unhealthy tick: under confirmation (hysteresis) — still silent.
    observer.observation = _unhealthy()
    result = await _tick(observer, sender, store, settings, clock)
    assert not sender.sent
    assert result["action"] == "none"

    # Second consecutive unhealthy tick: confirmed -> exactly one alert email.
    result = await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 1
    assert result["action"] == "alert"

    # Sustained unhealthy: dedup keeps it at one.
    for _ in range(3):
        await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 1

    alert = sender.sent[0]
    assert alert["to"] == ["ops@example.com", "sre@example.com"]
    assert "unavailable" in alert["subject"].lower()
    # The email names the failing sub-checks WITH their detail.
    assert "database: unavailable (connection refused)" in alert["html"]
    assert "redis: degraded (read timeout)" in alert["html"]
    # The healthy advisory check is not reported as a problem.
    assert "break_glass" not in alert["html"]


async def test_recovery_email_on_confirmed_recovery_then_silent() -> None:
    """unhealthy -> healthy emails ONE recovery (the visible incident end);
    sustained healthy emails nothing."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = _unhealthy()
    await _tick(observer, sender, store, settings, clock)
    await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 1

    # One healthy tick: under confirmation — the alert must NOT close yet.
    observer.observation = _healthy()
    result = await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 1
    assert result["action"] == "none"

    # Second consecutive healthy tick: recovery email.
    result = await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 2
    assert result["action"] == "recovery"

    recovery = sender.sent[1]
    assert "recovered" in recovery["subject"].lower()
    # The recovery names what cleared (the conditions the alert reported).
    assert "database: unavailable (connection refused)" in recovery["html"]

    # Sustained healthy: no further emails.
    for _ in range(3):
        await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 2


async def test_single_tick_flap_sends_nothing_in_either_direction() -> None:
    """Hysteresis: a one-tick blip never confirms, so a flap produces ZERO
    emails — not an alert, not a recovery."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    await _tick(observer, sender, store, settings, clock)
    await _tick(observer, sender, store, settings, clock)

    observer.observation = _unhealthy()  # blip: exactly one tick
    await _tick(observer, sender, store, settings, clock)

    observer.observation = _healthy()  # back before confirmation
    await _tick(observer, sender, store, settings, clock)
    await _tick(observer, sender, store, settings, clock)

    assert not sender.sent


async def test_quiet_path_when_email_not_configured(caplog: pytest.LogCaptureFixture) -> None:
    """The compose default (no SMTP_HOST / ALERT_EMAIL_TO): the check still
    runs, never raises, never calls the sender, and logs why alerting is off
    at a low rate (hourly) rather than per tick."""
    settings = _make_settings(smtp_host="", alert_email_to=None)
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = _unhealthy()
    with caplog.at_level(logging.INFO, logger=ha.__name__):
        first = await _tick(observer, sender, store, settings, clock)
        # A second and third confirmed tick: still no send, still no raise.
        clock["now"] += 100.0
        second = await _tick(observer, sender, store, settings, clock)
        clock["now"] += 100.0
        third = await _tick(observer, sender, store, settings, clock)

    assert not sender.sent
    # The health check itself still ran and reported the degradation.
    assert first["status"] == "unhealthy"
    assert second["status"] == "unhealthy"
    assert third["status"] == "unhealthy"
    assert third["action"] == "disabled"

    disabled_logs = [
        record.getMessage() for record in caplog.records if "health_alerts.disabled" in record.getMessage()
    ]
    # Logged once (first tick), NOT once per tick inside the hourly window.
    assert len(disabled_logs) == 1
    # The notice says why and what would enable it.
    assert "SMTP_HOST" in disabled_logs[0]
    assert "ALERT_EMAIL_TO" in disabled_logs[0]

    # Past the hourly window it logs again — low rate, not silent.
    caplog.clear()
    clock["now"] += ha.DISABLED_LOG_INTERVAL_SECONDS + 1.0
    with caplog.at_level(logging.INFO, logger=ha.__name__):
        await _tick(observer, sender, store, settings, clock)
    later_logs = [record for record in caplog.records if "health_alerts.disabled" in record.getMessage()]
    assert len(later_logs) == 1


async def test_dedup_state_is_read_from_the_store_not_memory() -> None:
    """A fresh invocation with NO in-process state must consult the store:
    a pre-seeded 'already notified' record suppresses a re-alert, and an alert
    that did send is durably recorded."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    # Seed the store exactly as a previous (already-notified) invocation would
    # have left it — no module or instance state is shared with this call.
    store.data[ha.STATE_KEY] = json.dumps(
        {
            "notified": "unhealthy",
            "pending": "unhealthy",
            "pending_count": 7,
            "conditions": ["database: unavailable (connection refused)"],
            "since": 999_000.0,
        }
    )

    observer.observation = _unhealthy()
    result = await _tick(observer, sender, store, settings, clock)
    assert not sender.sent
    assert result["notified"] == "unhealthy"

    # And the flip side: the alert that DID send is persisted for the next run.
    store2 = _FakeRedis()
    sender2 = _FakeSender()
    observer2 = _FakeObserver()
    observer2.observation = _unhealthy()
    await _tick(observer2, sender2, store2, settings, clock)
    await _tick(observer2, sender2, store2, settings, clock)
    assert len(sender2.sent) == 1

    persisted = json.loads(store2.data[ha.STATE_KEY])
    assert persisted["notified"] == "unhealthy"
    assert persisted["pending_count"] == 2
    assert persisted["conditions"] == ["database: unavailable (connection refused)", "redis: degraded (read timeout)"]


async def test_failed_send_is_retried_on_the_next_confirmed_tick() -> None:
    """Send-then-commit: a failed SMTP send must NOT be recorded as notified
    (that would swallow the incident), so the next tick retries."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender(result=False)
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = _unhealthy()
    await _tick(observer, sender, store, settings, clock)
    result = await _tick(observer, sender, store, settings, clock)
    assert result["action"] == "send_failed"
    assert result["notified"] == "none"

    sender.result = True
    result = await _tick(observer, sender, store, settings, clock)
    assert result["action"] == "alert"
    assert result["notified"] == "unhealthy"
    # Two attempts total: the failed confirmed tick, then the successful retry.
    assert len(sender.sent) == 2


async def test_unreadable_store_fails_quiet_without_sending() -> None:
    """No dedup state -> no notification decision: logged and skipped, never
    raised, never emailed."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    clock = {"now": 1_000_000.0}

    observer.observation = _unhealthy()
    result = await ha.run_health_alert_check(
        observe=observer,
        sender=sender,
        redis_client=_FailingRedis(),
        settings=settings,
        now=lambda: clock["now"],
    )
    assert result["status"] == "failed"
    assert not sender.sent


async def test_observe_readiness_reuses_the_route_evaluation() -> None:
    """The default observer calls the SAME ``evaluate_readiness`` the
    /healthz/ready route runs — the checks are never reimplemented."""
    from modulo.api.routes.health import CheckResult, ReadinessResponse

    response = ReadinessResponse(
        status="degraded",
        version="test",
        uptime_seconds=12.5,
        checks={"db_hygiene": CheckResult(status="degraded", detail="dead-tuple ratio 0.18")},
    )
    with patch("modulo.api.routes.health.evaluate_readiness", new=AsyncMock(return_value=response)) as evaluate:
        observation = await ha._observe_readiness()

    assert evaluate.await_count == 1
    assert observation.status == "degraded"
    assert observation.checks["db_hygiene"].detail == "dead-tuple ratio 0.18"
    assert observation.observed_state == "unhealthy"


#: The detail the db_hygiene check reports when its probe did NOT complete
#: (FAR-1510) — a stalled event loop, not a hygiene finding.
_HYGIENE_NOT_MEASURED_DETAIL = (
    "database-hygiene probe did not complete within 1s (likely an event-loop stall; "
    "see event_loop_lag) — hygiene not measured"
)


async def test_non_ok_advisory_sub_check_with_ok_aggregate_never_emails() -> None:
    """FAR-1510: a degraded ``db_hygiene`` sub-check whose probe timed out
    leaves the aggregate at ``ok`` (the probe result is advisory, so
    ``evaluate_readiness`` excludes it from the gate).

    The alert keys on the AGGREGATE only for this shape — ``db_hygiene`` is
    deliberately OUTSIDE ``REAL_FAILURE_ADVISORY_CHECKS`` (FAR-1510's
    not-measured probe is a statement about the probe, not a hygiene
    failure), so a degraded result with an ``ok`` aggregate is visible in
    ``conditions()``, never an unhealthy observation, and never an email
    across the hysteresis window. The production false "[Modulo] Readiness
    degraded" emails were exactly this shape once the aggregate itself was
    flipped to degraded by the timeout.
    """
    observer = _FakeObserver()
    observer.observation = ha.HealthObservation(
        status="ok",
        checks={
            "database": ha.SubCheck(status="ok", detail="database reachable"),
            "db_hygiene": ha.SubCheck(status="degraded", detail=_HYGIENE_NOT_MEASURED_DETAIL),
        },
    )
    sender = _FakeSender()
    settings = _make_settings()
    redis = _FakeRedis()
    clock = {"now": 1_000.0}

    first = await _tick(observer, sender, redis, settings, clock)
    clock["now"] += 300.0
    second = await _tick(observer, sender, redis, settings, clock)

    # Two confirmed ticks — enough for hysteresis to fire had the state been
    # unhealthy — and still no send.
    assert observer.calls == 2
    assert first["action"] == "none"
    assert second["action"] == "none"
    assert second["notified"] == "none"
    assert not sender.sent
    # The finding is still NAMED (an operator reading the conditions sees it).
    assert observer.observation.conditions() == [f"db_hygiene: degraded ({_HYGIENE_NOT_MEASURED_DETAIL})"]
    # And the observation stays healthy: the not-measured probe is NOT a
    # real-failure advisory, so it cannot arm the state machine (FAR-1510/1571).
    assert observer.observation.observed_state == "healthy"


# ---------------------------------------------------------------------------
# FAR-1571: advisory coverage. The readiness AGGREGATE excludes the advisory
# checks, so a dead/erroring SWEEP (real breakage) must still alert — while
# benign advisories and single-tick blips must NOT page (no FAR-1512 noise).
# Every observation below has aggregate status "ok": only the real-failure
# set can flip the state.
# ---------------------------------------------------------------------------


def _advisory_observation(*, sweep: ha.SubCheck, benign: ha.SubCheck | None = None) -> ha.HealthObservation:
    """An ``ok``-aggregate observation carrying one real sweep failure plus
    (optionally) a benign advisory — the FAR-1571 alerting shape."""
    checks = {
        "database": ha.SubCheck(status="ok", detail="connected"),
        "runner_marker_sweep": sweep,
    }
    if benign is not None:
        checks["event_loop_lag"] = benign
    return ha.HealthObservation(status="ok", checks=checks)


def test_real_failure_advisory_set_matches_the_readiness_taxonomy() -> None:
    """The classification is pinned: exactly the seven real-breakage sweeps
    alert, and the three benign/other-channel advisories never do (the
    readiness ``checks`` dict in ``api.routes.health`` is the taxonomy)."""
    assert {
        "dispatcher_reconcile",
        "stale_run_recovery",
        "slot_reconciliation",
        "hitl_park_sweep",
        "runner_workspace_reconcile",
        "runner_marker_sweep",
        "runner_health_probe",
    } == ha.REAL_FAILURE_ADVISORY_CHECKS
    benign = {"event_loop_lag", "break_glass", "db_hygiene"}
    assert not (benign & ha.REAL_FAILURE_ADVISORY_CHECKS)


def test_observed_state_and_reported_status_for_the_advisory_shapes() -> None:
    """A real advisory failure with an ``ok`` aggregate is unhealthy and must
    report ``degraded``; the benign ones stay healthy and report ``ok``; an
    unavailable aggregate still reports ``unavailable``."""
    real = ha.HealthObservation(
        status="ok",
        checks={"runner_marker_sweep": ha.SubCheck(status="degraded", detail="no sweep in 22m")},
    )
    assert real.observed_state == "unhealthy"
    assert real.reported_status == "degraded"

    for name in ("event_loop_lag", "break_glass", "db_hygiene"):
        benign = ha.HealthObservation(status="ok", checks={name: ha.SubCheck(status="degraded", detail="transient")})
        assert benign.observed_state == "healthy", name
        assert benign.reported_status == "ok", name

    down = ha.HealthObservation(
        status="unavailable",
        checks={"database": ha.SubCheck(status="unavailable", detail="connection refused")},
    )
    assert down.observed_state == "unhealthy"
    assert down.reported_status == "unavailable"


async def test_sustained_real_advisory_failure_alerts_once_then_recovers() -> None:
    """FAR-1571: a SUSTAINED dead sweep (``runner_marker_sweep: degraded``,
    aggregate ``ok``) is real breakage. Across CONFIRM_TICKS it emails
    EXACTLY ONE alert whose subject reports ``degraded`` — never the
    misleading aggregate ``ok`` — stays silent while sustained, and emails
    one recovery when the sweep comes back."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = _advisory_observation(sweep=ha.SubCheck(status="degraded", detail="no sweep in 22m"))

    # First unhealthy tick: under confirmation — still silent.
    first = await _tick(observer, sender, store, settings, clock)
    assert not sender.sent
    assert first["action"] == "none"
    assert first["status"] == "unhealthy"

    # Second consecutive tick: confirmed -> exactly one alert email.
    second = await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 1
    assert second["action"] == "alert"

    # Sustained: dedup keeps it at one (never one per tick).
    for _ in range(3):
        await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 1

    alert = sender.sent[0]
    # The reported status is derived, not the raw aggregate: "degraded", and
    # the body says the same.
    assert alert["subject"] == "[Modulo] Readiness degraded"
    assert "Readiness is now <strong>degraded</strong>" in alert["html"]
    # The failing sweep is named WITH its detail.
    assert "runner_marker_sweep: degraded (no sweep in 22m)" in alert["html"]
    assert "runner_marker_sweep: degraded (no sweep in 22m)" in alert["text"]

    # The sweep recovers: one healthy tick under confirmation, then ONE
    # recovery email naming what cleared.
    observer.observation = _healthy()
    pending = await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 1
    assert pending["action"] == "none"

    result = await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 2
    assert result["action"] == "recovery"
    recovery = sender.sent[1]
    assert "recovered" in recovery["subject"].lower()
    assert "runner_marker_sweep" in recovery["html"]

    for _ in range(3):
        await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 2


async def test_single_tick_real_advisory_blip_sends_nothing() -> None:
    """Hysteresis applies to advisory alerts too: a real sweep failure that
    lasts ONE tick (probe flake) never confirms, so ZERO emails."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    await _tick(observer, sender, store, settings, clock)
    await _tick(observer, sender, store, settings, clock)

    observer.observation = _advisory_observation(sweep=ha.SubCheck(status="degraded", detail="flake"))
    blip = await _tick(observer, sender, store, settings, clock)
    # The blip IS observed as unhealthy (real failure class) but only one
    # tick deep — under CONFIRM_TICKS, so no send.
    assert blip["status"] == "unhealthy"
    assert blip["action"] == "none"

    observer.observation = _healthy()
    await _tick(observer, sender, store, settings, clock)
    await _tick(observer, sender, store, settings, clock)

    assert not sender.sent


async def test_benign_event_loop_lag_advisory_never_alerts() -> None:
    """A benign advisory (``event_loop_lag: degraded``, aggregate ``ok``)
    sustained across the hysteresis window must NOT page — it is a transient
    stall diagnostic, visible in the readiness body only (no FAR-1512 noise)."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = ha.HealthObservation(
        status="ok",
        checks={
            "database": ha.SubCheck(status="ok", detail="connected"),
            "event_loop_lag": ha.SubCheck(status="degraded", detail="EVENT-LOOP STALL: loop delayed up to 2400ms"),
        },
    )

    results: list[dict[str, Any]] = []
    for _ in range(4):  # well past CONFIRM_TICKS
        results.append(await _tick(observer, sender, store, settings, clock))
        clock["now"] += 300.0

    assert not sender.sent
    assert {result["action"] for result in results} == {"none"}
    assert {result["status"] for result in results} == {"healthy"}
    # And it is never reported as a "Failing check": a benign, non-triggering
    # advisory contributes nothing to observed_state, so naming it as the
    # reason an alert fired is pure alarm fatigue (BENIGN_NON_TRIGGERING_CHECKS).
    assert not observer.observation.conditions()


async def test_gating_check_degraded_still_alerts() -> None:
    """Regression: a GATING check degraded flips the aggregate to ``degraded``
    and alerts exactly as before (reported status ``degraded``)."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = ha.HealthObservation(
        status="degraded",
        checks={
            "database": ha.SubCheck(status="degraded", detail="dead-tuple ratio 0.18"),
            "redis": ha.SubCheck(status="ok", detail="connected"),
        },
    )

    await _tick(observer, sender, store, settings, clock)
    result = await _tick(observer, sender, store, settings, clock)

    assert result["action"] == "alert"
    assert len(sender.sent) == 1
    assert sender.sent[0]["subject"] == "[Modulo] Readiness degraded"
    assert "database: degraded (dead-tuple ratio 0.18)" in sender.sent[0]["html"]


async def test_real_failure_alongside_benign_advisory_alerts_and_names_only_the_real_one() -> None:
    """A real sweep failure WITH a benign advisory alongside (both non-``ok``,
    aggregate ``ok``): the real one drives the alert and is named in the body;
    the benign one (``event_loop_lag``) is NOT listed as a failing check — it
    cannot contribute to ``observed_state``, so blaming it is alarm fatigue
    (``BENIGN_NON_TRIGGERING_CHECKS``)."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = _advisory_observation(
        sweep=ha.SubCheck(status="degraded", detail="no sweep in 22m"),
        benign=ha.SubCheck(status="degraded", detail="EVENT-LOOP STALL: loop delayed up to 2400ms"),
    )

    await _tick(observer, sender, store, settings, clock)
    result = await _tick(observer, sender, store, settings, clock)

    assert result["action"] == "alert"
    assert len(sender.sent) == 1
    alert = sender.sent[0]
    assert alert["subject"] == "[Modulo] Readiness degraded"
    # The real failure is named (it is the reason the alert fired)...
    assert "runner_marker_sweep: degraded (no sweep in 22m)" in alert["html"]
    assert "runner_marker_sweep: degraded (no sweep in 22m)" in alert["text"]
    # ...the benign advisory is not reported as a failing check anywhere.
    assert "event_loop_lag" not in alert["html"]
    assert "event_loop_lag" not in alert["text"]
    # Healthy checks are not reported as problems either.
    assert "database:" not in alert["html"]


# ---------------------------------------------------------------------------
# FAR-1571 follow-up: recovery honesty, unavailable-precedence, and the
# taxonomy drift-guard.
# ---------------------------------------------------------------------------


async def test_recovery_reports_only_conditions_that_actually_cleared() -> None:
    """The recovery email must report only what ACTUALLY cleared: exactly one
    recovery email, the cleared sweep named, and the still-degraded benign
    advisory never claimed as resolved (it appears nowhere in the recovery
    email — and, since benign advisories are no longer listed as failing
    checks at alert time either, nowhere in the alert email either)."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    lag_bullet = "event_loop_lag: degraded (EVENT-LOOP STALL: loop delayed up to 2400ms)"
    sweep_bullet = "runner_marker_sweep: degraded (no sweep in 22m)"

    # Incident: real sweep dead AND the benign advisory degraded, aggregate ok.
    observer.observation = _advisory_observation(
        sweep=ha.SubCheck(status="degraded", detail="no sweep in 22m"),
        benign=ha.SubCheck(status="degraded", detail="EVENT-LOOP STALL: loop delayed up to 2400ms"),
    )
    await _tick(observer, sender, store, settings, clock)
    result = await _tick(observer, sender, store, settings, clock)
    assert result["action"] == "alert"
    assert len(sender.sent) == 1
    # The alert recorded ONLY the real condition: the benign advisory is not
    # a failing check (BENIGN_NON_TRIGGERING_CHECKS), so it is never stored,
    # never reported, and can therefore never be claimed as cleared either.
    assert sweep_bullet in sender.sent[0]["html"]
    assert lag_bullet not in sender.sent[0]["html"]
    assert lag_bullet not in sender.sent[0]["text"]

    # The sweep recovers; the benign advisory is STILL degraded.
    observer.observation = _advisory_observation(
        sweep=ha.SubCheck(status="ok", detail="sweep ran 1m ago"),
        benign=ha.SubCheck(status="degraded", detail="EVENT-LOOP STALL: loop delayed up to 2400ms"),
    )
    pending = await _tick(observer, sender, store, settings, clock)
    assert pending["action"] == "none"  # hysteresis still applies
    result = await _tick(observer, sender, store, settings, clock)
    assert result["action"] == "recovery"
    assert len(sender.sent) == 2

    recovery = sender.sent[1]
    assert "recovered" in recovery["subject"].lower()
    # What actually cleared IS reported...
    assert sweep_bullet in recovery["html"]
    assert sweep_bullet in recovery["text"]
    # ...and the still-failing benign advisory is NOT claimed cleared — it
    # appears nowhere in either part of the recovery email.
    assert "event_loop_lag" not in recovery["html"]
    assert "event_loop_lag" not in recovery["text"]

    # Exactly one recovery: staying that way emails nothing further.
    for _ in range(3):
        await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 2


async def test_unavailable_aggregate_with_real_advisory_reports_unavailable_once() -> None:
    """Precedence: aggregate ``unavailable`` WITH a real-failure advisory
    non-ok -> the reported status is ``unavailable`` (the unavailable branch
    wins over the advisory-degraded branch) and exactly ONE alert fires."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = ha.HealthObservation(
        status="unavailable",
        checks={
            "database": ha.SubCheck(status="unavailable", detail="connection refused"),
            "runner_marker_sweep": ha.SubCheck(status="degraded", detail="no sweep in 22m"),
        },
    )
    assert observer.observation.reported_status == "unavailable"

    await _tick(observer, sender, store, settings, clock)
    result = await _tick(observer, sender, store, settings, clock)
    assert result["action"] == "alert"
    assert len(sender.sent) == 1
    assert sender.sent[0]["subject"] == "[Modulo] Readiness unavailable"

    for _ in range(3):
        await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 1


async def test_recovery_lists_remaining_when_no_alert_time_condition_cleared() -> None:
    """Defensive branch: when NOTHING can be confirmed cleared, the recovery
    says so honestly and lists what remains — it never claims an empty (or
    false) set of cleared conditions."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}
    # Seeded incident record whose only alert-time condition is the benign
    # advisory — still non-ok in the current observation.
    store.data[ha.STATE_KEY] = json.dumps(
        {
            "notified": "unhealthy",
            "pending": "unhealthy",
            "pending_count": 5,
            "conditions": ["event_loop_lag: degraded (stall)"],
            "since": 999_000.0,
        }
    )
    observer.observation = ha.HealthObservation(
        status="ok",
        checks={
            "database": ha.SubCheck(status="ok", detail="connected"),
            "event_loop_lag": ha.SubCheck(status="degraded", detail="stall"),
        },
    )

    await _tick(observer, sender, store, settings, clock)
    result = await _tick(observer, sender, store, settings, clock)

    assert result["action"] == "recovery"
    assert len(sender.sent) == 1
    recovery = sender.sent[0]
    # Honest copy: nothing cleared is SAID, and what remains is listed.
    assert "no alert-time condition has cleared" in recovery["html"]
    assert "event_loop_lag: degraded (stall)" in recovery["html"]
    assert "The following conditions have cleared" not in recovery["html"]


async def test_recovery_says_so_when_no_alert_time_condition_was_recorded() -> None:
    """Defensive branch: an incident record with NO alert-time conditions
    recovers with an honest sentence instead of an empty 'cleared' list."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}
    store.data[ha.STATE_KEY] = json.dumps(
        {
            "notified": "unhealthy",
            "pending": "unhealthy",
            "pending_count": 5,
            "conditions": [],
            "since": 999_000.0,
        }
    )
    observer.observation = _healthy()

    await _tick(observer, sender, store, settings, clock)
    result = await _tick(observer, sender, store, settings, clock)

    assert result["action"] == "recovery"
    assert len(sender.sent) == 1
    recovery = sender.sent[0]
    assert "no alert-time condition is still failing" in recovery["html"]
    assert "no alert-time condition is still failing" in recovery["text"]
    assert "The following conditions have cleared" not in recovery["html"]


def test_advisory_classification_tracks_the_readiness_taxonomy() -> None:
    """FAR-1571 drift-guard: the classification must stay in sync with the
    readiness route, which IS the taxonomy (``api.routes.health`` — PR #1372
    owns that file; this test only READS its source).

    Parse ``evaluate_readiness``'s own ``checks`` dict literal for every
    check name and its ``statuses`` aggregation list for the GATING names;
    the ADVISORY remainder must equal this module's real-failure set plus
    the three deliberate benign exclusions. A check added/renamed/removed
    over there fails HERE rather than silently losing alert coverage.

    The two production constants are pinned against this same parse:
    ``BENIGN_NON_TRIGGERING_CHECKS`` (the REPORTING-benign set — never
    listed as a failing check) must be exactly the benign literal minus
    ``db_hygiene`` (a graded failure that still gates, so it MUST be
    reported), and must never overlap the real-failure set — otherwise a
    future rename/reclassify could drift the reporting set with no test red.
    """
    from modulo.api.routes import health as health_module

    source = inspect.getsource(health_module.evaluate_readiness)
    checks_block = re.search(r"checks: dict\[str, CheckResult\] = \{(.*?)\n    \}", source, re.DOTALL)
    assert checks_block is not None, "readiness `checks` dict literal not found — health.py changed shape"
    name_to_var = dict(re.findall(r'^[ ]{8}"([a-z0-9_]+)": [ ]*([a-z_]+),[ ]*$', checks_block.group(1), re.MULTILINE))
    assert len(name_to_var) >= 10, f"parsed only {sorted(name_to_var)!r} — health.py checks dict shape changed"

    statuses_block = re.search(r"statuses = \[(.*?)\]", source, re.DOTALL)
    assert statuses_block is not None, "aggregate `statuses` list not found — health.py changed shape"
    gating_vars = set(re.findall(r"([a-z_]+_check)\b", statuses_block.group(1)))
    advisory_names = {name for name, var in name_to_var.items() if var not in gating_vars}

    benign = {"event_loop_lag", "break_glass", "db_hygiene"}
    assert advisory_names == ha.REAL_FAILURE_ADVISORY_CHECKS | benign
    # The reporting-benign set is the benign literal minus db_hygiene (a
    # graded failure that gates the aggregate and must keep being reported).
    assert set(benign) - {"db_hygiene"} == ha.BENIGN_NON_TRIGGERING_CHECKS
    # ...and can never drift INTO the real-failure set (a check that pages
    # must not also be filtered from the body that explains the page).
    assert not (ha.BENIGN_NON_TRIGGERING_CHECKS & ha.REAL_FAILURE_ADVISORY_CHECKS)


# ---------------------------------------------------------------------------
# Default collaborators, malformed state, and failure edges.  The transition
# tests above stub the observer/sender/redis and only drive the happy paths;
# these exercise the remaining changed lines and branches directly.
# ---------------------------------------------------------------------------


def test_recipients_splits_and_drops_empties() -> None:
    """Recipients are comma-split, trimmed, and empties dropped."""
    settings = _make_settings(alert_email_to=" ops@example.com , ,sre@example.com ")
    assert ha._recipients(settings) == ["ops@example.com", "sre@example.com"]


def test_recipients_empty_when_unset() -> None:
    assert not ha._recipients(_make_settings(alert_email_to=""))
    assert not ha._recipients(_make_settings(alert_email_to=None))


def test_alerting_configured_requires_both_halves() -> None:
    """``SMTP_HOST`` AND ``ALERT_EMAIL_TO`` are both required."""
    assert ha.alerting_configured(_make_settings()) is True
    assert ha.alerting_configured(_make_settings(smtp_host="")) is False
    assert ha.alerting_configured(_make_settings(alert_email_to="")) is False


# ---------------------------------------------------------------------------
# ALERT_ENVIRONMENTS: the shared environment allowlist gate
# (core.alert_context.alerting_enabled_for_environment — ONE definition for
# both alert channels). Unset/blank = alert in every environment; set = only
# when settings.environment is listed.
# ---------------------------------------------------------------------------


def test_environment_allowlist_unset_allows_every_environment() -> None:
    """Unset (the compose/self-hosted default) or blank must alert EVERYWHERE
    — a deployment that never heard of the allowlist keeps working."""
    assert ha.alerting_enabled_for_environment(_make_settings()) is True
    assert ha.alerting_enabled_for_environment(_make_settings(ALERT_ENVIRONMENTS="")) is True
    assert ha.alerting_enabled_for_environment(_make_settings(ALERT_ENVIRONMENTS="   ")) is True
    # Punctuation that parses to no entries is blank, not "allow nothing".
    assert ha.alerting_enabled_for_environment(_make_settings(ALERT_ENVIRONMENTS=" , , ")) is True


def test_environment_allowlist_matches_case_insensitively_and_trims() -> None:
    """Entries are comma-split, trimmed and case-folded on BOTH sides."""
    allowed = _make_settings(ALERT_ENVIRONMENTS=" Production , staging ", MODULO_ENV="staging")
    assert ha.alerting_enabled_for_environment(allowed) is True
    assert (
        ha.alerting_enabled_for_environment(
            _make_settings(ALERT_ENVIRONMENTS="production,staging", MODULO_ENV="Production")
        )
        is True
    )
    excluded = _make_settings(ALERT_ENVIRONMENTS="production", MODULO_ENV="staging")
    assert ha.alerting_enabled_for_environment(excluded) is False
    # An environment that is not in a non-empty allowlist never alerts, even
    # the self-hosted default one.
    assert (
        ha.alerting_enabled_for_environment(_make_settings(ALERT_ENVIRONMENTS="production", MODULO_ENV="development"))
        is False
    )


async def test_excluded_environment_never_sends_but_state_machine_advances(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``ALERT_ENVIRONMENTS=production`` + ``MODULO_ENV=staging``: health
    is still evaluated, the hysteresis/dedup state still advances (so the
    operator loses no history if the allowlist changes), but NOTHING is sent —
    exactly the disabled-channel quiet path, with its log-once notice."""
    settings = _make_settings(MODULO_ENV="staging", ALERT_ENVIRONMENTS="production")
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = _unhealthy()
    with caplog.at_level(logging.INFO, logger=ha.__name__):
        first = await _tick(observer, sender, store, settings, clock)
        clock["now"] += 100.0
        second = await _tick(observer, sender, store, settings, clock)

    # Health still evaluated and reported...
    assert first["status"] == "unhealthy"
    assert second["status"] == "unhealthy"
    # ...the state machine advanced across BOTH confirmed ticks...
    assert first["pending_count"] == 1
    assert second["pending_count"] == 2
    assert second["action"] == "disabled"
    assert second["notified"] == "none"
    persisted = json.loads(store.data[ha.STATE_KEY])
    assert persisted["pending"] == "unhealthy"
    assert persisted["pending_count"] == 2
    # ...but nothing was emailed.
    assert not sender.sent
    # The quiet path stays QUIET-but-visible: one notice in the window, and
    # it names the setting that would re-enable alerting here.
    disabled_logs = [
        record.getMessage() for record in caplog.records if "health_alerts.disabled" in record.getMessage()
    ]
    assert len(disabled_logs) == 1
    assert "ALERT_ENVIRONMENTS" in disabled_logs[0]


async def test_included_environment_still_sends() -> None:
    """``ALERT_ENVIRONMENTS=production`` + ``MODULO_ENV=production``:
    the allowlist does not suppress the environment it names — exactly one
    alert email on the confirmed edge."""
    settings = _make_settings(MODULO_ENV="production", ALERT_ENVIRONMENTS="production")
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = _unhealthy()
    await _tick(observer, sender, store, settings, clock)
    result = await _tick(observer, sender, store, settings, clock)

    assert result["action"] == "alert"
    assert len(sender.sent) == 1
    assert "unavailable" in sender.sent[0]["subject"].lower()


async def test_excluded_recovery_edge_clears_incident_then_reinclusion_alerts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F2 (health_alerts): while EXCLUDED, a confirmed RECOVERY edge closes
    the incident record WITHOUT sending. Leaving ``notified="unhealthy"``
    stranding would mean a distinct later incident after re-inclusion is
    never alerted, and a much later recovery email would name this old
    incident's conditions/duration."""
    excluded = _make_settings(MODULO_ENV="staging", ALERT_ENVIRONMENTS="production")
    included = _make_settings(MODULO_ENV="production", ALERT_ENVIRONMENTS="production")
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    # An incident that WAS alerted while the environment was included.
    store.data[ha.STATE_KEY] = json.dumps(
        {
            "notified": "unhealthy",
            "pending": "unhealthy",
            "pending_count": 5,
            "conditions": ["database: unavailable (connection refused)"],
            "since": 999_000.0,
        }
    )

    # 1. The incident recovers while excluded: no email, record closed.
    observer.observation = _healthy()
    with caplog.at_level(logging.INFO, logger=ha.__name__):
        await _tick(observer, sender, store, excluded, clock)
        result = await _tick(observer, sender, store, excluded, clock)

    assert result["action"] == "recovery_suppressed"
    assert result["notified"] == "none"
    assert not sender.sent
    persisted = json.loads(store.data[ha.STATE_KEY])
    assert persisted["notified"] is None
    assert not persisted["conditions"]
    assert persisted["since"] is None
    # Hysteresis state still advanced — only the notified record is closed.
    assert persisted["pending"] == "healthy"
    assert persisted["pending_count"] == 2  # seeded "unhealthy" -> fresh 2-tick healthy run
    # Quiet-but-visible: the excluded notice stays rate-limited across ticks.
    disabled_logs = [record for record in caplog.records if "health_alerts.disabled" in record.getMessage()]
    assert len(disabled_logs) == 1

    # 2. A DISTINCT new incident after re-inclusion alerts (no stranding).
    observer.observation = _unhealthy()
    await _tick(observer, sender, store, included, clock)
    result = await _tick(observer, sender, store, included, clock)

    assert result["action"] == "alert"
    assert len(sender.sent) == 1
    assert "unavailable" in sender.sent[0]["subject"].lower()
    # The alert reports the NEW incident's conditions, not the old ones.
    assert "database: unavailable (connection refused)" in sender.sent[0]["html"]


def test_conditions_name_non_ok_checks_without_detail() -> None:
    """A non-``ok`` check with no detail still produces a bullet."""
    observation = ha.HealthObservation(
        status="degraded",
        checks={
            "redis": ha.SubCheck(status="degraded", detail=None),
            "database": ha.SubCheck(status="ok", detail="connected"),
        },
    )
    assert observation.conditions() == ["redis: degraded"]
    assert observation.observed_state == "unhealthy"


def test_conditions_omit_benign_non_triggering_checks() -> None:
    """``break_glass`` (an expected config posture) and ``event_loop_lag`` (a
    transient stall diagnostic) are non-``ok`` but never contribute to
    ``observed_state``, so they must NOT be listed as "Failing checks" —
    every other check still is, ``db_hygiene`` (a genuine gating failure)
    included."""
    assert frozenset({"break_glass", "event_loop_lag"}) == ha.BENIGN_NON_TRIGGERING_CHECKS

    observation = ha.HealthObservation(
        status="degraded",
        checks={
            "break_glass": ha.SubCheck(status="degraded", detail="standby secret unset"),
            "event_loop_lag": ha.SubCheck(status="degraded", detail="EVENT-LOOP STALL: loop delayed up to 2400ms"),
            "dispatcher_reconcile": ha.SubCheck(status="degraded", detail="stale 240s since last run"),
            "db_hygiene": ha.SubCheck(status="degraded", detail="dead-tuple ratio 0.72"),
        },
    )
    assert observation.conditions() == [
        "db_hygiene: degraded (dead-tuple ratio 0.72)",
        "dispatcher_reconcile: degraded (stale 240s since last run)",
    ]

    # A benign-only observation reports nothing (it cannot page either).
    benign_only = ha.HealthObservation(
        status="ok",
        checks={
            "break_glass": ha.SubCheck(status="degraded", detail="expected posture"),
            "event_loop_lag": ha.SubCheck(status="degraded", detail="stall"),
        },
    )
    assert not benign_only.conditions()

    # non_ok_names() stays UNfiltered so the recovery split can never claim a
    # still-degraded benign advisory cleared.
    assert benign_only.non_ok_names() == {"break_glass", "event_loop_lag"}


def test_from_raw_malformed_json_starts_fresh(caplog: pytest.LogCaptureFixture) -> None:
    """Unparsable stored JSON degrades to a fresh state (never crashes)."""
    with caplog.at_level(logging.WARNING, logger=ha.__name__):
        state = ha._AlertState.from_raw("{not valid json")
    assert state.notified is None
    assert state.pending_count == 0
    assert "health_alerts.state_unparsable" in caplog.text


def test_from_raw_non_dict_starts_fresh(caplog: pytest.LogCaptureFixture) -> None:
    """A well-formed JSON value that is not an object also starts fresh."""
    with caplog.at_level(logging.WARNING, logger=ha.__name__):
        state = ha._AlertState.from_raw(json.dumps(["not", "a", "dict"]))
    assert state.pending is None
    assert state.pending_count == 0
    assert "health_alerts.state_unexpected_shape" in caplog.text


async def test_send_via_smtp_delegates_to_email_sender() -> None:
    """The default sender offloads the sync ``send_email`` and returns its bool."""
    settings = _make_settings()
    with patch.object(ha, "send_email", return_value=True) as send:
        delivered = await ha._send_via_smtp(settings, ["ops@example.com"], "s", "<p>h</p>", "t")
    assert delivered is True
    send.assert_called_once_with(settings, ["ops@example.com"], "s", "<p>h</p>", "t")


async def test_send_via_smtp_swallows_email_errors(caplog: pytest.LogCaptureFixture) -> None:
    """An ``EmailSendingError`` becomes ``False`` so the tick retries."""
    settings = _make_settings()
    with (
        caplog.at_level(logging.WARNING, logger=ha.__name__),
        patch.object(ha, "send_email", side_effect=ha.EmailSendingError("smtp refused")),
    ):
        delivered = await ha._send_via_smtp(settings, ["ops@example.com"], "s", "h", "t")
    assert delivered is False
    assert "health_alerts.email_send_failed" in caplog.text


async def test_send_via_smtp_swallows_unexpected_errors(caplog: pytest.LogCaptureFixture) -> None:
    """Any other sender failure also becomes ``False``, never a crash."""
    settings = _make_settings()
    with (
        caplog.at_level(logging.WARNING, logger=ha.__name__),
        patch.object(ha, "send_email", side_effect=RuntimeError("boom")),
    ):
        delivered = await ha._send_via_smtp(settings, ["ops@example.com"], "s", "h", "t")
    assert delivered is False
    assert "health_alerts.email_send_failed" in caplog.text


async def test_send_via_smtp_propagates_cancellation() -> None:
    """Cancellation must propagate — it is not a send failure to retry."""
    settings = _make_settings()
    with (
        patch.object(ha, "send_email", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await ha._send_via_smtp(settings, ["ops@example.com"], "s", "h", "t")


async def test_recovery_quiet_when_email_unconfigured() -> None:
    """A seeded incident recovering with no SMTP config takes the recovery
    quiet branch: no send, state untouched."""
    settings = _make_settings(smtp_host="", alert_email_to=None)
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}
    store.data[ha.STATE_KEY] = json.dumps(
        {
            "notified": "unhealthy",
            "pending": "unhealthy",
            "pending_count": 5,
            "conditions": ["database: unavailable (connection refused)"],
            "since": 999_000.0,
        }
    )

    observer.observation = _healthy()
    await _tick(observer, sender, store, settings, clock)
    result = await _tick(observer, sender, store, settings, clock)

    assert not sender.sent
    assert result["action"] == "disabled"
    assert json.loads(store.data[ha.STATE_KEY])["notified"] == "unhealthy"


async def test_recovery_send_failure_is_retried() -> None:
    """A failed recovery send is not committed, so the next tick retries."""
    settings = _make_settings()
    observer = _FakeObserver()
    sender = _FakeSender(result=False)
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}
    store.data[ha.STATE_KEY] = json.dumps(
        {
            "notified": "unhealthy",
            "pending": "unhealthy",
            "pending_count": 5,
            "conditions": ["database: unavailable (connection refused)"],
            "since": 999_000.0,
        }
    )

    observer.observation = _healthy()
    await _tick(observer, sender, store, settings, clock)
    result = await _tick(observer, sender, store, settings, clock)

    assert result["action"] == "send_failed"
    assert json.loads(store.data[ha.STATE_KEY])["notified"] == "unhealthy"


class _BoomObserver:
    """Observer whose readiness evaluation always raises."""

    async def __call__(self) -> ha.HealthObservation:
        raise RuntimeError("probe blew up")


class _CancelledObserver:
    """Observer that raises ``CancelledError`` (shutdown, not failure)."""

    async def __call__(self) -> ha.HealthObservation:
        raise asyncio.CancelledError


async def test_observe_failure_is_logged_and_non_fatal(caplog: pytest.LogCaptureFixture) -> None:
    """A failed evaluation is reported, not raised — the next tick retries."""
    with caplog.at_level(logging.WARNING, logger=ha.__name__):
        result = await ha.run_health_alert_check(
            observe=_BoomObserver(),
            sender=_FakeSender(),
            redis_client=_FakeRedis(),
            settings=_make_settings(),
            now=lambda: 1.0,
        )
    assert result["status"] == "observe_failed"
    assert "probe blew up" in result["error"]
    assert "health_alerts.observe_failed" in caplog.text


async def test_observe_cancellation_propagates() -> None:
    """Cancellation during evaluation must not be swallowed."""
    with pytest.raises(asyncio.CancelledError):
        await ha.run_health_alert_check(
            observe=_CancelledObserver(),
            sender=_FakeSender(),
            redis_client=_FakeRedis(),
            settings=_make_settings(),
            now=lambda: 1.0,
        )


class _CancellingRedis:
    """Store whose ``get`` raises ``CancelledError`` during a tick."""

    async def get(self, key: str) -> str | None:
        raise asyncio.CancelledError

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        raise AssertionError("set must not be reached after cancellation")

    async def aclose(self) -> None:
        return None


async def test_tick_cancellation_propagates() -> None:
    """Cancellation while reading the dedup store must not be swallowed."""
    with pytest.raises(asyncio.CancelledError):
        await ha.run_health_alert_check(
            observe=_FakeObserver(),
            sender=_FakeSender(),
            redis_client=_CancellingRedis(),
            settings=_make_settings(),
            now=lambda: 1.0,
        )


async def test_owned_redis_client_is_created_and_closed() -> None:
    """With no injected client the tick builds one from ``settings.redis_url``
    and closes it in the ``finally`` (it owns the connection)."""
    settings = _make_settings()
    store = _FakeRedis()
    with patch.object(ha.aioredis.Redis, "from_url", return_value=store) as from_url:
        result = await ha.run_health_alert_check(
            observe=_FakeObserver(),
            sender=_FakeSender(),
            settings=settings,
            now=lambda: 1.0,
        )
    from_url.assert_called_once()
    assert result["notified"] == "none"


async def test_alert_and_recovery_bodies_carry_environment_and_context() -> None:
    """FAR-1495: BOTH the alert and the recovery email identify the deployment
    environment and append the operator's ALERT_CONTEXT free text — in the
    HTML part and the text part alike, with the context after the detection
    stamp in the text part."""
    settings = _make_settings(
        # Settings keys match the field's env alias case-insensitively (see
        # test_alert_context.py) — hence MODULO_ENV / ALERT_CONTEXT here.
        MODULO_ENV="staging",
        ALERT_CONTEXT="runbook: https://example.com/runbook\npage the on-call",
    )
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = _unhealthy()
    await _tick(observer, sender, store, settings, clock)
    await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 1

    alert = sender.sent[0]
    for part in (alert["html"], alert["text"]):
        assert "Environment: staging" in part
        assert "runbook: https://example.com/runbook" in part
        assert "page the on-call" in part
    # The context follows the detection stamp in the text part.
    assert alert["text"].index("Detected at") < alert["text"].index("Environment: staging")

    observer.observation = _healthy()
    await _tick(observer, sender, store, settings, clock)
    await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 2

    recovery = sender.sent[1]
    for part in (recovery["html"], recovery["text"]):
        assert "Environment: staging" in part
        assert "runbook: https://example.com/runbook" in part
    assert recovery["text"].index("Resolved at") < recovery["text"].index("Environment: staging")


# ---------------------------------------------------------------------------
# FAR-1495 follow-up: the stdout stamp is STRICTLY best-effort (a print failure
# must never skip the send/state commit that surrounds it) and it carries the
# conditions plus the environment line ONLY - never the operator's
# ALERT_CONTEXT free text, which is repr=False precisely to keep it out of logs.
# ---------------------------------------------------------------------------


def _failing_stamp_print(prefix: str) -> Callable[..., None]:
    """A ``print`` replacement that fails ONLY for the alert stamp.

    A blanket ``patch("builtins.print", side_effect=...)`` would also break
    pytest's logging formatter (it calls ``print`` while rendering an
    ``exc_info`` traceback), which masks the behaviour under test - so only
    the stamp's own message raises and everything else prints normally.
    """
    real_print = print

    def _print(*args: Any, **kwargs: Any) -> None:
        message = args[0] if args else ""
        if isinstance(message, str) and message.startswith(prefix):
            raise BrokenPipeError("stdout gone")
        real_print(*args, **kwargs)

    return _print


async def test_stdout_stamp_names_environment_but_never_arbitrary_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both edges print the environment line and never the free text."""
    settings = _make_settings(
        MODULO_ENV="staging",
        ALERT_CONTEXT="runbook: https://example.com/runbook\npage the on-call",
    )
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = _unhealthy()
    await _tick(observer, sender, store, settings, clock)
    await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 1

    alert_stamp = capsys.readouterr().out
    assert "[health-alert] ALERT" in alert_stamp
    assert "Environment: staging" in alert_stamp
    assert "runbook: https://example.com/runbook" not in alert_stamp

    observer.observation = _healthy()
    await _tick(observer, sender, store, settings, clock)
    await _tick(observer, sender, store, settings, clock)
    assert len(sender.sent) == 2

    recovery_stamp = capsys.readouterr().out
    assert "[health-alert] RECOVERY" in recovery_stamp
    assert "Environment: staging" in recovery_stamp
    assert "runbook: https://example.com/runbook" not in recovery_stamp


async def test_print_failure_never_skips_send_or_state_commit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stamp prints AFTER the send but BEFORE the dedup state is
    committed - a print failure must be swallowed (with a log) so neither the
    send nor the commit is lost (a lost commit would re-send a DUPLICATE alert
    on the next confirmed tick)."""
    settings = _make_settings(MODULO_ENV="staging")
    observer = _FakeObserver()
    sender = _FakeSender()
    store = _FakeRedis()
    clock = {"now": 1_000_000.0}

    observer.observation = _unhealthy()
    await _tick(observer, sender, store, settings, clock)  # pending, no send yet

    with (
        patch("builtins.print", new=_failing_stamp_print("[health-alert]")),
        caplog.at_level(logging.WARNING, logger="modulo.core.health_alerts"),
    ):
        result = await _tick(observer, sender, store, settings, clock)

    assert result["action"] == "alert"
    assert len(sender.sent) == 1
    persisted = json.loads(store.data[ha.STATE_KEY])
    assert persisted["notified"] == "unhealthy"
    assert "health_alerts.stamp_print_failed" in caplog.text

    observer.observation = _healthy()
    await _tick(observer, sender, store, settings, clock)  # pending, no send yet

    caplog.clear()
    with (
        patch("builtins.print", new=_failing_stamp_print("[health-alert]")),
        caplog.at_level(logging.WARNING, logger="modulo.core.health_alerts"),
    ):
        result = await _tick(observer, sender, store, settings, clock)

    assert result["action"] == "recovery"
    assert len(sender.sent) == 2
    persisted = json.loads(store.data[ha.STATE_KEY])
    assert persisted["notified"] == "healthy"
    assert "health_alerts.stamp_print_failed" in caplog.text
