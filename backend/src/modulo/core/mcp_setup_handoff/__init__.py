"""Generic MCP setup handoff — secrets that never touch the LLM context.

Flow:
1. MCP tool creates a resource in 'pending_setup' state
2. Service generates a one-time token and returns the setup URL
3. User visits the URL in their browser and provides the secret
4. Service consumes the token and completes the resource setup

Future tools: call create_handoff() and consume_handoff().
"""

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.mcp_setup_token import McpSetupToken
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

HANDOFF_TTL_MINUTES = 15


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_handoff(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
    created_by: uuid.UUID,
) -> dict[str, Any]:
    """Create a setup handoff. Returns {setup_url, expires_at, expires_in_minutes}."""
    raw_token = _generate_token()
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(UTC) + timedelta(minutes=HANDOFF_TTL_MINUTES)

    token_record = McpSetupToken(
        organisation_id=org_id,
        resource_type=resource_type,
        resource_id=resource_id,
        token_hash=token_hash,
        expires_at=expires_at,
        created_by=created_by,
    )
    session.add(token_record)

    _log.info("Created handoff: type=%s resource_id=%s org=%s", resource_type, resource_id, org_id)

    settings = get_settings()
    base_url = settings.modulo_public_url.rstrip("/")
    setup_url = f"{base_url}/setup/{resource_type}/{resource_id}?token={raw_token}"

    return {
        "setup_url": setup_url,
        "expires_at": expires_at.isoformat(),
        "expires_in_minutes": HANDOFF_TTL_MINUTES,
    }


async def consume_handoff(
    session: AsyncSession,
    *,
    raw_token: str,
    resource_type: str,
    org_id: uuid.UUID,
) -> McpSetupToken | None:
    """Validate and consume a setup token. Returns the token record or None."""
    token_hash = _hash_token(raw_token)
    now = datetime.now(UTC)
    result = await session.execute(
        select(McpSetupToken)
        .where(
            McpSetupToken.token_hash == token_hash,
            McpSetupToken.resource_type == resource_type,
            McpSetupToken.organisation_id == org_id,
            McpSetupToken.completed_at.is_(None),
            McpSetupToken.expires_at > now,
        )
        .with_for_update()
    )
    record = result.scalar_one_or_none()
    if record is None:
        _log.warning("Handoff token not found or expired: type=%s org=%s", resource_type, org_id)
        return None

    _log.info("Consumed handoff: type=%s resource_id=%s", resource_type, record.resource_id)
    record.completed_at = now
    return record
