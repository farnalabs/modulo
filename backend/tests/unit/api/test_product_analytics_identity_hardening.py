"""Unit tests for product-analytics identity route hardening.

FAR-1633 — ``_get_last_sequence`` + ``_set_last_sequence`` must form an atomic
read-check-write within the request transaction.  The read takes a per-instance
row lock (``SELECT … FOR UPDATE`` on the identity anchor row) *before* reading
the stored sequence, and the route must request that lock.

FAR-1634 — a missing sequence row is legitimate only before the first rotation.
Once the has-rotated marker is set, a missing row fails closed with
``SequenceStateError`` instead of silently resetting the guard to ``0``.

Two further regressions are pinned here:

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
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.api.routes import product_analytics_identity as pa


def _mock_session() -> AsyncMock:
    """An AsyncMock session whose ``begin()`` is an async context manager."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _fresh_limiter() -> dict[str, list[float]]:
    """Return an empty rate-limiter map (typed as the module-level one)."""
    return {}


def _fake_session() -> AsyncMock:
    """Return an AsyncMock session whose ``begin()`` is an async context manager.

    ``execute`` yields a result whose ``scalar_one_or_none`` reports the
    identity anchor row as present, so :func:`_lock_rotation_anchor` (reached
    because the route reads the sequence with ``for_update=True``) finds the
    row and does not fail closed. A bare ``AsyncMock`` would hand back a
    coroutine from ``scalar_one_or_none`` that is never awaited.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    result = MagicMock()
    result.scalar_one_or_none.return_value = MagicMock()
    session.execute = AsyncMock(return_value=result)
    return session


def _request(host: str = "10.9.9.9") -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=host))


def _rotate_req() -> pa.RotateRequest:
    return pa.RotateRequest(old_secret="old", timestamp=1.0, sequence=1, hmac_digest="digest")


# ---------------------------------------------------------------------------
# FAR-1634 — fail closed when the sequence row was deleted after a rotation
# ---------------------------------------------------------------------------


class TestGetLastSequenceFailClosed:
    async def test_missing_row_before_first_rotation_is_legitimate_zero(self) -> None:
        """No sequence row AND no has-rotated marker → pristine instance → 0."""
        session = AsyncMock()
        with patch.object(pa, "get_config", new=AsyncMock(return_value=None)):
            assert await pa._get_last_sequence(session, "inst-1") == 0

    async def test_missing_row_after_rotation_fails_closed(self) -> None:
        """Sequence row gone but the has-rotated marker set → refuse (no reset to 0)."""
        session = AsyncMock()

        async def fake_get_config(_session: object, key: str) -> object:
            if key == pa._has_rotated_key("inst-1"):
                return SimpleNamespace(value=True)
            return None

        with (
            patch.object(pa, "get_config", new=AsyncMock(side_effect=fake_get_config)),
            pytest.raises(pa.SequenceStateError, match="already rotated"),
        ):
            await pa._get_last_sequence(session, "inst-1")

    async def test_marker_false_is_treated_as_not_rotated(self) -> None:
        """A falsy marker value (e.g. pathological stored value) does not fail closed."""
        session = AsyncMock()

        async def fake_get_config(_session: object, key: str) -> object:
            if key == pa._has_rotated_key("inst-1"):
                return SimpleNamespace(value=False)
            return None

        with patch.object(pa, "get_config", new=AsyncMock(side_effect=fake_get_config)):
            assert await pa._get_last_sequence(session, "inst-1") == 0

    async def test_present_row_returns_stored_sequence(self) -> None:
        session = AsyncMock()
        with patch.object(pa, "get_config", new=AsyncMock(return_value=SimpleNamespace(value=7))):
            assert await pa._get_last_sequence(session, "inst-1") == 7

    async def test_set_last_sequence_persists_value_and_marker(self) -> None:
        """The has-rotated marker is written alongside the sequence value."""
        session = AsyncMock()
        calls: list[tuple[str, object]] = []

        async def fake_update(_session: object, key: str, value: object, *args: object, **kwargs: object) -> object:
            calls.append((key, value))
            return SimpleNamespace(key=key, value=value)

        with patch.object(pa, "update_config", new=AsyncMock(side_effect=fake_update)):
            await pa._set_last_sequence(session, "inst-1", 9)

        assert calls == [(pa._sequence_key("inst-1"), 9), (pa._has_rotated_key("inst-1"), True)]


# ---------------------------------------------------------------------------
# FAR-1633 — atomic read-check-write via a row lock
# ---------------------------------------------------------------------------


class TestAtomicSequenceLock:
    async def test_for_update_takes_lock_before_reading_sequence(self) -> None:
        """The lock must be acquired BEFORE the sequence read (atomic critical section)."""
        session = AsyncMock()
        order: list[str] = []
        captured: dict[str, object] = {}

        async def fake_execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            order.append("lock")
            captured["stmt"] = stmt
            return MagicMock()

        async def fake_get_config(_session: object, key: str) -> object:
            order.append("read")
            return None

        session.execute = AsyncMock(side_effect=fake_execute)
        with patch.object(pa, "get_config", new=AsyncMock(side_effect=fake_get_config)):
            await pa._get_last_sequence(session, "inst-1", for_update=True)

        assert order[0] == "lock"
        stmt = captured["stmt"]
        assert stmt._for_update_arg is not None  # type: ignore[attr-defined]
        assert "FOR UPDATE" in str(stmt)
        assert pa._INSTANCE_ID_KEY in stmt.compile().params.values()

    async def test_without_for_update_no_lock_is_taken(self) -> None:
        session = AsyncMock()
        with patch.object(pa, "get_config", new=AsyncMock(return_value=None)):
            await pa._get_last_sequence(session, "inst-1")
        session.execute.assert_not_called()

    async def test_missing_anchor_row_fails_closed(self) -> None:
        """A FOR UPDATE that matches no row takes no lock — it must fail closed, not no-op."""
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        with pytest.raises(pa.SequenceStateError, match="anchor"):
            await pa._get_last_sequence(session, "inst-1", for_update=True)

    async def test_lock_timeout_maps_to_409(self) -> None:
        """A bounded-lock timeout (SQLSTATE 55P03) is a conflict, not a database outage."""
        from sqlalchemy.exc import OperationalError

        orig = Exception("lock timeout")
        orig.pgcode = "55P03"  # type: ignore[attr-defined]
        lock_error = OperationalError("SELECT ... FOR UPDATE", {}, orig)
        session = _mock_session()
        with (
            patch.object(pa, "set_mutation_row_lock_timeout", new=AsyncMock()),
            patch.object(
                pa,
                "get_or_create_instance_identity",
                new=AsyncMock(return_value=(uuid.uuid4(), "current")),
            ),
            patch.object(pa, "_constant_time_equal", return_value=True),
            patch.object(pa, "verify_hmac", return_value=True),
            patch.object(pa, "_get_last_sequence", new=AsyncMock(side_effect=lock_error)),
            pytest.raises(HTTPException) as exc_info,
        ):
            await pa.rotate_identity_secret(_rotate_req(), _request("10.9.9.21"), session, _current_user=None)

        assert exc_info.value.status_code == 409

    async def test_route_bounds_the_lock_wait(self) -> None:
        """The route must bound the row-lock wait before taking the lock (FAR-1313 pattern)."""
        session = _mock_session()
        bounded = AsyncMock()
        with (
            patch.object(pa, "set_mutation_row_lock_timeout", new=bounded),
            patch.object(
                pa,
                "get_or_create_instance_identity",
                new=AsyncMock(return_value=(uuid.uuid4(), "current")),
            ),
            patch.object(pa, "_constant_time_equal", return_value=True),
            patch.object(pa, "verify_hmac", return_value=True),
            patch.object(pa, "_get_last_sequence", new=AsyncMock(return_value=0)),
            patch.object(pa, "_set_last_sequence", new=AsyncMock()),
            patch.object(pa, "rotate_secret", new=AsyncMock(return_value="new-secret")),
        ):
            await pa.rotate_identity_secret(_rotate_req(), _request("10.9.9.22"), session, _current_user=None)

        bounded.assert_awaited_once()

    async def test_route_requests_the_lock(self) -> None:
        """The rotate route must call ``_get_last_sequence`` with ``for_update=True``."""
        session = _mock_session()
        mock_get = AsyncMock(return_value=0)
        with (
            patch.object(
                pa,
                "get_or_create_instance_identity",
                new=AsyncMock(return_value=(uuid.uuid4(), "current")),
            ),
            patch.object(pa, "_constant_time_equal", return_value=True),
            patch.object(pa, "verify_hmac", return_value=True),
            patch.object(pa, "_get_last_sequence", new=mock_get),
            patch.object(pa, "_set_last_sequence", new=AsyncMock()),
            patch.object(pa, "rotate_secret", new=AsyncMock(return_value="new-secret")),
        ):
            resp = await pa.rotate_identity_secret(_rotate_req(), _request(), session, _current_user=None)

        mock_get.assert_awaited_once()
        assert mock_get.await_args.kwargs.get("for_update") is True
        assert resp.new_secret == "new-secret"

    async def test_route_fails_closed_when_sequence_state_inconsistent(self) -> None:
        """A ``SequenceStateError`` surfaces as a clear 500, never a silent rotate."""
        session = _mock_session()
        with (
            patch.object(
                pa,
                "get_or_create_instance_identity",
                new=AsyncMock(return_value=(uuid.uuid4(), "current")),
            ),
            patch.object(pa, "_constant_time_equal", return_value=True),
            patch.object(pa, "verify_hmac", return_value=True),
            patch.object(
                pa,
                "_get_last_sequence",
                new=AsyncMock(side_effect=pa.SequenceStateError("already rotated")),
            ),
            patch.object(pa, "_set_last_sequence", new=AsyncMock()),
            patch.object(pa, "rotate_secret", new=AsyncMock(return_value="new-secret")),
            pytest.raises(HTTPException) as exc_info,
        ):
            await pa.rotate_identity_secret(_rotate_req(), _request(), session, _current_user=None)

        assert exc_info.value.status_code == 500
        assert "inconsistent" in exc_info.value.detail


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
