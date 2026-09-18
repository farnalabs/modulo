"""Unit tests for product analytics instance identity and HMAC."""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.sql import Insert

from modulo.core.product_analytics.hmac_verify import (
    _TIMESTAMP_WINDOW_SECONDS,
    sign_rotation_request,
    verify_hmac,
)
from modulo.core.product_analytics.instance_identity import (
    _INSTANCE_ID_KEY,
    _SECRET_KEY,
    get_instance_id,
    get_or_create_instance_identity,
    get_secret_exists,
    rotate_secret,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_mock_session(existing: dict[str, str] | None = None) -> AsyncMock:
    """Build a mock AsyncSession with optional existing config entries.

    Tracks what key was last queried so tests can verify the mock returned
    the right value for the right key.
    """
    session = AsyncMock()
    _stored: dict[str, str] = dict(existing) if existing else {}
    last_queried_key: list[str | None] = [None]

    def _execute(stmt):
        """Mock that inspects the compiled statement to find the key param."""
        result = MagicMock()
        stmt_str = str(stmt)

        # Both ``set_config`` (TOFU mint) and ``update_config`` (deliberate
        # overwrite) issue a ``pg_insert`` whose ``value`` is JSON-typed and
        # cannot be rendered with ``literal_binds=True``. Capture the inserted
        # key/value so the subsequent re-SELECT round-trips, and skip the
        # literal-bind compile that would otherwise raise a CompileError on the
        # JSON literal.
        if isinstance(stmt, Insert):
            try:
                params = stmt.compile().params
                inserted_key = params.get("key")
                if inserted_key is not None:
                    _stored[inserted_key] = params.get("value")
            except (ValueError, KeyError, AttributeError):
                pass
            result.scalar_one_or_none.return_value = None
            return result

        # Try to extract the key from the statement's compiled parameters
        key = None
        try:
            compiled = stmt.compile(compile_kwargs={"literal_binds": True})
            sql = str(compiled)
            for k in _stored:
                if f"'{k}'" in sql:
                    key = k
                    break
            if key is None:
                for k in _stored:
                    if f"'{k}'" in stmt_str:
                        key = k
                        break
            if key is None and hasattr(stmt, "compile"):
                for param_key, param_val in stmt.compile().params.items():
                    if isinstance(param_val, str) and param_val in _stored:
                        key = param_val
                        break
        except (ValueError, KeyError, AttributeError):
            # Compilation may fail on mock stmts — that's fine, key stays None
            pass

        last_queried_key[0] = key

        if key is None or key not in _stored:
            result.scalar_one_or_none.return_value = None
            return result

        # Distinguish between select(Model.value) and select(Model)
        # select(Model) includes all columns (id, key, value, updated_at, updated_by)
        # select(Model.value) only has the value column
        is_full_model = "system_config.id" in stmt_str

        scalar_result = MagicMock()
        if is_full_model:
            mock_obj = MagicMock()
            mock_obj.value = _stored[key]
            scalar_result.scalar_one_or_none.return_value = mock_obj
            scalar_result.scalar_one.return_value = mock_obj
        elif ".id" in stmt_str:
            # select(Model.id) — existence check
            scalar_result.scalar_one_or_none.return_value = "exists"
            scalar_result.scalar_one.return_value = "exists"
        else:
            # select(Model.value) — scalar value
            scalar_result.scalar_one_or_none.return_value = _stored[key]
            scalar_result.scalar_one.return_value = _stored[key]
        return scalar_result

    session.execute = AsyncMock(side_effect=_execute)
    session.flush = AsyncMock()

    added: list = []

    def _add(obj):
        added.append(obj)
        # Persist SystemConfig rows so subsequent re-selects observe the write,
        # mirroring real DB behaviour (the production code re-selects after upsert).
        if getattr(obj, "key", None) is not None and getattr(obj, "value", None) is not None:
            _stored[obj.key] = obj.value

    session.add = MagicMock(side_effect=_add)
    session._added = added
    session._stored = _stored
    session._last_queried_key = last_queried_key

    return session


# ── Instance identity tests ─────────────────────────────────────────────────


class TestInstanceIdentity:
    @pytest.mark.asyncio
    async def test_mint_instance_id_and_secret(self):
        """First call should mint both instance_id and secret."""
        session = _make_mock_session()
        instance_id, secret = await get_or_create_instance_identity(session)

        assert isinstance(instance_id, uuid.UUID)
        assert len(secret) == 64  # secrets.token_hex(32) = 64 hex chars
        assert session.flush.called

    @pytest.mark.asyncio
    async def test_instance_id_idempotent(self):
        """Calling twice with existing data returns the same ID."""
        existing_id = str(uuid.uuid4())
        existing_secret = "a" * 64
        session = _make_mock_session(
            {
                _INSTANCE_ID_KEY: existing_id,
                _SECRET_KEY: existing_secret,
            }
        )

        instance_id, secret = await get_or_create_instance_identity(session)
        assert str(instance_id) == existing_id
        assert secret == existing_secret

    @pytest.mark.asyncio
    async def test_get_instance_id_returns_none_when_missing(self):
        """get_instance_id returns None if not yet minted."""
        session = _make_mock_session()
        result = await get_instance_id(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_instance_id_returns_uuid(self):
        """get_instance_id returns UUID when stored."""
        existing_id = str(uuid.uuid4())
        session = _make_mock_session({_INSTANCE_ID_KEY: existing_id})
        result = await get_instance_id(session)
        assert result is not None
        assert str(result) == existing_id

    @pytest.mark.asyncio
    async def test_get_secret_exists_false(self):
        """get_secret_exists returns False when no secret stored."""
        session = _make_mock_session()
        result = await get_secret_exists(session)
        assert result is False

    @pytest.mark.asyncio
    async def test_rotate_secret_generates_new(self):
        """rotate_secret creates a new random secret."""
        session = _make_mock_session({_SECRET_KEY: "old" * 20})
        new_secret = await rotate_secret(session)
        assert len(new_secret) == 64
        assert new_secret != "old" * 20
        assert session.flush.called


# ── HMAC tests ──────────────────────────────────────────────────────────────


class TestHMAC:
    def test_compute_hmac_deterministic(self):
        """Same inputs produce the same HMAC."""
        secret = "test-secret"
        payload = b'{"test": true}'
        ts = 1700000000.0
        seq = 1

        mac1 = sign_rotation_request(secret, payload, ts, seq)
        mac2 = sign_rotation_request(secret, payload, ts, seq)
        assert mac1 == mac2
        assert len(mac1) == 64  # SHA-256 hex = 64 chars

    def test_compute_hmac_different_secrets(self):
        """Different secrets produce different HMACs."""
        payload = b'{"test": true}'
        ts = 1700000000.0
        seq = 1

        mac1 = sign_rotation_request("secret-a", payload, ts, seq)
        mac2 = sign_rotation_request("secret-b", payload, ts, seq)
        assert mac1 != mac2

    def test_compute_hmac_different_sequences(self):
        """Different sequences produce different HMACs."""
        secret = "test-secret"
        payload = b'{"test": true}'
        ts = 1700000000.0

        mac1 = sign_rotation_request(secret, payload, ts, 1)
        mac2 = sign_rotation_request(secret, payload, ts, 2)
        assert mac1 != mac2

    def test_verify_hmac_valid(self):
        """Valid HMAC within timestamp window passes."""
        secret = "test-secret"
        payload = b'{"test": true}'
        ts = time.time()
        seq = 1

        mac = sign_rotation_request(secret, payload, ts, seq)
        assert verify_hmac(secret, payload, ts, seq, mac) is True

    def test_verify_hmac_invalid_mac(self):
        """Invalid HMAC fails."""
        secret = "test-secret"
        payload = b'{"test": true}'
        ts = time.time()
        seq = 1

        assert verify_hmac(secret, payload, ts, seq, "bad") is False

    def test_verify_hmac_expired_timestamp(self):
        """HMAC with timestamp older than 5 minutes fails."""
        secret = "test-secret"
        payload = b'{"test": true}'
        old_ts = time.time() - _TIMESTAMP_WINDOW_SECONDS - 1
        seq = 1

        mac = sign_rotation_request(secret, payload, old_ts, seq)
        assert verify_hmac(secret, payload, old_ts, seq, mac) is False

    def test_verify_hmac_future_timestamp(self):
        """HMAC with timestamp more than 5 minutes in the future fails."""
        secret = "test-secret"
        payload = b'{"test": true}'
        future_ts = time.time() + _TIMESTAMP_WINDOW_SECONDS + 1
        seq = 1

        mac = sign_rotation_request(secret, payload, future_ts, seq)
        assert verify_hmac(secret, payload, future_ts, seq, mac) is False

    def test_verify_hmac_at_boundary(self):
        """HMAC at exactly the boundary passes."""
        secret = "test-secret"
        payload = b'{"test": true}'
        now = time.time()
        boundary_ts = now - _TIMESTAMP_WINDOW_SECONDS + 1  # 1s inside window
        seq = 1

        mac = sign_rotation_request(secret, payload, boundary_ts, seq)
        assert verify_hmac(secret, payload, boundary_ts, seq, mac, now=now) is True

    def test_verify_hmac_just_outside_boundary(self):
        """HMAC just outside the 5-min window fails."""
        secret = "test-secret"
        payload = b'{"test": true}'
        now = time.time()
        ts = now - _TIMESTAMP_WINDOW_SECONDS - 0.001
        seq = 1

        mac = sign_rotation_request(secret, payload, ts, seq)
        assert verify_hmac(secret, payload, ts, seq, mac, now=now) is False

    def test_verify_hmac_with_injected_now(self):
        """The now parameter allows overriding time.time()."""
        secret = "test-secret"
        payload = b'{"test": true}'
        fixed_now = 1700000000.0
        ts = fixed_now
        seq = 42

        mac = sign_rotation_request(secret, payload, ts, seq)
        assert verify_hmac(secret, payload, ts, seq, mac, now=fixed_now) is True

    def test_verify_hmac_wrong_secret(self):
        """HMAC computed with a different secret fails."""
        payload = b'{"test": true}'
        ts = time.time()
        seq = 1

        mac = sign_rotation_request("correct-secret", payload, ts, seq)
        assert verify_hmac("wrong-secret", payload, ts, seq, mac) is False
