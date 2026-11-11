"""Org-admin feature-flag governance (FAR-2302).

An organisation admin may set the feature flags that govern THEIR OWN
organisation's usage, without the instance-wide ``system.config.manage`` gate the
system feature-flags page requires. Not every flag is theirs to set: the
``FeatureFlag.org_admin_settable`` classification (declared once in
``core.feature_flags._KNOWN_FLAGS``) is the single source of truth, and it is
fail-closed — a flag that does not opt in is system-administrator-only, so a new
sensitive flag can never be opened to org admins by omission.

This surface lists and writes only:

* community-tier flags (a team-tier flag needs a licence, so an org admin can
  never enable one here), AND
* flags whose ``org_admin_settable`` is True, AND
* flags that do not already have a dedicated, better-labelled org control.

``mcp_human_linked_hitl_authority`` is deliberately excluded: it has its own
org-admin control at ``/api/v1/admin/org/mcp-human-linked-hitl-authority`` (and
its own SectionCard on ``/admin/org``), so keeping it off this list preserves
"exactly one UI per flag" and leaves its behaviour untouched.

Writes target the same durable truth every other org flag override writes — the
org ``settings_json.feature_overrides`` map — through the shared atomic patch
helper, invalidate the system feature-flag list cache, and emit the existing
``feature_flag_override_set`` / ``feature_flag_override_cleared`` audit events.

Authz: ``org.feature_flags.manage`` (``admin``) via ``require_permission``, so
the registry remains the single source of the role. The key is non-delegable —
org flag governance is an organisation-governance control, not something a
credential should hold.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from farna.api.constants import MSG_ORGANISATION_NOT_FOUND
from farna.api.db_error_handling import handle_db_errors
from farna.api.dependencies import get_db_session, require_permission, resolve_audit_principal
from farna.api.routes._org_settings import apply_org_settings_patch_or_404
from farna.api.routes.admin_feature_flags import (
    _build_registry,
    _emit_org_flag_override_audit,
    _invalidate_cache,
)
from farna.auth.jwt import TenantPrincipal
from farna.core.audit_coverage import audited
from farna.core.feature_flags import FeatureFlag, feature_overrides_from_settings
from farna.db.crud.organisation import get_organisation
from farna.db.rls import set_rls_org
from farna.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/org/feature-flags", tags=["admin", "feature-flags"])

_PERMISSION_MANAGE = "org.feature_flags.manage"

# Flags with a dedicated, better-labelled org control. They are never listed or
# written by this surface, even if their classification changes later, so there
# is exactly one UI per flag.
_DEDICATED_CONTROL_FLAGS: frozenset[str] = frozenset({"mcp_human_linked_hitl_authority"})

_CODE_LIST_FAILED = "admin_org_feature_flags.list_failed"
_CODE_SET_FAILED = "admin_org_feature_flags.set_failed"
_CODE_CLEAR_FAILED = "admin_org_feature_flags.clear_failed"

_MSG_UNKNOWN_FLAG = "Unknown feature flag: {flag_name}"
_MSG_NOT_ORG_SETTABLE = "Feature flag '{flag_name}' is not settable by an organisation administrator; a system administrator must set it."


class OrgFeatureFlagItem(BaseModel):
    """One org-settable flag, with its effective state for this organisation.

    ``default`` is the registry's computed state for the org's plan (a
    system-level override included) before any org override is applied;
    ``enabled`` is the effective state after the org override; ``overridden``
    reports whether the org has an explicit override, so the UI can distinguish
    "on by default" from "on because someone set it".
    """

    name: str
    description: str
    enabled: bool
    overridden: bool
    default: bool


class OrgFeatureFlagListResponse(BaseModel):
    flags: list[OrgFeatureFlagItem]


class OrgFeatureFlagUpdateRequest(BaseModel):
    enabled: bool


def _is_org_settable(flag: FeatureFlag) -> bool:
    """Whether this flag belongs on the org-admin surface (fail closed)."""
    return flag.name not in _DEDICATED_CONTROL_FLAGS and flag.tier == "community" and flag.org_admin_settable


async def _read_org_overrides(session: AsyncSession, org_id: uuid.UUID) -> dict[str, bool]:
    """Read the org's ``feature_overrides`` map (bool entries only).

    Fails closed: a missing org is a 404 and a read error propagates to the
    caller's typed status arms, so a failed read can never be mistaken for "no
    overrides" and silently report the wrong effective state.
    """
    async with session.begin():
        await set_rls_org(session, org_id)
        org = await get_organisation(session, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_ORGANISATION_NOT_FOUND)
    return feature_overrides_from_settings(getattr(org, "settings_json", None))


async def _resolve_settable_flag(
    settings: Settings,
    session: AsyncSession,
    current_user: TenantPrincipal,
    flag_name: str,
) -> FeatureFlag:
    """Resolve ``flag_name`` to a flag this surface may write, or raise.

    404 for an unknown flag; 403 for a flag that is not settable by an org
    admin (a dedicated-control flag, a team-tier flag, or a community flag that
    did not opt in). A registry read failure propagates so the caller's error
    arms answer a typed status instead of treating the flag as writable.
    """
    registry = await _build_registry(settings, session, current_user)
    flag = registry.get_flag(flag_name)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_UNKNOWN_FLAG.format(flag_name=flag_name),
        )
    if not _is_org_settable(flag):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_MSG_NOT_ORG_SETTABLE.format(flag_name=flag_name),
        )
    return flag


def _item(flag: FeatureFlag, *, enabled: bool, overridden: bool) -> OrgFeatureFlagItem:
    return OrgFeatureFlagItem(
        name=flag.name,
        description=flag.description,
        enabled=enabled,
        overridden=overridden,
        default=flag.currently_active,
    )


@router.get("")
@handle_db_errors(_CODE_LIST_FAILED)
async def list_org_feature_flags(
    current_user: Annotated[TenantPrincipal, require_permission(_PERMISSION_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OrgFeatureFlagListResponse:
    """List the community-tier flags this org admin may set for their own org."""
    registry = await _build_registry(settings, session, current_user)
    overrides = await _read_org_overrides(session, current_user.organisation_id)
    return OrgFeatureFlagListResponse(
        flags=[
            _item(
                flag,
                enabled=overrides.get(flag.name, flag.currently_active),
                overridden=flag.name in overrides,
            )
            for flag in registry.list_flags()
            if _is_org_settable(flag)
        ]
    )


@router.put(
    "/{flag_name}",
    dependencies=[Depends(audited("api_access_put", "org", principal_dep=resolve_audit_principal))],
)
@handle_db_errors(_CODE_SET_FAILED)
async def set_org_feature_flag(
    flag_name: str,
    req: OrgFeatureFlagUpdateRequest,
    current_user: Annotated[TenantPrincipal, require_permission(_PERMISSION_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OrgFeatureFlagItem:
    """Set (or update) this org's override for an org-settable flag."""
    flag = await _resolve_settable_flag(settings, session, current_user, flag_name)
    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await apply_org_settings_patch_or_404(
            session,
            current_user.organisation_id,
            nested_patch={"feature_overrides": {flag_name: req.enabled}},
        )
    await _invalidate_cache(settings, current_user.organisation_id)
    await _emit_org_flag_override_audit(
        session,
        current_user,
        flag_name=flag_name,
        enabled=req.enabled,
    )
    return _item(flag, enabled=req.enabled, overridden=True)


