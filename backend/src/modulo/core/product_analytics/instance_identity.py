"""Instance identity for product analytics — Trust-On-First-Use bootstrap.

Mints a unique instance_id (UUID) and a shared secret (hex token) once per
deployment, storing both in SystemConfig.  The secret is never returned by the
identity helpers and is only revealed through an explicit rotation call, so it
never leaks into logs or payloads.
"""

from __future__ import annotations

import logging
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.system_config import get_config, set_config, update_config
from modulo.db.models.system_config import SystemConfig

_INSTANCE_ID_KEY = "product_analytics_instance_id"
_SECRET_KEY = "product_analytics_instance_secret"

_log = logging.getLogger(__name__)


async def get_or_create_instance_identity(
    session: AsyncSession,
) -> tuple[uuid.UUID, str]:
    """Return ``(instance_id, secret)``, creating both on first call.

    Uses ``get_config`` + ``set_config`` idempotently: if the values already
    exist they are returned unchanged, otherwise they are minted once.  The
    concurrent-first-write race is handled inside ``set_config`` (which is
    first-write-wins: the losing caller adopts the winning caller's stored value,
    so all concurrent callers converge to a single instance id / secret).
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
    await update_config(session, _SECRET_KEY, new_secret)
    _log.info("product_analytics.secret_rotated")
    return new_secret


# ── internals ───────────────────────────────────────────────────────────────


async def _get_or_create_uuid(session: AsyncSession, key: str) -> uuid.UUID:
    """Idempotently read or mint a UUID in SystemConfig.

    Returns the stored value unchanged when it already exists, so concurrent
    callers converge to a single instance id.
    """
    existing = await get_config(session, key)
    if existing is not None:
        return uuid.UUID(str(existing.value))
    new_id = uuid.uuid4()
    # ``set_config`` is first-write-wins: if a concurrent caller won the race,
    # ``entity.value`` is that caller's (winning) id, so all callers converge to
    # a single instance id.
    entity = await set_config(session, key, str(new_id))
    _log.info("product_analytics.instance_id_minted")
    return uuid.UUID(str(entity.value))


async def _get_or_create_secret(session: AsyncSession, key: str) -> str:
    """Idempotently read or mint a hex secret in SystemConfig.

    Returns the stored value unchanged when it already exists, so concurrent
    callers converge to a single shared secret.
    """
    existing = await get_config(session, key)
    if existing is not None:
        return str(existing.value)
    new_secret = secrets.token_hex(32)
    # ``set_config`` is first-write-wins: if a concurrent caller won the race,
    # ``entity.value`` is that caller's (winning) secret, so all callers converge
    # to a single shared secret.
    entity = await set_config(session, key, new_secret)
    _log.info("product_analytics.secret_generated")
    return str(entity.value)
