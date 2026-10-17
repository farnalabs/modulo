"""Shared route-layer wrapper for the canonical org-settings patch (FAR-1832 QA F7).

FAR-1830/1832 established ONE canonical way to write org settings keys, but
every converted route hand-copied the same
``except LookupError -> HTTPException(404, MSG_ORGANISATION_NOT_FOUND)`` exit
around its ``update_org_settings_patch`` call. A new writer that omits the
mapping surfaces a raw 500 instead of a 404, and a change to the not-found
exception type or status would need a lockstep edit of every site. This helper
is the shared translation:

.. code-block:: python

    await apply_org_settings_patch_or_404(session, org_id, patch={...})

It lives beside the route modules (a module-level cross-route import is
allowed — only inline/function-local route imports are blocked by semgrep)
and imports nothing route-specific, so ``admin``, ``admin_orgs`` and
``product_analytics`` share it without a cycle. The 404 response shape is
byte-identical to the hand-copied blocks it replaces:
``HTTPException(404, MSG_ORGANISATION_NOT_FOUND)`` raised ``from None``.

Call-site set (FAR-1832 QA F12): the routes that write org settings through
the atomic patch helper use this wrapper — ``admin_orgs`` (license set/clear),
``admin`` (sandbox concurrency, HITL review window, run concurrency, retention)
and ``product_analytics`` (consent, level). Two org-settings writers do NOT:
``admin.admin_update_work_item_agent_minting`` and
``admin.admin_update_community_objects`` wrap ``set_org_flag`` (a crud call
with its own not-found path), so they keep their own ``LookupError -> 404``
mapping deliberately. This helper is therefore the shared translation for the
patch-helper call sites, not a universal one for every org-settings writer.

Patch contract (mirrors :func:`update_org_json_column_patch`): at most ONE
``nested_patch`` key per call, and no key may appear in BOTH ``patch`` and
``nested_patch``, nor in ``delete_keys`` as well as either — the compose order
would be ambiguous, so the crud entry point refuses it rather than silently
resolving delete-wins.
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
    ``MSG_ORGANISATION_NOT_FOUND``. ``patch`` is always passed positionally so
    call-site spies on ``_org_settings.update_org_settings_patch`` observe the
    same ``(session, org_id, patch)`` shape the hand-copied blocks had; an
    absent top-level patch is passed as ``None`` (one call shape, no arity
    branch).
    """
    try:
        return await update_org_settings_patch(
            session, org_id, patch, delete_keys=delete_keys, nested_patch=nested_patch
        )
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_ORGANISATION_NOT_FOUND) from None
