"""FAR-1478 / ADR 058 tiered TTL: the SERVICE half (365-day maximum).

The cap lives at the ``create_api_key`` AND ``update_api_key`` choke points
so every mint/extension surface (REST, MCP, on-behalf bootstrap, key update)
refuses the same way. The 90-day USER-scoped half is FAR-1477's
(``_resolve_grants_expiry``) and is not duplicated here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.auth.api_key import (
    SERVICE_KEY_MAX_TTL_DAYS,
    ApiKeyTtlError,
    create_api_key,
    update_api_key,
)
from modulo.db.models.account import KIND_HUMAN, KIND_SERVICE

_ORG_ID = uuid.uuid4()
_ACCOUNT_ID = uuid.uuid4()
_KEY_ID = uuid.uuid4()


def _session(owner: object | None) -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.get = AsyncMock(return_value=owner)
    return session


def _owner(kind: str) -> SimpleNamespace:
    return SimpleNamespace(id=_ACCOUNT_ID, kind=kind, decommissioned_at=None)


def _update_session(owner: object | None, key: object) -> AsyncMock:
    """A session whose SELECT resolves *key* and whose GET resolves *owner*."""
    session = _session(owner)
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=key)
    session.execute = AsyncMock(return_value=result)
    return session


def _live_key() -> SimpleNamespace:
    return SimpleNamespace(
        id=_KEY_ID,
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        name="svc key",
        role="runner",
        team_id=None,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        revoked_at=None,
    )


@pytest.mark.asyncio
async def test_service_key_default_is_365_days() -> None:
    key, _ = await create_api_key(
        _session(_owner(KIND_SERVICE)),
        org_id=_ORG_ID,
        name="svc key",
        role="runner",
        account_id=_ACCOUNT_ID,
    )
    days = (key.expires_at - datetime.now(UTC)).days
    assert 364 <= days <= SERVICE_KEY_MAX_TTL_DAYS


@pytest.mark.asyncio
async def test_service_key_within_ceiling_is_accepted() -> None:
    expires_at = datetime.now(UTC) + timedelta(days=30)
    key, _ = await create_api_key(
        _session(_owner(KIND_SERVICE)),
        org_id=_ORG_ID,
        name="svc key",
        role="runner",
        account_id=_ACCOUNT_ID,
        expires_at=expires_at,
    )
    assert key.expires_at == expires_at


@pytest.mark.asyncio
async def test_service_key_beyond_ceiling_is_refused_not_clamped() -> None:
    session = _session(_owner(KIND_SERVICE))
    expires_at = datetime.now(UTC) + timedelta(days=SERVICE_KEY_MAX_TTL_DAYS + 1)
    with pytest.raises(ApiKeyTtlError, match="expire within 365 days"):
        await create_api_key(
            session,
            org_id=_ORG_ID,
            name="svc key",
            role="runner",
            account_id=_ACCOUNT_ID,
            expires_at=expires_at,
        )
    # Fail-closed: nothing was staged for insert.
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_human_key_beyond_365_days_is_unaffected() -> None:
    """The cap is service-owned-keys only — a human owner keeps the old shape."""
    expires_at = datetime.now(UTC) + timedelta(days=400)
    key, _ = await create_api_key(
        _session(_owner(KIND_HUMAN)),
        org_id=_ORG_ID,
        name="human key",
        role="operator",
        account_id=_ACCOUNT_ID,
        expires_at=expires_at,
    )
    assert key.expires_at == expires_at


@pytest.mark.asyncio
async def test_missing_owner_skips_the_service_cap() -> None:
    """No owner row cannot be a service principal (FK guarantees existence)."""
    expires_at = datetime.now(UTC) + timedelta(days=400)
    key, _ = await create_api_key(
        _session(None),
        org_id=_ORG_ID,
        name="orphan key",
        role="operator",
        account_id=_ACCOUNT_ID,
        expires_at=expires_at,
    )
    assert key.expires_at == expires_at


# ---------------------------------------------------------------------------
# update_api_key — the extension path (QA M1: the cap must hold on PUT too).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_service_key_beyond_ceiling_is_refused() -> None:
    """QA M1 regression: a >365d EXTENSION of a service-owned key is refused.

    Pre-fix ``update_api_key`` set ``key.expires_at`` unconditionally, so any
    runner/operator (api_key.revoke floor on the PUT route) could extend a
    service-owned key arbitrarily — contradicting the mint-time cap.
    """
    key = _live_key()
    original_expiry = key.expires_at
    session = _update_session(_owner(KIND_SERVICE), key)
    too_long = datetime.now(UTC) + timedelta(days=SERVICE_KEY_MAX_TTL_DAYS + 1)
    with pytest.raises(ApiKeyTtlError, match="expire within 365 days"):
        await update_api_key(session, _KEY_ID, _ORG_ID, expires_at=too_long)
    # Fail-closed: the key's expiry is untouched and nothing was flushed.
    assert key.expires_at == original_expiry
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_service_key_within_ceiling_is_accepted() -> None:
    key = _live_key()
    session = _update_session(_owner(KIND_SERVICE), key)
    new_expiry = datetime.now(UTC) + timedelta(days=90)
    updated = await update_api_key(session, _KEY_ID, _ORG_ID, expires_at=new_expiry)
    assert updated is not None
    assert updated.expires_at == new_expiry


@pytest.mark.asyncio
async def test_update_service_key_at_exact_ceiling_is_accepted() -> None:
    """The boundary is strict ``>``: exactly 365 days is allowed."""
    key = _live_key()
    session = _update_session(_owner(KIND_SERVICE), key)
    # Compute against a small safety margin so the helper's own now() cannot
    # push the comparison over the ceiling while the test runs.
    exact = datetime.now(UTC) + timedelta(days=SERVICE_KEY_MAX_TTL_DAYS, seconds=-5)
    updated = await update_api_key(session, _KEY_ID, _ORG_ID, expires_at=exact)
    assert updated is not None
    assert updated.expires_at == exact


@pytest.mark.asyncio
async def test_update_human_key_beyond_ceiling_is_unaffected() -> None:
    """The update cap is service-owned-keys only — a human owner keeps the old shape."""
    key = _live_key()
    session = _update_session(_owner(KIND_HUMAN), key)
    expires_at = datetime.now(UTC) + timedelta(days=400)
    updated = await update_api_key(session, _KEY_ID, _ORG_ID, expires_at=expires_at)
    assert updated is not None
    assert updated.expires_at == expires_at


@pytest.mark.asyncio
async def test_update_without_expiry_skips_the_cap() -> None:
    """``expires_at=None`` on update means 'leave unchanged' — no owner read needed."""
    key = _live_key()
    original_expiry = key.expires_at
    session = _update_session(_owner(KIND_SERVICE), key)
    updated = await update_api_key(session, _KEY_ID, _ORG_ID, name="renamed")
    assert updated is not None
    assert updated.name == "renamed"
    assert updated.expires_at == original_expiry
    session.get.assert_not_awaited()
