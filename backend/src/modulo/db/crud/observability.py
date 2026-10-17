"""CRUD for organisation observability/OTel config stored on Organisation."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.organisation import Organisation


async def get_otel_config(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    """Return the stored OTel config for an organisation, or {} when unset."""
    # FAR-1025: opt out of the global soft-delete filter — a pending-deletion
    # org is still operationally live; its OTel config must remain readable.
    from modulo.db.soft_delete import include_soft_deleted

    result = await session.execute(
        include_soft_deleted(select(Organisation.otel_config_json).where(Organisation.id == org_id))
    )
    row = result.scalar_one_or_none()
    return row if row is not None else {}


async def update_otel_config(
    session: AsyncSession,
    org_id: uuid.UUID,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Atomically merge ``config`` into the stored OTel config and return it.

    FAR-1832 (QA F3): routes through the SHARED atomic JSON-column patch
    machinery (:func:`modulo.db.crud.organisation.update_org_json_column_patch`)
    — one implementation of the bounded lock wait, the fail-closed tenant
    guard, the dialect split, the ``LookupError`` for a missing org and the
    identity-map refresh, exactly as the settings writer uses. The two
    legitimate OTel differences are ARGUMENTS, not a fork:

    - ``column="otel_config_json"`` — a different column;
    - ``require_live_row=False`` — FAR-1025 keeps a pending-deletion org's
      OTel config **writable** during the confirmation window, so this path
      deliberately carries no ``deleted_at IS NULL`` predicate on either
      dialect (the settings helper refuses soft-deleted orgs; inheriting that
      refusal here would silently regress FAR-1025);
    - it never uses ``delete_keys`` (an empty clear list is passed).

    The tenant guard APPLIES here (FAR-1832 QA F3): the caller must have bound
    the org RLS context (``set_rls_org``) for this org — the same contract as
    every settings writer. ``organisations`` is NOT RLS-scoped, so this guard
    is the only tenant control on the raw ``UPDATE organisations … WHERE id =
    :org_id``; before F3 this function trusted the ``org_id`` argument
    verbatim. A session bound to a different org is refused with
    ``OrgSettingsPatchOrgMismatch``; an unbound/unreadable context is refused
    with the ``OrgSettingsPatchContext*`` errors.

    Missing org raises ``ValueError("Organisation not found")`` (unchanged
    contract — the CRUD's ``LookupError`` is translated at this boundary).
    """
    from modulo.db.crud.organisation import update_org_json_column_patch

    try:
        return await update_org_json_column_patch(
            session,
            org_id,
            patch=config,
            delete_keys=[],
            column="otel_config_json",
            require_live_row=False,
        )
    except LookupError:
        raise ValueError("Organisation not found") from None
