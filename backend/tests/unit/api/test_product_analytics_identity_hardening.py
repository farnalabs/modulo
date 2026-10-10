"""Unit tests for product-analytics identity route hardening.

Two regressions are pinned here:

1. The in-memory rotation rate limiter must keep its tracked-client map
   *hard bounded*. The pre-fix code pruned only the *current* key's expired
   timestamps and never removed keys, so entries for one-off client IPs
   accumulated forever (a slow memory leak). Stale keys must be swept, and the
   map must stay at or under ``_MAX_TRACKED_CLIENTS`` even when every tracked
   client is still in-window (the sweep alone cannot evict those).
2. ``_get_last_sequence`` must fail CLOSED on a corrupt stored value. The
   pre-fix code called ``int(entry.value)`` directly, so a non-numeric stored
   sequence escaped as a generic 500 (and ``int()`` would silently coerce a
   JSON ``true``/``2.9`` into a usable integer). A corrupt value must produce a
   clear, specific error and the rotation must not proceed.

These are pure unit tests — ``get_config`` and the DB session are mocked, so
no database is required.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import modulo.api.routes.product_analytics_identity as pa


def _fresh_limiter() -> dict[str, list[float]]:
    """Return an empty rate-limiter map (typed as the module-level one)."""
    return {}


def _fake_session() -> AsyncMock:
    """Return an AsyncMock session whose ``begin()`` is an async context manager."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


