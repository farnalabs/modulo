"""FAR-1478 / ADR 058 tiered TTL: the SERVICE half (365-day maximum).

The cap lives at the ``create_api_key`` choke point so every mint surface
(REST, MCP, on-behalf bootstrap) refuses the same way. The 90-day USER-scoped
half is FAR-1477's (``_resolve_grants_expiry``) and is not duplicated here.
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
)
from modulo.db.models.account import KIND_HUMAN, KIND_SERVICE

_ORG_ID = uuid.uuid4()
_ACCOUNT_ID = uuid.uuid4()


def _session(owner: object | None) -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.get = AsyncMock(return_value=owner)
    return session


def _owner(kind: str) -> SimpleNamespace:
    return SimpleNamespace(id=_ACCOUNT_ID, kind=kind, decommissioned_at=None)


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
