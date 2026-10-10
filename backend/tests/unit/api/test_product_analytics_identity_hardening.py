"""Unit tests for the rotation-sequence hardening (FAR-1633 + FAR-1634).

FAR-1633 — ``_get_last_sequence`` + ``_set_last_sequence`` must form an atomic
read-check-write within the request transaction.  The read takes a per-instance
row lock (``SELECT … FOR UPDATE`` on the identity anchor row) *before* reading
the stored sequence, and the route must request that lock.

FAR-1634 — a missing sequence row is legitimate only before the first rotation.
Once the has-rotated marker is set, a missing row fails closed with
``SequenceStateError`` instead of silently resetting the guard to ``0``.
"""

from __future__ import annotations

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
