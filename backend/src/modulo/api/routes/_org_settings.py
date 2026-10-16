"""Shared route-layer wrapper for the canonical org-settings patch (FAR-1832 QA F7).

The acceptance of FAR-1830/1832 is ONE canonical way to write org settings
keys — yet every converted route hand-copied the same
``except LookupError -> HTTPException(404, MSG_ORGANISATION_NOT_FOUND)``
exit around its ``update_org_settings_patch`` call (nine sites at QA time). A
new writer that omits the mapping surfaces a raw 500 instead of a 404, and a
change to the not-found exception type or status would need a lockstep edit of
every site. This helper is that ONE translation:

.. code-block:: python

    await apply_org_settings_patch_or_404(session, org_id, patch={...})

It lives beside the route modules (a module-level cross-route import is
allowed — only inline/function-local route imports are blocked by semgrep)
and imports nothing route-specific, so ``admin``, ``admin_orgs`` and
``product_analytics`` share it without a cycle. The response shape is
byte-identical to the hand-copied blocks it replaces: ``HTTPException(404,
MSG_ORGANISATION_NOT_FOUND)`` raised ``from None``.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_ORGANISATION_NOT_FOUND
from modulo.db.crud.organisation import update_org_settings_patch

__all__ = ["apply_org_settings_patch_or_404"]


async def apply_org_settings_patch_or_404(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    patch: dict[str, Any] | None = None,
    delete_keys: list[str] | None = None,
    nested_patch: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply the atomic org-settings patch; a missing/soft-deleted org is a 404.

    Wraps :func:`modulo.db.crud.organisation.update_org_settings_patch`,
    translating its ``LookupError`` (org missing or soft-deleted — the patch
    helper refuses both) into the routes' shared ``404`` with
    ``MSG_ORGANISATION_NOT_FOUND``. ``patch`` is passed positionally through
    so call-site spies on ``_org_settings.update_org_settings_patch`` observe
    the same ``(session, org_id, patch)`` shape the hand-copied blocks had.
    """
    try:
        if patch is not None:
            # Positional (session, org_id, patch) — the exact call shape the
            # hand-copied blocks used, so route-level spies and their
            # ``call_args.args[2]`` assertions keep working unchanged.
            return await update_org_settings_patch(
                session, org_id, patch, delete_keys=delete_keys, nested_patch=nested_patch
            )
        return await update_org_settings_patch(session, org_id, delete_keys=delete_keys, nested_patch=nested_patch)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_ORGANISATION_NOT_FOUND) from None