class TestRotationRateLimiterBoundedState:
    def test_stale_clients_are_swept_and_key_set_stays_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Many stale keys must be evicted so the tracked set does not grow forever.

        Pre-fix this test cannot even run (``_MAX_TRACKED_CLIENTS`` does not
        exist, so ``monkeypatch.setattr`` raises); post-fix the sweep drops
        every key with no in-window timestamp, leaving only the live client.
        """
        monkeypatch.setattr(pa, "_rotation_timestamps", _fresh_limiter())
        monkeypatch.setattr(pa, "_MAX_TRACKED_CLIENTS", 10)
        stale_ts = time.time() - pa._ROTATION_WINDOW - 60.0
        for i in range(50):
            pa._rotation_timestamps[f"stale-{i}"] = [stale_ts]

        pa._check_rotation_rate_limit("fresh-client")

        assert len(pa._rotation_timestamps) == 1
        assert "stale-0" not in pa._rotation_timestamps
        assert "stale-49" not in pa._rotation_timestamps
        assert "fresh-client" in pa._rotation_timestamps

    def test_in_window_client_is_preserved_during_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A client with a live timestamp must not be evicted when the sweep frees room."""
        monkeypatch.setattr(pa, "_rotation_timestamps", _fresh_limiter())
        monkeypatch.setattr(pa, "_MAX_TRACKED_CLIENTS", 2)
        now = time.time()
        pa._rotation_timestamps["active"] = [now]
        pa._rotation_timestamps["stale"] = [now - pa._ROTATION_WINDOW - 60.0]

        pa._check_rotation_rate_limit("fresh")

        assert "active" in pa._rotation_timestamps
        assert "stale" not in pa._rotation_timestamps
        assert "fresh" in pa._rotation_timestamps

    def test_in_window_clients_are_hard_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The map must stay at/below the cap even when every tracked client is in-window.

        This is the case the sweep alone cannot handle: all keys are live, so
        the sweep frees nothing. The hard eviction backstop must still keep the
        map bounded.
        """
        monkeypatch.setattr(pa, "_rotation_timestamps", _fresh_limiter())
        monkeypatch.setattr(pa, "_MAX_TRACKED_CLIENTS", 3)
        now = time.time()
        for i in range(3):
            pa._rotation_timestamps[f"live-{i}"] = [now]

        pa._check_rotation_rate_limit("new-client")

        assert len(pa._rotation_timestamps) == 3
        assert "new-client" in pa._rotation_timestamps

    def test_eviction_spares_the_most_recently_used_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Eviction must drop the least-recently-used key, not the client that last rotated."""
        monkeypatch.setattr(pa, "_rotation_timestamps", _fresh_limiter())
        monkeypatch.setattr(pa, "_MAX_TRACKED_CLIENTS", 2)
        now = time.time()
        pa._rotation_timestamps["a"] = [now]
        pa._rotation_timestamps["b"] = [now]
        # Touch 'a' so it becomes the most-recently-used key (order: b, a).
        pa._check_rotation_rate_limit("a")

        pa._check_rotation_rate_limit("c")

        assert "a" in pa._rotation_timestamps
        assert "b" not in pa._rotation_timestamps
        assert "c" in pa._rotation_timestamps

    def test_cap_of_one_still_admits_a_new_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cap of 1 must hard-evict the single live key to admit a new client."""
        monkeypatch.setattr(pa, "_rotation_timestamps", _fresh_limiter())
        monkeypatch.setattr(pa, "_MAX_TRACKED_CLIENTS", 1)
        pa._rotation_timestamps["live"] = [time.time()]

        pa._check_rotation_rate_limit("new")

        assert len(pa._rotation_timestamps) == 1
        assert "new" in pa._rotation_timestamps
        assert "live" not in pa._rotation_timestamps

    def test_eviction_does_not_weaken_a_limited_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Admitting a new client at cap must not reset an actively-limited client's window."""
        monkeypatch.setattr(pa, "_rotation_timestamps", _fresh_limiter())
        monkeypatch.setattr(pa, "_MAX_TRACKED_CLIENTS", 2)
        now = time.time()
        pa._rotation_timestamps["limited"] = [now] * pa._MAX_ROTATIONS
        pa._rotation_timestamps["idle"] = [now]
        # Touching the limited client marks it most-recently-used (and raises 429).
        with pytest.raises(HTTPException) as first:
            pa._check_rotation_rate_limit("limited")
        assert first.value.status_code == 429

        pa._check_rotation_rate_limit("new")

        assert "limited" in pa._rotation_timestamps
        assert "idle" not in pa._rotation_timestamps
        with pytest.raises(HTTPException) as second:
            pa._check_rotation_rate_limit("limited")
        assert second.value.status_code == 429

    def test_empty_key_is_evicted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A key holding an empty list carries no rate-limit information and is swept."""
        monkeypatch.setattr(pa, "_rotation_timestamps", _fresh_limiter())
        monkeypatch.setattr(pa, "_MAX_TRACKED_CLIENTS", 1)
        pa._rotation_timestamps["empty"] = []

        pa._check_rotation_rate_limit("fresh")

        assert "empty" not in pa._rotation_timestamps
        assert "fresh" in pa._rotation_timestamps

    def test_rate_limit_still_trips_after_max_rotations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The hardening must not weaken the limiter itself."""
        monkeypatch.setattr(pa, "_rotation_timestamps", _fresh_limiter())
        for _ in range(pa._MAX_ROTATIONS):
            pa._check_rotation_rate_limit("client")

        with pytest.raises(HTTPException) as exc:
            pa._check_rotation_rate_limit("client")

        assert exc.value.status_code == 429


