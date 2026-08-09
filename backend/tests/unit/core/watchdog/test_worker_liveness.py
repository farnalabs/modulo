"""Unit tests for the in-process worker-liveness watchdog (FAR-121).

The watchdog reads SAQ worker liveness directly from Redis (worker_info stats
zset + system-cron heartbeats) and POSTs a Slack-compatible webhook when every
worker is dead. No Docker, no real Redis — a fake in-memory redis double
covers the keys it touches, and the webhook HTTP client is mocked.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

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
        "watchdog_alert_cooldown_seconds": 900,
        "SAQ_RUNS_QUEUE": "runs",
    }
    base.update(overrides)
    return Settings(**base)


class _FakeWatchdogRedis:
    """In-memory redis double covering the keys the watchdog touches."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._zscores: dict[str, dict[str, float]] = {}
        self._fail_reads = False

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

    async def keys(self, pattern: str) -> list[str]:
        self._raise_if_failing()
        prefix = pattern.split("*")[0]
        return [k for k in self._data if k.startswith(prefix)]

    async def get(self, key: str) -> str | None:
        self._raise_if_failing()
        return self._data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._raise_if_failing()
        self._data[key] = value

    async def exists(self, key: str) -> bool:
        return key in self._data

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
            patch.object(wl, "_post_webhook", post),
            pytest.raises(asyncio.CancelledError),
        ):
            await wl.run_worker_liveness_watchdog(settings)

        assert sleeps["n"] == 1  # the loop ticked before cancelling
        post.assert_not_awaited()
        assert wl._WATCHDOG_HEARTBEAT_KEY in fake._data
        assert float(fake._data[wl._WATCHDOG_HEARTBEAT_KEY]) <= time.time()

    async def test_all_workers_dead_alerts_once_then_cooldown_suppresses(self) -> None:
        fake = _FakeWatchdogRedis()
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")
        # Fleet has been dead for longer than the 180s stale threshold.
        dead_since = time.time() - 200

        post = AsyncMock()
        with patch.object(wl, "_post_webhook", post):
            state = await wl._evaluate_once(settings, fake, dead_since)

        assert state == dead_since  # still dead, timer keeps running
        post.assert_awaited_once()
        assert wl._ALERT_COOLDOWN_KEY in fake._data

        # Next tick within the cooldown window: suppressed.
        post2 = AsyncMock()
        with patch.object(wl, "_post_webhook", post2):
            state2 = await wl._evaluate_once(settings, fake, state)
        post2.assert_not_awaited()
        assert state2 == state

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
            patch.object(wl, "_post_webhook", post),
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
            await wl._post_webhook(settings, ["no live SAQ worker"])  # must not raise

        client.__aenter__.assert_awaited()

    async def test_webhook_posts_slack_compatible_payload(self) -> None:
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/services/T/X/B")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = SimpleNamespace(is_success=True, status_code=200)

        with patch.object(wl.httpx, "AsyncClient", return_value=client) as ctor:
            await wl._post_webhook(settings, ["no live SAQ worker"])

        ctor.assert_called_once_with(timeout=wl._WEBHOOK_TIMEOUT_SECONDS)
        call = client.post.await_args
        assert call.args[0] == "https://hooks.slack.com/services/T/X/B"
        body = json.loads(call.kwargs["content"])
        assert isinstance(body["text"], str)
        assert "worker-liveness" in body["text"]

    async def test_no_webhook_url_never_posts_but_still_ticks(self) -> None:
        fake = _FakeWatchdogRedis()
        settings = _make_settings(ALERT_WEBHOOK_URL=None)
        dead_since = time.time() - 200

        post = AsyncMock()
        with patch.object(wl, "_post_webhook", post):
            state = await wl._evaluate_once(settings, fake, dead_since)

        post.assert_not_awaited()
        assert state is not None  # still tracking the dead state
        assert wl._ALERT_COOLDOWN_KEY not in fake._data

    async def test_recovery_resets_and_new_alert_can_fire_after_cooldown(self) -> None:
        fake = _FakeWatchdogRedis()
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")

        # 1. Alert fires while the fleet is dead past the threshold.
        post = AsyncMock()
        with patch.object(wl, "_post_webhook", post):
            dead_state = await wl._evaluate_once(settings, fake, time.time() - 200)
        post.assert_awaited_once()
        assert wl._ALERT_COOLDOWN_KEY in fake._data

        # 2. Recovery: live workers return -> state resets, no alert.
        fake.add_live_worker("runs")
        fake.add_live_worker("system")
        fake.set_cron_heartbeat()
        post2 = AsyncMock()
        with patch.object(wl, "_post_webhook", post2):
            recovered = await wl._evaluate_once(settings, fake, dead_state)
        post2.assert_not_awaited()
        assert recovered is None

        # 3. Second death within the cooldown window is suppressed.
        fake.clear_workers("runs")
        fake.clear_workers("system")
        fake.set_cron_heartbeat(age_seconds=600)
        post3 = AsyncMock()
        with patch.object(wl, "_post_webhook", post3):
            state2 = await wl._evaluate_once(settings, fake, time.time() - 200)
        post3.assert_not_awaited()

        # 4. Cooldown expires (TTL) -> a new alert can fire again.
        fake._data.pop(wl._ALERT_COOLDOWN_KEY, None)
        post4 = AsyncMock()
        with patch.object(wl, "_post_webhook", post4):
            await wl._evaluate_once(settings, fake, state2)
        post4.assert_awaited_once()

    async def test_cron_stale_alone_triggers_alert(self) -> None:
        fake = _FakeWatchdogRedis()
        fake.add_live_worker("runs")
        fake.add_live_worker("system")
        fake.set_cron_heartbeat(age_seconds=600)  # workers alive, cron dead
        settings = _make_settings(ALERT_WEBHOOK_URL="https://hooks.slack.com/webhook")

        post = AsyncMock()
        with patch.object(wl, "_post_webhook", post):
            state = await wl._evaluate_once(settings, fake, None)

        post.assert_awaited_once()
        assert state is None  # workers never looked dead

    async def test_configured_queues_are_prefix_aware(self) -> None:
        settings = _make_settings(SAQ_RUNS_QUEUE="staging-runs")
        assert wl._configured_queues(settings) == ["staging-runs", "staging-system"]
