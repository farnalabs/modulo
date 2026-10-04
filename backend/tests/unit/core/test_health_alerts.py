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

import json
import logging
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
