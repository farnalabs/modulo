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


def org_row_is_paused(status: str | None, triggers_paused: bool | None) -> bool:
    """Pure predicate: is this org row's pause state considered "paused"?

    Returns ``True`` when ``triggers_paused`` is explicitly True, OR when
    ``status`` is set and not ``"active"`` (a suspended/deleted org blocks
    triggers the same way a paused one does — fail-closed on non-active orgs).

    A missing row (``status=None``) returns ``False`` here; the query-level
    helper ``org_is_paused`` handles missing rows (fail-closed).
    """
    if triggers_paused is True:
        return True
    return bool(status is not None and status != "active")


async def org_is_paused(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Return whether the org-wide trigger pause is in effect for *org_id*.

    Dedicated column-level SELECT (``Organisation.triggers_paused`` +
    ``Organisation.status``) — never the ORM identity map, so a freshly toggled
    value is observed even on a session that already loaded the org row.

    Fail-closed: a missing row (deleted org) returns ``True`` so its triggers
    can never fire. Do NOT swallow ``SQLAlchemyError`` — it propagates to the
    caller, which decides how to surface the read failure.

    Unknown pause state => caller must treat as unavailable; this helper raises
    on read failure.
    """
    from sqlalchemy import select

    from modulo.db.models.organisation import Organisation

    result = await session.execute(
        select(Organisation.triggers_paused, Organisation.status).where(Organisation.id == org_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return True
    triggers_paused, status = row
    return org_row_is_paused(status, triggers_paused)