@router.delete(
    "/{flag_name}",
    dependencies=[
        Depends(
            audited(
                "api_access_delete",
                "org",
                principal_dep=resolve_audit_principal,
                fail_closed=True,
            )
        )
    ],
)
@handle_db_errors(_CODE_CLEAR_FAILED)
async def clear_org_feature_flag(
    flag_name: str,
    current_user: Annotated[TenantPrincipal, require_permission(_PERMISSION_MANAGE)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OrgFeatureFlagItem:
    """Clear this org's override for an org-settable flag (revert to default).

    FAR-2309: the clear is atomic. ``nested_delete`` removes ONLY this flag's
    entry from the stored ``feature_overrides`` object inside the same UPDATE
    that composes the settings patch, so two concurrent clears of different
    flags both persist and a concurrent SET of another flag can never be
    clobbered by a stale map. (The earlier shape read the map, popped the key
    and wrote the whole map back — a lost update between the read and the
    write.) Only the one map is touched; no other settings key is rewritten.
    """
    flag = await _resolve_settable_flag(settings, session, current_user, flag_name)
    async with session.begin():
        await set_rls_org(session, current_user.organisation_id)
        await apply_org_settings_patch_or_404(
            session,
            current_user.organisation_id,
            nested_delete={"feature_overrides": [flag_name]},
        )
    await _invalidate_cache(settings, current_user.organisation_id)
    await _emit_org_flag_override_audit(
        session,
        current_user,
        flag_name=flag_name,
        enabled=None,
    )
    return _item(flag, enabled=flag.currently_active, overridden=False)
