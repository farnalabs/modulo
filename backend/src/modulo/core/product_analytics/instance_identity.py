"""Instance identity for product analytics — Trust-On-First-Use bootstrap.

Mints a unique instance_id (UUID) and a shared secret (hex token) once per
deployment, storing both in SystemConfig.  The secret is stored with
``repr=False`` so it never leaks into logs or payloads.
"""

from __future__ import annotations

import logging
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.system_config import SystemConfig

_INSTANCE_ID_KEY = "product_analytics_instance_id"
_SECRET_KEY = "product_analytics_instance_secret"

_log = logging.getLogger(__name__)


async def get_or_create_instance_identity(
    session: AsyncSession,
) -> tuple[uuid.UUID, str]:
    """Return ``(instance_id, secret)``, creating both on first call.

    Uses ``INSERT … ON CONFLICT DO NOTHING`` + re-select so concurrent
    callers converge to the same values without race conditions.
    """
    instance_id = await _get_or_create_uuid(session, _INSTANCE_ID_KEY)
    secret = await _get_or_create_secret(session, _SECRET_KEY)
    return instance_id, secret


async def get_instance_id(session: AsyncSession) -> uuid.UUID | None:
    """Return the stored instance ID, or ``None`` if not yet minted."""
    row = await session.execute(select(SystemConfig.value).where(SystemConfig.key == _INSTANCE_ID_KEY))
    raw = row.scalar_one_or_none()
    if raw is None:
        return None
    return uuid.UUID(str(raw))


async def get_secret_exists(session: AsyncSession) -> bool:
    """Return ``True`` if a secret has been stored (without revealing it)."""
    row = await session.execute(select(SystemConfig.id).where(SystemConfig.key == _SECRET_KEY))
    return row.scalar_one_or_none() is not None


async def rotate_secret(session: AsyncSession) -> str:
    """Generate and store a new secret, returning its value.

    The old secret is overwritten.  Callers must authenticate with the old
    secret before calling this.
    """
    new_secret = secrets.token_hex(32)
    await _upsert_config(session, _SECRET_KEY, new_secret)
    _log.info("product_analytics.secret_rotated")
    return new_secret


# ── internals ───────────────────────────────────────────────────────────────


async def _get_or_create_uuid(session: AsyncSession, key: str) -> uuid.UUID:
    """Idempotently mint a UUID in SystemConfig.

    Uses ``_upsert_config`` (which does ``SELECT … FOR UPDATE``) to avoid
    TOCTOU races between concurrent callers.
    """
    new_id = uuid.uuid4()
    await _upsert_config(session, key, str(new_id))
    # Re-select the authoritative value — another caller may have won the race.
    row = await session.execute(select(SystemConfig.value).where(SystemConfig.key == key))
    raw = row.scalar_one()
    _log.info("product_analytics.instance_id_minted")
    return uuid.UUID(str(raw))


async def _get_or_create_secret(session: AsyncSession, key: str) -> str:
    """Idempotently mint a hex secret in SystemConfig.

    Uses ``_upsert_config`` (which does ``SELECT … FOR UPDATE``) to avoid
    TOCTOU races between concurrent callers.
    """
    new_secret = secrets.token_hex(32)
    await _upsert_config(session, key, new_secret)
    # Re-select the authoritative value — another caller may have won the race.
    row = await session.execute(select(SystemConfig.value).where(SystemConfig.key == key))
    raw = row.scalar_one()
    _log.info("product_analytics.secret_generated")
    return str(raw)


async def _upsert_config(session: AsyncSession, key: str, value: str) -> None:
    """Insert-or-update a SystemConfig row."""
    existing = await session.execute(select(SystemConfig).where(SystemConfig.key == key).with_for_update())
    row = existing.scalar_one_or_none()
    if row is not None:
        row.value = value
    else:
        session.add(SystemConfig(key=key, value=value))
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await session.execute(select(SystemConfig).where(SystemConfig.key == key).with_for_update())
        row = existing.scalar_one()
        row.value = value
        await session.flush()