class TestGetLastSequenceFailsClosed:
    async def test_corrupt_value_raises_specific_error(self) -> None:
        """A non-numeric stored value must raise a specific, actionable error.

        FAILS pre-fix: ``int("not-an-integer")`` raises ``ValueError``, not an
        ``HTTPException``.
        """
        session = MagicMock()
        entry = MagicMock()
        entry.value = "not-an-integer"
        with (
            patch.object(pa, "get_config", AsyncMock(return_value=entry)),
            pytest.raises(HTTPException) as exc,
        ):
            await pa._get_last_sequence(session, "instance-1")

        assert exc.value.status_code == 500
        assert "sequence" in exc.value.detail.lower()
        assert pa._SEQUENCE_KEY_PREFIX in exc.value.detail

    async def test_corrupt_value_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """The corrupt config must be logged so it is diagnosable."""
        session = MagicMock()
        entry = MagicMock()
        entry.value = "not-an-integer"
        with (
            caplog.at_level(logging.ERROR, logger=pa.__name__),
            patch.object(pa, "get_config", AsyncMock(return_value=entry)),
            pytest.raises(HTTPException),
        ):
            await pa._get_last_sequence(session, "instance-1")

        assert "corrupt stored sequence" in caplog.text

    async def test_none_value_raises_specific_error(self) -> None:
        """A ``None`` stored value (JSON null) must also fail closed."""
        session = MagicMock()
        entry = MagicMock()
        entry.value = None
        with (
            patch.object(pa, "get_config", AsyncMock(return_value=entry)),
            pytest.raises(HTTPException) as exc,
        ):
            await pa._get_last_sequence(session, "instance-1")

        assert exc.value.status_code == 500

    async def test_bool_value_raises_specific_error(self) -> None:
        """A JSON boolean is not a sequence number and must fail closed."""
        session = MagicMock()
        entry = MagicMock()
        entry.value = True
        with (
            patch.object(pa, "get_config", AsyncMock(return_value=entry)),
            pytest.raises(HTTPException) as exc,
        ):
            await pa._get_last_sequence(session, "instance-1")

        assert exc.value.status_code == 500

    async def test_float_value_raises_specific_error(self) -> None:
        """A fractional stored value must fail closed rather than be truncated."""
        session = MagicMock()
        entry = MagicMock()
        entry.value = 2.9
        with (
            patch.object(pa, "get_config", AsyncMock(return_value=entry)),
            pytest.raises(HTTPException) as exc,
        ):
            await pa._get_last_sequence(session, "instance-1")

        assert exc.value.status_code == 500

    async def test_missing_entry_returns_zero(self) -> None:
        """No stored sequence (first rotation) legitimately starts from 0."""
        session = MagicMock()
        with patch.object(pa, "get_config", AsyncMock(return_value=None)):
            assert await pa._get_last_sequence(session, "instance-1") == 0

    async def test_valid_numeric_value_is_returned(self) -> None:
        """A healthy integer stored value must be returned unchanged."""
        session = MagicMock()
        entry = MagicMock()
        entry.value = 7
        with patch.object(pa, "get_config", AsyncMock(return_value=entry)):
            assert await pa._get_last_sequence(session, "instance-1") == 7


class TestRotationAbortsOnCorruptSequence:
    async def test_rotation_does_not_call_rotate_secret_when_sequence_corrupt(self) -> None:
        """A corrupt stored sequence must abort the rotation before it mints a new secret.

        FAILS pre-fix on the assertion that the error names the sequence config:
        the pre-fix ``ValueError`` is swallowed by the route's broad ``except``
        and becomes a generic 500.
        """
        session = _fake_session()
        request = MagicMock()
        request.client.host = "10.0.0.1"
        req = pa.RotateRequest(old_secret="s", timestamp=1.0, sequence=2, hmac_digest="d")
        corrupt_entry = MagicMock()
        corrupt_entry.value = "NaN"
        rotate_mock = AsyncMock(return_value="new-secret")
        with (
            patch.object(pa, "_check_rotation_rate_limit"),
            patch.object(pa, "get_or_create_instance_identity", AsyncMock(return_value=("iid", "s"))),
            patch.object(pa, "_constant_time_equal", return_value=True),
            patch.object(pa, "verify_hmac", return_value=True),
            patch.object(pa, "get_config", AsyncMock(return_value=corrupt_entry)),
            patch.object(pa, "update_config", AsyncMock()),
            patch.object(pa, "rotate_secret", rotate_mock),
            pytest.raises(HTTPException) as exc,
        ):
            await pa.rotate_identity_secret(req, request, session, _current_user=MagicMock())

        assert exc.value.status_code == 500
        assert "sequence" in exc.value.detail.lower()
        rotate_mock.assert_not_awaited()
