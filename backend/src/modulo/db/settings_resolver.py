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
