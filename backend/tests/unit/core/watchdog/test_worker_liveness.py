"""Unit tests for the in-process worker-liveness watchdog (FAR-121).

The watchdog reads SAQ worker liveness directly from Redis (worker_info stats
zset + system-cron heartbeats) and POSTs a Slack-compatible webhook when every
worker is dead. No Docker, no real Redis — a fake in-memory redis double
covers the keys it touches, and the webhook HTTP client is mocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from modulo.core.email_service import EmailSendingError
from modulo.core.watchdog import worker_liveness as wl
from modulo.settings import Settings


def _make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": "a" * 32,
        "fernet_key": "a" * 32,
        "modulo_admin_password": "test",
        "redis_url": "redis://localhost:6379/0",
        "watchdog_tick_seconds": 30,
        "watchdog_worker_stale_seconds": 180,
        "watchdog_alert_state_ttl_seconds": 7 * 24 * 3600,
        "SAQ_RUNS_QUEUE": "runs",
    }
    base.update(overrides)
    return Settings(**base)


def _async_iter(values: list[str]) -> AsyncIterator[str]:
    """An async iterator over *values* (for mocking ``SCAN`` key discovery)."""

    async def _gen() -> AsyncIterator[str]:
        for value in values:
            yield value

    return _gen()


class _FakeWatchdogRedis:
    """In-memory redis double covering the keys the watchdog touches."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._zscores: dict[str, dict[str, float]] = {}
        self._fail_reads = False
        self._set_opts: dict[str, dict[str, Any]] = {}

    def set_fail_reads(self, fail: bool) -> None:
        self._fail_reads = fail

    def add_live_worker(self, queue: str, worker_id: str = "w1") -> None:
        """Insert a live worker_info entry (expiry score far in the future, ms)."""
        key = f"saq:{queue}:worker_info:{worker_id}"
        self._zscores.setdefault(f"saq:{queue}:stats", {})[key] = time.time() * 1000 + 90_000

    def clear_workers(self, queue: str) -> None:
        self._zscores[f"saq:{queue}:stats"] = {}

    def set_cron_heartbeat(self, age_seconds: float = 10) -> None:
        self._data["saq:cron:heartbeat:fire_due_triggers:m1"] = str(int(time.time() - age_seconds))

    def clear_cron_heartbeats(self) -> None:
        self._data = {k: v for k, v in self._data.items() if not k.startswith("saq:cron:heartbeat:")}

    def _raise_if_failing(self) -> None:
        if self._fail_reads:
            raise RuntimeError("redis down")

    async def zrangebyscore(self, key: str, _min: Any, _max: Any) -> list[str]:
        self._raise_if_failing()
        return [m for m, score in self._zscores.get(key, {}).items() if score >= _min]

    async def scan_iter(self, match: str | None = None, count: int = 10) -> AsyncIterator[str]:
        self._raise_if_failing()
        prefix = (match or "").split("*")[0]
        for k in self._data:
            if k.startswith(prefix):
                yield k

    async def keys(self, pattern: str) -> list[str]:
        self._raise_if_failing()
        prefix = pattern.split("*", maxsplit=1)[0]
        return [k for k in self._data if k.startswith(prefix)]

    async def get(self, key: str) -> str | None:
        self._raise_if_failing()
        return self._data.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        self._raise_if_failing()
        if nx and key in self._data:
            return None  # SET NX: key already exists -> not set
        self._data[key] = value
        self._set_opts[key] = {"ex": ex}
        return True

    async def exists(self, key: str) -> bool:
        return key in self._data

    async def getdel(self, key: str) -> str | None:
        self._raise_if_failing()
        return self._data.pop(key, None)

    async def delete(self, key: str) -> int:
        self._raise_if_failing()
        return 1 if self._data.pop(key, None) is not None else 0

    async def aclose(self) -> None:
        return None


