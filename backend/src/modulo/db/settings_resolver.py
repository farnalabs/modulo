import logging
import uuid

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.organisation import get_organisation
from modulo.db.crud.system_config import get_config

_log = logging.getLogger(__name__)


async def get_effective_setting(
    session: AsyncSession,
    org_id: uuid.UUID | None,
    key: str,
    default: object = None,
) -> object:
    """Resolve a setting: org.settings_json → SystemConfig → default."""
    if org_id is not None:
        try:
            org = await get_organisation(session, org_id)
            if org is not None and isinstance(org.settings_json, dict) and key in org.settings_json:
                return org.settings_json[key]
        except SQLAlchemyError:
            _log.warning("Failed to resolve org setting for key=%s org=%s", key, org_id, exc_info=True)

    try:
        config = await get_config(session, key)
        if config is not None:
            return config.value
    except SQLAlchemyError:
        _log.warning("Failed to resolve system config for key=%s", key, exc_info=True)

    return default


async def resolve_authz_enforce(
    session: AsyncSession,
    org_id: uuid.UUID | None,
) -> bool:
    """Return whether authorization enforcement is ON for the org.

    Reads ``organisations.authz_enforce`` (dedicated boolean column) — the
    per-org kill switch. Defaults to True when the row is absent. Per-request
    read (uncached). ADR 017 DECISION 3.

    Fail-closed: a SQL read error also defaults to True (enforce). The caller
    must provide an active transaction (e.g. ``async with session.begin():``);
    the read is a dedicated SELECT so it never observes a stale pre-flip value
    from the ORM identity map.
    """
    if org_id is None:
        return True
    try:
        from sqlalchemy import select

        from modulo.db.models.organisation import Organisation

        result = await session.execute(select(Organisation.authz_enforce).where(Organisation.id == org_id))
        value = result.scalar_one_or_none()
    except SQLAlchemyError:
        _log.warning("permission.kill_switch_read_failed", exc_info=True)
        return True
    if value is None:
        return True
    return bool(value)
