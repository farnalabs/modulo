"""Unit tests for API key generation and validation."""

import hashlib
import hmac
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.auth.api_key import (
    ApiKeyInvalidError,
    _hash_key,
    generate_api_key,
    validate_api_key,
)

# ---------------------------------------------------------------------------
# generate_api_key
# ---------------------------------------------------------------------------


def test_generate_api_key_has_mk_prefix() -> None:
    full_key, _prefix, _hashed = generate_api_key()
    assert full_key.startswith("mk_")


def test_generate_api_key_prefix_8_chars() -> None:
    _, prefix, _ = generate_api_key()
    assert len(prefix) == 8


def test_generate_api_key_hash_is_sha256_of_full_key() -> None:
    full_key, _, hashed = generate_api_key()
    expected = hashlib.sha256(full_key.encode()).hexdigest()
    assert hashed == expected


def test_generate_api_key_unique_each_call() -> None:
    k1, _, _ = generate_api_key()
    k2, _, _ = generate_api_key()
    assert k1 != k2


def test_hash_key_constant_time_verifiable() -> None:
    full_key, _, expected_hash = generate_api_key()
    computed = _hash_key(full_key)
    assert hmac.compare_digest(expected_hash, computed)


# ---------------------------------------------------------------------------
# validate_api_key
# ---------------------------------------------------------------------------


def _make_key_row(full_key: str) -> MagicMock:
    k = MagicMock()
    k.id = uuid.uuid4()
    k.lookup_prefix = full_key[3:11]
    k.hashed_secret = _hash_key(full_key)
    k.role = "operator"
    k.last_used_at = None
    k.expires_at = None
    return k


def _make_session(key_row: MagicMock | None) -> AsyncMock:
    session = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value = [key_row] if key_row else []
    session.execute = AsyncMock(return_value=scalar_result)
    return session


@pytest.mark.asyncio
async def test_validate_api_key_success() -> None:
    full_key, _, _ = generate_api_key()
    key_row = _make_key_row(full_key)
    org_id = uuid.uuid4()
    session = _make_session(key_row)

    result = await validate_api_key(session, full_key, org_id)
    assert result is key_row


@pytest.mark.asyncio
async def test_validate_api_key_invalid_prefix_raises() -> None:
    session = _make_session(None)
    org_id = uuid.uuid4()
    with pytest.raises(ApiKeyInvalidError):
        await validate_api_key(session, "bad_key", org_id)


@pytest.mark.asyncio
async def test_validate_api_key_not_found_raises() -> None:
    full_key, _, _ = generate_api_key()
    session = _make_session(None)
    org_id = uuid.uuid4()
    with pytest.raises(ApiKeyInvalidError):
        await validate_api_key(session, full_key, org_id)


@pytest.mark.asyncio
async def test_validate_api_key_hash_mismatch_raises() -> None:
    full_key, _, _ = generate_api_key()
    key_row = _make_key_row(full_key)
    key_row.hashed_secret = "a" * 64  # wrong hash
    org_id = uuid.uuid4()
    session = _make_session(key_row)
    with pytest.raises(ApiKeyInvalidError):
        await validate_api_key(session, full_key, org_id)


@pytest.mark.asyncio
async def test_validate_api_key_expired_raises() -> None:
    from datetime import UTC, datetime, timedelta

    full_key, _, _ = generate_api_key()
    key_row = _make_key_row(full_key)
    key_row.expires_at = datetime.now(UTC) - timedelta(days=1)
    org_id = uuid.uuid4()
    session = _make_session(key_row)
    with pytest.raises(ApiKeyInvalidError):
        await validate_api_key(session, full_key, org_id)


# ---------------------------------------------------------------------------
# _validate_team_key_role
# ---------------------------------------------------------------------------


def test_validate_team_key_role_rejects_admin() -> None:
    from unittest.mock import MagicMock

    from modulo.auth.api_key import ApiKeyInvalidError, _validate_team_key_role

    key = MagicMock()
    key.team_id = uuid.uuid4()
    key.role = "admin"

    with pytest.raises(ApiKeyInvalidError, match="team-scoped API keys cannot have admin role"):
        _validate_team_key_role(key)


def test_validate_team_key_role_allows_operator() -> None:
    from unittest.mock import MagicMock

    from modulo.auth.api_key import _validate_team_key_role

    key = MagicMock()
    key.team_id = uuid.uuid4()
    key.role = "operator"

    _validate_team_key_role(key)