class TestWorkerLivenessWatchdog:
    async def test_live_workers_no_alert_heartbeat_written(self) -> None:
        fake = _FakeWatchdogRedis()
        fake.add_live_worker("runs")
        fake.add_live_worker("system")
        fake.set_cron_heartbeat()
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")

        post = AsyncMock()
        sleeps = {"n": 0}

        async def _stop(_secs: float) -> None:
            sleeps["n"] += 1
            raise asyncio.CancelledError

        with (
            patch.object(wl.aioredis.Redis, "from_url", return_value=fake),
            patch.object(wl.asyncio, "sleep", side_effect=_stop),
            patch.object(wl, "_send_alerts", post),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl.run_worker_liveness_watchdog(settings)

        assert sleeps["n"] == 1  # the loop ticked before cancelling
        post.assert_not_awaited()
        assert wl._WATCHDOG_HEARTBEAT_KEY in fake._data
        assert float(fake._data[wl._WATCHDOG_HEARTBEAT_KEY]) <= time.time()

    async def test_all_workers_dead_alerts_once_then_silent_while_active(self) -> None:
        fake = _FakeWatchdogRedis()
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        # Fleet has been dead for longer than the 180s stale threshold.
        dead_since = time.time() - 200

        post = AsyncMock()
        with patch.object(wl, "_send_alerts", post):
            state = await wl._evaluate_once(settings, fake, dead_since)

        assert state == dead_since  # still dead, timer keeps running
        post.assert_awaited_once()
        assert wl._ALERT_STATE_KEY in fake._data
        # The stored state carries the conditions for the recovery email.
        stored = json.loads(fake._data[wl._ALERT_STATE_KEY])
        assert stored["conditions"]
        assert "started_at" in stored
        assert fake._set_opts[wl._ALERT_STATE_KEY]["ex"] == settings.watchdog_alert_state_ttl_seconds

        # Next tick while the incident is STILL active: no repeat alert.
        post2 = AsyncMock()
        with patch.object(wl, "_send_alerts", post2):
            state2 = await wl._evaluate_once(settings, fake, state)
        post2.assert_not_awaited()
        assert state2 == state
        assert wl._ALERT_STATE_KEY in fake._data  # state persists until recovery

    async def test_redis_read_failure_fails_open_and_loop_continues(self) -> None:
        fake = _FakeWatchdogRedis()
        fake.set_fail_reads(True)
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")

        post = AsyncMock()
        sleeps = {"n": 0}

        async def _stop(_secs: float) -> None:
            sleeps["n"] += 1
            if sleeps["n"] >= 3:
                raise asyncio.CancelledError

        with (
            patch.object(wl.aioredis.Redis, "from_url", return_value=fake),
            patch.object(wl.asyncio, "sleep", side_effect=_stop),
            patch.object(wl, "_send_alerts", post),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl.run_worker_liveness_watchdog(settings)

        # Three ticks ran (loop continued past the read failures) and never
        # alerted — death could not be confirmed while Redis reads failed.
        assert sleeps["n"] == 3
        post.assert_not_awaited()

    async def test_webhook_post_failure_is_caught(self) -> None:
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = httpx.ConnectError("boom")

        with patch.object(wl.httpx, "AsyncClient", return_value=client):
            await wl._post_generic_webhook(settings, wl._alert_text(["no live SAQ worker"]))

        client.__aenter__.assert_awaited()

    async def test_webhook_posts_slack_compatible_payload(self) -> None:
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/services/T/X/B")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(is_success=True, status_code=200)

        with patch.object(wl.httpx, "AsyncClient", return_value=client) as ctor:
            await wl._post_generic_webhook(settings, wl._alert_text(["no live SAQ worker"]))

        ctor.assert_called_once_with(timeout=wl._WEBHOOK_TIMEOUT_SECONDS)
        call = client.post.await_args
        assert call.args[0] == "https://hooks.slack.com/services/T/X/B"
        body = json.loads(call.kwargs["content"])
        assert set(body) == {"text"}
        assert "worker-liveness" in body["text"]

    async def test_no_channel_configured_never_alerts_but_still_ticks(self) -> None:
        fake = _FakeWatchdogRedis()
        settings = _make_settings()  # no generic/Teams webhook, no email
        dead_since = time.time() - 200

        send_alerts = AsyncMock()
        with patch.object(wl, "_send_alerts", send_alerts):
            state = await wl._evaluate_once(settings, fake, dead_since)

        send_alerts.assert_not_awaited()
        assert state is not None  # still tracking the dead state
        assert wl._ALERT_STATE_KEY not in fake._data

    async def test_channel_configured_via_teams_or_email_fires_alert(self) -> None:
        fake = _FakeWatchdogRedis()

        for settings in (
            _make_settings(ALERT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/t"),
            _make_settings(ALERT_EMAIL_TO="ops@example.com", smtp_host="smtp.example.com"),
        ):
            send_alerts = AsyncMock()
            with patch.object(wl, "_send_alerts", send_alerts):
                await wl._evaluate_once(settings, fake, time.time() - 200)
            send_alerts.assert_awaited_once()
            assert wl._ALERT_STATE_KEY in fake._data
            fake._data.pop(wl._ALERT_STATE_KEY, None)

    async def test_recovery_sends_all_clear_then_fresh_alert_can_fire(self) -> None:
        fake = _FakeWatchdogRedis()
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")

        # 1. Alert fires while the fleet is dead past the threshold.
        post = AsyncMock()
        with patch.object(wl, "_send_alerts", post):
            dead_state = await wl._evaluate_once(settings, fake, time.time() - 200)
        post.assert_awaited_once()
        assert wl._ALERT_STATE_KEY in fake._data

        # 2. Recovery: live workers return -> ONE recovery email, state cleared.
        fake.add_live_worker("runs")
        fake.add_live_worker("system")
        fake.set_cron_heartbeat()
        post2 = AsyncMock()
        with patch.object(wl, "_send_alerts", post2):
            recovered = await wl._evaluate_once(settings, fake, dead_state)
        assert recovered is None
        post2.assert_awaited_once()
        # The recovery fan-out carried the prior incident state.
        assert post2.await_args.kwargs["recovery_state"] is not None
        assert post2.await_args.kwargs["recovery_state"]["conditions"]
        assert wl._ALERT_STATE_KEY not in fake._data

        # 3. Still healthy on the next tick: no repeat recovery email.
        post3 = AsyncMock()
        with patch.object(wl, "_send_alerts", post3):
            still_healthy = await wl._evaluate_once(settings, fake, None)
        post3.assert_not_awaited()
        assert still_healthy is None

        # 4. A NEW incident (second death) fires a fresh alert again.
        fake.clear_workers("runs")
        fake.clear_workers("system")
        fake.set_cron_heartbeat(age_seconds=600)
        post4 = AsyncMock()
        with patch.object(wl, "_send_alerts", post4):
            state2 = await wl._evaluate_once(settings, fake, time.time() - 200)
        post4.assert_awaited_once()
        assert state2 is not None
        assert wl._ALERT_STATE_KEY in fake._data

    async def test_cron_stale_alone_triggers_alert(self) -> None:
        fake = _FakeWatchdogRedis()
        fake.add_live_worker("runs")
        fake.add_live_worker("system")
        fake.set_cron_heartbeat(age_seconds=600)  # workers alive, cron dead
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")

        post = AsyncMock()
        with patch.object(wl, "_send_alerts", post):
            state = await wl._evaluate_once(settings, fake, None)

        post.assert_awaited_once()
        assert state is None  # workers never looked dead

    def test_configured_queues_are_prefix_aware(self) -> None:
        settings = _make_settings(SAQ_RUNS_QUEUE="staging-runs")
        assert wl._configured_queues(settings) == ["staging-runs", "staging-system"]


# ---------------------------------------------------------------------------
# _cron_heartbeat_fresh
# ---------------------------------------------------------------------------


async def test_cron_heartbeat_fresh_true_when_any_key_fresh() -> None:
    """A single fresh heartbeat among stale keys must read as fresh (fleet-wide)."""
    fake = _FakeWatchdogRedis()
    fake._data["saq:cron:heartbeat:fire_due_triggers:m1"] = str(int(time.time() - 600))
    fake._data["saq:cron:heartbeat:fire_due_triggers:m2"] = str(int(time.time() - 5))
    assert await wl._cron_heartbeat_fresh(fake) is True


async def test_cron_heartbeat_fresh_false_when_all_stale() -> None:
    fake = _FakeWatchdogRedis()
    fake._data["saq:cron:heartbeat:fire_due_triggers:m1"] = str(int(time.time() - 600))
    fake._data["saq:cron:heartbeat:fire_due_triggers:m2"] = str(int(time.time() - 300))
    assert await wl._cron_heartbeat_fresh(fake) is False


async def test_cron_heartbeat_fresh_false_when_no_heartbeats() -> None:
    assert await wl._cron_heartbeat_fresh(_FakeWatchdogRedis()) is False


async def test_cron_heartbeat_fresh_skips_missing_and_corrupt_values() -> None:
    """Missing and non-numeric heartbeat values are skipped, not fatal."""
    redis = AsyncMock()

    async def _get(key: str) -> str | None:
        if key == "k1":
            return None
        if key == "k2":
            return "not-a-number"
        return str(int(time.time() - 5))  # k3: fresh

    redis.scan_iter = MagicMock(side_effect=lambda *args, **kwargs: _async_iter(["k1", "k2", "k3"]))
    redis.get.side_effect = _get
    assert await wl._cron_heartbeat_fresh(redis) is True


async def test_cron_heartbeat_fresh_false_when_no_value_is_fresh() -> None:
    redis = AsyncMock()

    async def _get(key: str) -> str | None:
        if key == "k1":
            return None
        return str(int(time.time() - 600))  # k2: stale

    redis.scan_iter = MagicMock(side_effect=lambda *args, **kwargs: _async_iter(["k1", "k2"]))
    redis.get.side_effect = _get
    assert await wl._cron_heartbeat_fresh(redis) is False


# ---------------------------------------------------------------------------
# _evaluate_once — fail-open and pre-threshold paths
# ---------------------------------------------------------------------------


async def test_cron_read_failure_fails_open_without_alert() -> None:
    """A Redis error reading cron heartbeats must not alert (fail-open)."""
    fake = _FakeWatchdogRedis()
    fake.add_live_worker("runs")
    settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
    post = AsyncMock()

    with (
        patch.object(fake, "scan_iter", side_effect=RuntimeError("redis down")),
        patch.object(wl, "_send_alerts", post),
    ):
        state = await wl._evaluate_once(settings, fake, None)

    assert state is None  # workers were live the whole time
    post.assert_not_awaited()


async def test_workers_dead_below_stale_threshold_does_not_alert() -> None:
    """Death must be sustained past the stale threshold before alerting."""
    fake = _FakeWatchdogRedis()
    fake.set_cron_heartbeat(age_seconds=5)  # cron fresh -> no cron condition
    settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
    post = AsyncMock()
    dead_since = time.time() - 10  # far below the 180s stale threshold

    with patch.object(wl, "_send_alerts", post):
        state = await wl._evaluate_once(settings, fake, dead_since)

    assert state == dead_since  # still tracking the dead window
    post.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cooldown fence failure paths
# ---------------------------------------------------------------------------


async def test_claim_alert_write_failure_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    """A Redis write failure claiming the alert edge must NOT suppress the alert."""
    redis = AsyncMock()
    redis.set.side_effect = ConnectionError("redis down")
    settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")

    claimed = await wl._claim_alert(redis, settings, ["no live SAQ worker"])

    assert claimed is True  # fail-open: alert sent rather than lost
    assert "watchdog.alert_claim_failed" in caplog.text


async def test_claim_alert_nx_true_only_for_first_claimant() -> None:
    """SET NX returns True only for the machine that writes the state."""
    redis = AsyncMock()
    redis.set.side_effect = [True, False]  # first machine wins, second loses
    settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")

    assert await wl._claim_alert(redis, settings, ["no live SAQ worker"]) is True
    assert await wl._claim_alert(redis, settings, ["no live SAQ worker"]) is False
    call = redis.set.await_args
    assert call.kwargs.get("nx") is True
    assert call.kwargs.get("ex") == settings.watchdog_alert_state_ttl_seconds
    assert call.args[0] == wl._ALERT_STATE_KEY


async def test_claim_recovery_getdel_returns_state_only_to_winner() -> None:
    """GETDEL returns the incident state only to the machine that clears it."""
    redis = AsyncMock()
    redis.getdel.side_effect = [
        json.dumps({"conditions": ["no live SAQ worker"], "started_at": time.time()}),
        None,  # second machine sees no state
    ]

    first = await wl._claim_recovery(redis)
    second = await wl._claim_recovery(redis)

    assert first is not None
    assert first["conditions"]
    assert second is None
    assert redis.getdel.await_count == 2


async def test_claim_recovery_failure_returns_none_and_retries_next_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Redis error claiming recovery returns None (next tick retries)."""
    redis = AsyncMock()
    redis.getdel.side_effect = ConnectionError("redis down")

    assert await wl._claim_recovery(redis) is None
    assert "watchdog.recovery_claim_failed" in caplog.text


# ---------------------------------------------------------------------------
# _hostname
# ---------------------------------------------------------------------------


def test_hostname_prefers_fly_machine_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLY_MACHINE_ID", "fly-abc")
    monkeypatch.delenv("HOSTNAME", raising=False)
    assert wl._hostname() == "fly-abc"


def test_hostname_falls_back_to_hostname_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.setenv("HOSTNAME", "box-1")
    assert wl._hostname() == "box-1"


def test_hostname_defaults_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.delenv("HOSTNAME", raising=False)
    assert wl._hostname() == "unknown"


class TestMultiChannelAlertFanout:
    async def test_generic_webhook_only_posts_text_payload(self) -> None:
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/services/T/X/B")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(is_success=True, status_code=200)

        with patch.object(wl.httpx, "AsyncClient", return_value=client) as ctor:
            await wl._send_alerts(settings, ["no live SAQ worker"])

        ctor.assert_called_once_with(timeout=wl._WEBHOOK_TIMEOUT_SECONDS)
        call = client.post.await_args
        assert call.args[0] == "https://hooks.slack.com/services/T/X/B"
        body = json.loads(call.kwargs["content"])
        assert set(body) == {"text"}
        assert "no live SAQ worker" in body["text"]
        assert "worker-liveness" in body["text"]

    async def test_teams_webhook_only_posts_message_card(self) -> None:
        settings = _make_settings(ALERT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/abc")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(is_success=True, status_code=200)

        with patch.object(wl.httpx, "AsyncClient", return_value=client) as ctor:
            await wl._send_alerts(settings, ["no live SAQ worker"])

        ctor.assert_called_once_with(timeout=wl._WEBHOOK_TIMEOUT_SECONDS)
        call = client.post.await_args
        assert call.args[0] == "https://outlook.office.com/webhook/abc"
        body = json.loads(call.kwargs["content"])
        assert body["@type"] == "MessageCard"
        assert body["@context"] == "http://schema.org/extensions"
        assert "worker-liveness" in body["summary"]
        assert "worker-liveness" in body["title"]
        assert "no live SAQ worker" in body["text"]

    async def test_email_only_sends_via_to_thread(self) -> None:
        settings = _make_settings(
            ALERT_EMAIL_TO="ops@example.com, alice@example.com",
            smtp_host="smtp.example.com",
        )
        send = MagicMock(return_value=True)
        to_thread = AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs))

        with (
            patch.object(wl, "send_email", send),
            patch.object(wl.asyncio, "to_thread", to_thread),
        ):
            await wl._send_email_alert(settings, ["no live SAQ worker"])

        to_thread.assert_awaited_once()
        assert to_thread.await_args.args[0] is send
        send.assert_called_once()
        call = send.call_args
        assert call.args[1] == ["ops@example.com", "alice@example.com"]
        assert call.args[2] == "[Modulo Watchdog] Worker-liveness alert"
        assert "<li>no live SAQ worker</li>" in call.args[3]
        assert "Detected at" in call.args[4]

    async def test_multiple_channels_all_fire(self) -> None:
        settings = _make_settings(
            ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook",
            ALERT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/abc",
            ALERT_EMAIL_TO="ops@example.com",
            smtp_host="smtp.example.com",
        )
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(is_success=True, status_code=200)
        send = MagicMock(return_value=True)

        with (
            patch.object(wl.httpx, "AsyncClient", return_value=client),
            patch.object(wl, "send_email", send),
        ):
            await wl._send_alerts(settings, ["no live SAQ worker"])

        assert client.post.await_count == 2  # generic + Teams
        assert send.call_count == 1

    async def test_channel_isolation_email_failure_does_not_block_webhook(self) -> None:
        settings = _make_settings(
            ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook",
            ALERT_EMAIL_TO="ops@example.com",
            smtp_host="smtp.example.com",
        )
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(is_success=True, status_code=200)

        def _raise_send_error(*_args: Any, **_kwargs: Any) -> bool:
            raise EmailSendingError("smtp down")

        with (
            patch.object(wl.httpx, "AsyncClient", return_value=client),
            patch.object(wl, "send_email", side_effect=_raise_send_error),
        ):
            await wl._send_alerts(settings, ["no live SAQ worker"])  # must not raise

        client.post.assert_awaited_once()
        assert client.post.await_args.args[0] == "https://hooks.slack.com/webhook"

    def test_parse_alert_email_to(self) -> None:
        assert wl._parse_alert_email_to("a@b.com, c@d.com , ,e@f.com") == [
            "a@b.com",
            "c@d.com",
            "e@f.com",
        ]
        assert not wl._parse_alert_email_to("")
        assert not wl._parse_alert_email_to(None)
        assert not wl._parse_alert_email_to("  ,  ")

    async def test_email_skipped_when_recipient_list_empty(self) -> None:
        settings = _make_settings(ALERT_EMAIL_TO="  , ", smtp_host="smtp.example.com")
        send = MagicMock()
        with patch.object(wl, "send_email", send):
            await wl._send_alerts(settings, ["no live SAQ worker"])
        send.assert_not_called()

    async def test_email_skipped_when_smtp_not_configured(self) -> None:
        settings = _make_settings(ALERT_EMAIL_TO="ops@example.com")
        send = MagicMock()
        with patch.object(wl, "send_email", send):
            await wl._send_alerts(settings, ["no live SAQ worker"])  # must not raise
        send.assert_not_called()

    async def test_send_email_alert_no_smtp_host_returns_without_sending(self) -> None:
        settings = _make_settings(ALERT_EMAIL_TO="ops@example.com")
        send = MagicMock()
        with patch.object(wl, "send_email", send):
            await wl._send_email_alert(settings, ["no live SAQ worker"])
        send.assert_not_called()


# ---------------------------------------------------------------------------
# Edge-triggered alert/recovery (postmortem follow-up 2026-08-10)
# ---------------------------------------------------------------------------


class TestEdgeTriggeredAlertRecovery:
    async def test_recovery_email_uses_recovered_subject(self) -> None:
        """The recovery email subject says recovered, not alert."""
        settings = _make_settings(
            ALERT_EMAIL_TO="ops@example.com",
            smtp_host="smtp.example.com",
        )
        send = MagicMock(return_value=True)
        to_thread = AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs))

        with (
            patch.object(wl, "send_email", send),
            patch.object(wl.asyncio, "to_thread", to_thread),
        ):
            await wl._send_email_alert(
                settings,
                [],
                recovery_state={"conditions": ["no live SAQ worker"], "started_at": time.time() - 60},
            )

        send.assert_called_once()
        assert send.call_args.args[2] == "[Modulo Watchdog] Worker-liveness recovered"
        assert "recovered" in send.call_args.args[4]
        assert "no live SAQ worker" in send.call_args.args[3]

    def test_recovery_text_includes_duration_and_prior_conditions(self) -> None:
        state = {"conditions": ["no live SAQ worker"], "started_at": time.time() - 120}
        text = wl._recovery_text(state)
        assert "worker-liveness recovered" in text
        assert "no live SAQ worker" in text
        assert "120s" in text

    async def test_never_alerted_so_no_recovery_email(self) -> None:
        """Healthy state with no active incident never sends a recovery email."""
        fake = _FakeWatchdogRedis()
        fake.add_live_worker("runs")
        fake.add_live_worker("system")
        fake.set_cron_heartbeat()
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")

        post = AsyncMock()
        with patch.object(wl, "_send_alerts", post):
            state = await wl._evaluate_once(settings, fake, None)

        post.assert_not_awaited()
        assert state is None
        assert wl._ALERT_STATE_KEY not in fake._data

    async def test_multiple_machines_send_exactly_one_alert_and_one_recovery(self) -> None:
        """Two app machines racing: SET NX + GETDEL guarantee single alert + single recovery."""
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")

        # Two fake Redis instances sharing NO state (simulate the race at the
        # Redis layer: the first SET NX wins; the second sees the key present).
        shared = _FakeWatchdogRedis()
        fake_a = shared
        fake_b = _FakeWatchdogRedis()
        fake_b._data = shared._data  # share the same backing dict for the state key
        fake_b._set_opts = shared._set_opts
        fake_b._zscores = shared._zscores

        dead_since = time.time() - 200
        post_a = AsyncMock()
        post_b = AsyncMock()
        with (
            patch.object(wl, "_send_alerts", post_a),
        ):
            await wl._evaluate_once(settings, fake_a, dead_since)
        with (
            patch.object(wl, "_send_alerts", post_b),
        ):
            await wl._evaluate_once(settings, fake_b, dead_since)

        # Exactly one alert email across both machines.
        assert post_a.await_count + post_b.await_count == 1
        assert wl._ALERT_STATE_KEY in shared._data

        # Recovery: both machines see live workers; exactly one recovery email.
        fake_a.add_live_worker("runs")
        fake_a.add_live_worker("system")
        fake_a.set_cron_heartbeat()
        post_a2 = AsyncMock()
        post_b2 = AsyncMock()
        with patch.object(wl, "_send_alerts", post_a2):
            await wl._evaluate_once(settings, fake_a, dead_since)
        with patch.object(wl, "_send_alerts", post_b2):
            await wl._evaluate_once(settings, fake_b, dead_since)

        assert post_a2.await_count + post_b2.await_count == 1
        assert wl._ALERT_STATE_KEY not in shared._data


# ---------------------------------------------------------------------------
# Webhook / email error paths
# ---------------------------------------------------------------------------


class TestWebhookErrorPaths:
    async def test_generic_webhook_no_url_logs_and_returns(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings()  # no webhook URL
        with caplog.at_level(logging.WARNING, logger="modulo.watchdog"):
            await wl._post_generic_webhook(settings, wl._alert_text(["no live SAQ worker"]))
        assert "watchdog.webhook_no_url" in caplog.text

    async def test_teams_webhook_no_url_logs_and_returns(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings()  # no Teams webhook URL
        with caplog.at_level(logging.WARNING, logger="modulo.watchdog"):
            await wl._post_teams_webhook(settings, wl._alert_text(["no live SAQ worker"]))
        assert "watchdog.teams_webhook_no_url" in caplog.text

    async def test_generic_webhook_http_error_status_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(is_success=False, status_code=500)

        with (
            patch.object(wl.httpx, "AsyncClient", return_value=client),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._post_generic_webhook(settings, wl._alert_text(["no live SAQ worker"]))

        assert "watchdog.webhook_http_error" in caplog.text
        assert "500" in caplog.text

    async def test_generic_webhook_posts_encoded_json_with_channel_headers(self) -> None:
        """The deduped POST contract: encoded JSON body + Content-Type + User-Agent."""
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(is_success=True, status_code=200)
        alert_text = wl._alert_text(["no live SAQ worker"])

        with patch.object(wl.httpx, "AsyncClient", return_value=client):
            await wl._post_generic_webhook(settings, alert_text)

        client.post.assert_awaited_once()
        call = client.post.await_args
        assert call.args
        assert call.args[0] == settings.alert_webhook_url
        body = json.loads(call.kwargs["content"])
        assert body == {"text": alert_text}
        assert call.kwargs["headers"]["Content-Type"] == "application/json"
        assert call.kwargs["headers"]["User-Agent"] == "Modulo-Watchdog/1.0"

    async def test_teams_webhook_http_error_status_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/t")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(is_success=False, status_code=503)

        with (
            patch.object(wl.httpx, "AsyncClient", return_value=client),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._post_teams_webhook(settings, wl._alert_text(["no live SAQ worker"]))

        assert "watchdog.teams_webhook_http_error" in caplog.text
        assert "503" in caplog.text

    async def test_teams_webhook_request_error_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/t")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = httpx.ConnectError("boom")

        with (
            patch.object(wl.httpx, "AsyncClient", return_value=client),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._post_teams_webhook(settings, wl._alert_text(["no live SAQ worker"]))

        assert "watchdog.teams_webhook_request_failed" in caplog.text

    async def test_generic_webhook_unknown_failure_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = RuntimeError("boom")

        with (
            patch.object(wl.httpx, "AsyncClient", return_value=client),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._post_generic_webhook(settings, wl._alert_text(["no live SAQ worker"]))

        assert "watchdog.webhook_unknown_failure" in caplog.text

    async def test_teams_webhook_unknown_failure_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/t")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = RuntimeError("boom")

        with (
            patch.object(wl.httpx, "AsyncClient", return_value=client),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._post_teams_webhook(settings, wl._alert_text(["no live SAQ worker"]))

        assert "watchdog.teams_webhook_unknown_failure" in caplog.text


class TestEmailErrorPaths:
    async def test_send_email_alert_email_send_failure_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_EMAIL_TO="ops@example.com", smtp_host="smtp.example.com")
        send = MagicMock(side_effect=EmailSendingError("smtp down"))
        to_thread = AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs))

        with (
            patch.object(wl, "send_email", send),
            patch.object(wl.asyncio, "to_thread", to_thread),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._send_email_alert(settings, ["no live SAQ worker"])

        assert "watchdog.email_send_failed" in caplog.text
        assert "smtp down" in caplog.text

    async def test_send_email_alert_unknown_failure_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_EMAIL_TO="ops@example.com", smtp_host="smtp.example.com")
        send = MagicMock(side_effect=RuntimeError("boom"))
        to_thread = AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs))

        with (
            patch.object(wl, "send_email", send),
            patch.object(wl.asyncio, "to_thread", to_thread),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._send_email_alert(settings, ["no live SAQ worker"])

        assert "watchdog.email_unknown_failure" in caplog.text

    async def test_send_email_alert_no_recipients_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(smtp_host="smtp.example.com")  # no recipients
        send = MagicMock()
        with (
            patch.object(wl, "send_email", send),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._send_email_alert(settings, ["no live SAQ worker"])
        send.assert_not_called()
        assert "watchdog.email_no_recipients" in caplog.text

    def test_recovery_text_without_started_at_omits_duration(self) -> None:
        text = wl._recovery_text({"conditions": ["no live SAQ worker"]})
        assert "for" not in text


class TestCancelledErrorReRaise:
    """A cancellation must propagate, never be swallowed by the fail-open handlers."""

    async def test_claim_alert_reraises_cancelled_error(self) -> None:
        redis = AsyncMock()
        redis.set.side_effect = asyncio.CancelledError()
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        with pytest.raises(asyncio.CancelledError):
            await wl._claim_alert(redis, settings, ["no live SAQ worker"])

    async def test_claim_recovery_reraises_cancelled_error(self) -> None:
        redis = AsyncMock()
        redis.getdel.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await wl._claim_recovery(redis)

    async def test_generic_webhook_reraises_cancelled_error(self) -> None:
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = asyncio.CancelledError()
        with (
            patch.object(wl.httpx, "AsyncClient", return_value=client),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl._post_generic_webhook(settings, wl._alert_text(["no live SAQ worker"]))

    async def test_teams_webhook_reraises_cancelled_error(self) -> None:
        settings = _make_settings(ALERT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/t")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.side_effect = asyncio.CancelledError()
        with (
            patch.object(wl.httpx, "AsyncClient", return_value=client),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl._post_teams_webhook(settings, wl._alert_text(["no live SAQ worker"]))

    async def test_send_email_alert_reraises_cancelled_error(self) -> None:
        settings = _make_settings(ALERT_EMAIL_TO="ops@example.com", smtp_host="smtp.example.com")

        async def _cancelled(*_args: Any, **_kwargs: Any) -> None:
            raise asyncio.CancelledError

        send = MagicMock(return_value=True)
        with (
            patch.object(wl, "send_email", send),
            patch.object(wl.asyncio, "to_thread", _cancelled),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl._send_email_alert(settings, ["no live SAQ worker"])


# ---------------------------------------------------------------------------
# Additional fail-open paths
# ---------------------------------------------------------------------------


class TestAdditionalFailOpenPaths:
    async def test_claim_recovery_corrupt_state_returns_none(self) -> None:
        redis = AsyncMock()
        redis.getdel.return_value = "not-json"
        assert await wl._claim_recovery(redis) is None

    async def test_send_alerts_generic_channel_unknown_failure_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        with (
            patch.object(wl, "_post_generic_webhook", AsyncMock(side_effect=RuntimeError("boom"))),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._send_alerts(settings, ["no live SAQ worker"])
        assert "watchdog.channel_generic_failed" in caplog.text

    async def test_send_alerts_teams_channel_unknown_failure_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/t")
        with (
            patch.object(wl, "_post_teams_webhook", AsyncMock(side_effect=RuntimeError("boom"))),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._send_alerts(settings, ["no live SAQ worker"])
        assert "watchdog.channel_teams_failed" in caplog.text

    async def test_send_alerts_email_channel_unknown_failure_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_EMAIL_TO="ops@example.com", smtp_host="smtp.example.com")
        with (
            patch.object(wl, "_send_email_alert", AsyncMock(side_effect=RuntimeError("boom"))),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            await wl._send_alerts(settings, ["no live SAQ worker"])
        assert "watchdog.channel_email_failed" in caplog.text

    async def test_worker_read_failure_fails_open_without_alert(self, caplog: pytest.LogCaptureFixture) -> None:
        """A Redis error reading worker liveness must not alert (fail-open)."""
        fake = _FakeWatchdogRedis()
        fake.add_live_worker("runs")
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        post = AsyncMock()

        with (
            patch.object(fake, "zrangebyscore", side_effect=RuntimeError("redis down")),
            patch.object(wl, "_send_alerts", post),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
        ):
            state = await wl._evaluate_once(settings, fake, None)

        assert state is None  # workers were live the whole time
        post.assert_not_awaited()
        assert "watchdog.worker_read_failed" in caplog.text

    async def test_workers_dead_first_detection_starts_timer(self) -> None:
        """The first tick after death starts the stale-timer rather than alerting."""
        fake = _FakeWatchdogRedis()
        fake.set_cron_heartbeat(age_seconds=5)  # cron fresh -> no cron condition
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        post = AsyncMock()

        with patch.object(wl, "_send_alerts", post):
            state = await wl._evaluate_once(settings, fake, None)

        assert state is not None  # all_dead_since now set, below the stale threshold
        post.assert_not_awaited()

    async def test_worker_read_cancelled_error_propagates(self) -> None:
        fake = _FakeWatchdogRedis()
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        with (
            patch.object(fake, "zrangebyscore", side_effect=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl._evaluate_once(settings, fake, None)

    async def test_cron_read_cancelled_error_propagates(self) -> None:
        fake = _FakeWatchdogRedis()
        fake.add_live_worker("runs")
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        with (
            patch.object(fake, "scan_iter", side_effect=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl._evaluate_once(settings, fake, None)

    async def test_watchdog_tick_failure_logs_and_continues(self, caplog: pytest.LogCaptureFixture) -> None:
        fake = _FakeWatchdogRedis()
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        sleeps = {"n": 0}

        async def _stop(_secs: float) -> None:
            sleeps["n"] += 1
            raise asyncio.CancelledError

        with (
            patch.object(wl.aioredis.Redis, "from_url", return_value=fake),
            patch.object(wl.asyncio, "sleep", side_effect=_stop),
            patch.object(wl, "_evaluate_once", AsyncMock(side_effect=RuntimeError("boom"))),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl.run_worker_liveness_watchdog(settings)

        assert sleeps["n"] == 1  # the loop ticked (and survived the failure) before cancelling
        assert "watchdog.tick_failed" in caplog.text

    async def test_send_alerts_generic_channel_cancelled_propagates(self) -> None:
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        with (
            patch.object(wl, "_post_generic_webhook", AsyncMock(side_effect=asyncio.CancelledError())),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl._send_alerts(settings, ["no live SAQ worker"])

    async def test_send_alerts_teams_channel_cancelled_propagates(self) -> None:
        settings = _make_settings(ALERT_TEAMS_WEBHOOK_URL="https://outlook.office.com/webhook/t")
        with (
            patch.object(wl, "_post_teams_webhook", AsyncMock(side_effect=asyncio.CancelledError())),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl._send_alerts(settings, ["no live SAQ worker"])

    async def test_send_alerts_email_channel_cancelled_propagates(self) -> None:
        settings = _make_settings(ALERT_EMAIL_TO="ops@example.com", smtp_host="smtp.example.com")
        with (
            patch.object(wl, "_send_email_alert", AsyncMock(side_effect=asyncio.CancelledError())),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl._send_alerts(settings, ["no live SAQ worker"])

    async def test_watchdog_loop_reraises_cancelled_error(self) -> None:
        fake = _FakeWatchdogRedis()
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        with (
            patch.object(wl.aioredis.Redis, "from_url", return_value=fake),
            patch.object(wl, "_evaluate_once", AsyncMock(side_effect=asyncio.CancelledError())),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl.run_worker_liveness_watchdog(settings)

    async def test_watchdog_redis_connect_failure_fails_open(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        sleeps = {"n": 0}

        async def _stop(_secs: float) -> None:
            sleeps["n"] += 1
            raise asyncio.CancelledError

        with (
            patch.object(wl.aioredis.Redis, "from_url", side_effect=ConnectionError("redis down")),
            patch.object(wl.asyncio, "sleep", side_effect=_stop),
            caplog.at_level(logging.WARNING, logger="modulo.watchdog"),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl.run_worker_liveness_watchdog(settings)

        assert sleeps["n"] == 1  # the loop survived the connect failure and kept ticking
        assert "watchdog.tick_failed" in caplog.text
