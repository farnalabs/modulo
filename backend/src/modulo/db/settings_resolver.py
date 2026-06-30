import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.organisation import get_organisation
from modulo.db.crud.system_config import get_config


async def get_effective_setting(
    session: AsyncSession,
    org_id: uuid.UUID | None,
    key: str,
    default: Any = None,
) -> Any:
    """Resolve a setting: org.settings_json → SystemConfig → default."""
    if org_id is not None:
        org = await get_organisation(session, org_id)
        if org is not None and org.settings_json and key in org.settings_json:
            return org.settings_json[key]

    config = await get_config(session, key)
    if config is not None:
        return config.value

    return default