def test_validate_team_key_role_allows_runner() -> None:
    from unittest.mock import MagicMock

    from modulo.auth.api_key import _validate_team_key_role

    key = MagicMock()
    key.team_id = uuid.uuid4()
    key.role = "runner"

    _validate_team_key_role(key)


def test_validate_team_key_role_skips_org_wide() -> None:
    from unittest.mock import MagicMock

    from modulo.auth.api_key import _validate_team_key_role

    key = MagicMock()
    key.team_id = None
    key.role = "admin"

    _validate_team_key_role(key)


# ---------------------------------------------------------------------------
# create_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_api_key_accepts_expires_at() -> None:
    from datetime import UTC, datetime, timedelta

    from modulo.auth.api_key import create_api_key

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    future = datetime.now(UTC) + timedelta(days=30)

    key, full_key = await create_api_key(
        session,
        org_id=org_id,
        name="Test Key",
        role="runner",
        account_id=user_id,
        expires_at=future,
    )
    assert key is not None
    assert full_key.startswith("mk_")


@pytest.mark.asyncio
async def test_create_api_key_with_team_id() -> None:
    from modulo.auth.api_key import create_api_key

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    team_id = uuid.uuid4()

    key, full_key = await create_api_key(
        session,
        org_id=org_id,
        name="Team Key",
        role="operator",
        account_id=user_id,
        team_id=team_id,
    )
    assert key is not None
    assert key.team_id == team_id
    assert full_key.startswith("mk_")


@pytest.mark.asyncio
async def test_create_api_key_with_team_id_rejects_admin() -> None:
    from modulo.auth.api_key import ApiKeyInvalidError, create_api_key

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    team_id = uuid.uuid4()

    with pytest.raises(ApiKeyInvalidError):
        await create_api_key(
            session,
            org_id=org_id,
            name="Bad Key",
            role="admin",
            account_id=user_id,
            team_id=team_id,
        )


# ---------------------------------------------------------------------------
# update_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_api_key_updates_name_and_role() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from modulo.auth.api_key import update_api_key
    from modulo.db.models.api_key import OrgApiKey

    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.name = "Original"
    key.role = "operator"

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    updated = await update_api_key(session, key_id, org_id, name="Updated", role="runner")
    assert updated is not None
    assert updated.name == "Updated"
    assert updated.role == "runner"


@pytest.mark.asyncio
async def test_update_api_key_not_found_returns_none() -> None:
    from modulo.auth.api_key import update_api_key

    result = MagicMock()
    result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    updated = await update_api_key(session, uuid.uuid4(), uuid.uuid4(), name="Nope")
    assert updated is None


@pytest.mark.asyncio
async def test_update_api_key_updates_team_id() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from modulo.auth.api_key import update_api_key
    from modulo.db.models.api_key import OrgApiKey

    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    team_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.name = "Original"
    key.role = "operator"
    key.team_id = None

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    updated = await update_api_key(session, key_id, org_id, team_id=team_id)
    assert updated is not None
    assert updated.team_id == team_id


@pytest.mark.asyncio
async def test_update_api_key_with_team_id_rejects_admin() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from modulo.auth.api_key import ApiKeyInvalidError, update_api_key
    from modulo.db.models.api_key import OrgApiKey

    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    team_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.name = "Original"
    key.role = "admin"
    key.team_id = None

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    with pytest.raises(ApiKeyInvalidError):
        await update_api_key(session, key_id, org_id, team_id=team_id)


@pytest.mark.asyncio
async def test_update_api_key_updates_expires_at() -> None:
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock, MagicMock

    from modulo.auth.api_key import update_api_key
    from modulo.db.models.api_key import OrgApiKey

    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    future = datetime.now(UTC) + timedelta(days=60)
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.name = "Original"
    key.role = "operator"
    key.expires_at = None

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    updated = await update_api_key(session, key_id, org_id, expires_at=future)
    assert updated is not None
    assert updated.expires_at == future


# ---------------------------------------------------------------------------
# validate_api_key — revoked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_api_key_revoked_raises() -> None:
    """Revoked keys are filtered out by the query (revoked_at IS NULL) — not found."""

    full_key, _, _ = generate_api_key()
    org_id = uuid.uuid4()
    session = _make_session(None)

    with pytest.raises(ApiKeyInvalidError):
        await validate_api_key(session, full_key, org_id)
