"""Admin-only routes for cross-tenant organisation management."""

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_system_permission, require_target_org_role
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.passwords import hash_password, validate_password_strength
from modulo.core.audit_logger import append_audit_event
from modulo.core.seed_data.cost_components import seed_cost_components_for_org
from modulo.db.crud.account import create_account, get_account_by_email
from modulo.db.crud.org_membership import create_membership
from modulo.db.crud.organisation import (
    create_organisation,
    delete_organisation,
    get_organisation,
    get_organisation_by_slug,
    list_organisations,
    update_organisation,
)
from modulo.db.models.organisation import Organisation
from modulo.db.rls import set_rls_org

_CODE_SYSTEM_ORG_MANAGE = "system.org.manage"
_MSG_ORGANISATION_NOT_FOUND = "Organisation not found"
_CODE_ADMIN_ORGS_ADMIN_SET = "admin_orgs.admin_set_org_license"
_CODE_ADMIN_ORGS_ADMIN_REMOVE = "admin_orgs.admin_remove_org_license"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/orgs", tags=["admin"])


# ── Create Org ──────────────────────────────────────────────────────────


class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9-]+$",
    )
    plan_id: str | None = None


class CreateOrgResponse(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    created_at: str


@router.post("", response_model=CreateOrgResponse, status_code=status.HTTP_201_CREATED)
@handle_db_errors("admin.orgs.admin_create_org")
async def admin_create_org(
    req: CreateOrgRequest,
    current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_ORG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> CreateOrgResponse:
    try:
        async with session.begin():
            existing = await get_organisation_by_slug(session, req.slug)
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"An organisation with slug '{req.slug}' already exists",
                )

            org = await create_organisation(
                session,
                name=req.name,
                slug=req.slug,
                plan_id=req.plan_id,
                created_by=current_user.account_id,
            )

            # Seed default cost components for the new org in the SAME
            # transaction (idempotent). Fail-open: a seed failure must never
            # block org creation — log it loudly instead.
            try:
                await seed_cost_components_for_org(session, org.id)
            except Exception:
                logger.exception("admin_orgs.cost_components_seed_failed")

            return CreateOrgResponse(
                id=str(org.id),
                name=org.name,
                slug=org.slug,
                status=org.status,
                created_at=org.created_at.isoformat(),
            )
    except ProgrammingError as exc:
        logger.exception("admin_orgs.admin_create_org")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("admin_orgs.admin_create_org")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while creating organisation.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_create_org")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


# ── List Orgs ──────────────────────────────────────────────────────────


class ListOrgItem(BaseModel):
    id: str
    name: str
    slug: str
    plan_id: str | None = None
    status: str
    created_at: str


@router.get("", response_model=list[ListOrgItem])
@handle_db_errors("admin.orgs.admin_list_orgs")
async def admin_list_orgs(
    _: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_ORG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> list[ListOrgItem]:
    try:
        async with session.begin():
            orgs = await list_organisations(session)
    except ProgrammingError as exc:
        logger.exception("admin_orgs.admin_list_orgs")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("admin_orgs.admin_list_orgs")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while listing organisations.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_list_orgs")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    return [
        ListOrgItem(
            id=str(o.id),
            name=o.name,
            slug=o.slug,
            plan_id=o.plan_id,
            status=o.status,
            created_at=o.created_at.isoformat(),
        )
        for o in orgs
    ]


# ── Create User in Org ─────────────────────────────────────────────────


class CreateOrgUserRequest(BaseModel):
    email: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    password: str = Field(min_length=8)
    org_role: str = Field(default="runner")


class CreateOrgUserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    org_role: str
    auth_provider: str
    created_at: str


@router.post("/{org_id}/users", response_model=CreateOrgUserResponse, status_code=status.HTTP_201_CREATED)
@handle_db_errors("admin.orgs.admin_create_org_user")
async def admin_create_org_user(
    org_id: uuid.UUID,
    req: CreateOrgUserRequest,
    current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_ORG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> CreateOrgUserResponse:
    if req.org_role not in ("admin", "operator", "runner", "viewer"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid role: {req.org_role}. Must be one of: admin, operator, runner, viewer",
        )

    try:
        validate_password_strength(req.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    try:
        async with session.begin():
            org = await get_organisation(session, org_id)
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_ORGANISATION_NOT_FOUND,
                )

            existing = await get_account_by_email(session, req.email)
            if existing is not None:
                from modulo.db.crud.org_membership import get_membership_by_account_and_org

                membership = await get_membership_by_account_and_org(session, existing.id, org_id)
                if membership is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A user with this email already exists in this organisation",
                    )

            pw_hash = hash_password(req.password)

            if existing is not None:
                account = existing
                account.password_hash = pw_hash
            else:
                account = await create_account(
                    session,
                    email=req.email,
                    display_name=req.display_name,
                    password_hash=pw_hash,
                )

            membership = await create_membership(
                session,
                account_id=account.id,
                org_id=org_id,
                role=req.org_role,
            )

            return CreateOrgUserResponse(
                id=str(account.id),
                email=account.email,
                display_name=account.display_name,
                org_role=membership.role,
                auth_provider=account.auth_provider,
                created_at=account.created_at.isoformat(),
            )
    except ProgrammingError as exc:
        logger.exception("admin_orgs.admin_create_org_user")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("admin_orgs.admin_create_org_user")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while creating org user.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_create_org_user")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


# ── Delete Org ─────────────────────────────────────────────────────────


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("admin.orgs.admin_delete_org")
async def admin_delete_org(
    org_id: uuid.UUID,
    _: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_ORG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> None:
    try:
        async with session.begin():
            org = await get_organisation(session, org_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

            deleted = await delete_organisation(session, org_id)
            if not deleted:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)
    except ProgrammingError as exc:
        logger.exception("admin_orgs.admin_delete_org")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("admin_orgs.admin_delete_org")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while deleting organisation.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_delete_org")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


# ── Org License Management ──────────────────────────────────────────────


class OrgLicenseResponse(BaseModel):
    has_license: bool
    tier: str = "community"
    features: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    org_id: str | None = None


class SetOrgLicenseRequest(BaseModel):
    license_key: str = Field(min_length=1)


@router.get("/{org_id}/license", response_model=OrgLicenseResponse)
@handle_db_errors("admin.orgs.admin_get_org_license")
async def admin_get_org_license(
    org_id: uuid.UUID,
    _: AuthenticatedPrincipal = require_target_org_role("org.license.view", "operator"),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> OrgLicenseResponse:
    try:
        org = await get_organisation(session, org_id)
    except ProgrammingError as exc:
        logger.exception("admin_orgs.admin_get_org_license")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("admin_orgs.admin_get_org_license")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching org license.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_get_org_license")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

    from modulo.core.license import get_license as get_sys_license
    from modulo.core.license import parse_and_verify

    org_key = org.settings_json.get("license_key") if org.settings_json else None
    if org_key:
        validation = parse_and_verify(org_key)
        if validation.valid and validation.license_data is not None:
            d = validation.license_data
            return OrgLicenseResponse(
                has_license=True,
                tier=d.tier,
                features=d.features,
                expires_at=d.expires_at or None,
                org_id=d.org_id or None,
            )

    lic = get_sys_license()
    if lic is not None:
        return OrgLicenseResponse(
            has_license=True,
            tier=lic.tier,
            features=lic.features,
            expires_at=lic.expires_at or None,
            org_id=lic.org_id or None,
        )

    return OrgLicenseResponse(has_license=False)


@router.put("/{org_id}/license", response_model=OrgLicenseResponse)
@handle_db_errors("admin.orgs.admin_set_org_license")
async def admin_set_org_license(
    org_id: uuid.UUID,
    req: SetOrgLicenseRequest,
    _: AuthenticatedPrincipal = require_target_org_role("org.license.manage", "admin"),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> OrgLicenseResponse:
    try:
        org = await get_organisation(session, org_id)
    except ProgrammingError as exc:
        logger.exception(_CODE_ADMIN_ORGS_ADMIN_SET)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_ADMIN_ORGS_ADMIN_SET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching org for set-license.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_set_org_license (fetch)")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

    from modulo.core.license import parse_and_verify

    try:
        validation = parse_and_verify(req.license_key)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(_CODE_ADMIN_ORGS_ADMIN_SET)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    if not validation.valid or validation.license_data is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=validation.error or "Invalid license key",
        )

    settings_json = dict(org.settings_json or {})
    settings_json["license_key"] = req.license_key

    try:
        await update_organisation(session, org_id, {"settings_json": settings_json})
    except ProgrammingError as exc:
        logger.exception(_CODE_ADMIN_ORGS_ADMIN_SET)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_ADMIN_ORGS_ADMIN_SET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating org license.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_set_org_license (update)")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    d = validation.license_data
    return OrgLicenseResponse(
        has_license=True,
        tier=d.tier,
        features=d.features,
        expires_at=d.expires_at or None,
        org_id=d.org_id or None,
    )


@router.delete("/{org_id}/license", response_model=OrgLicenseResponse)
@handle_db_errors("admin.orgs.admin_remove_org_license")
async def admin_remove_org_license(
    org_id: uuid.UUID,
    _: AuthenticatedPrincipal = require_target_org_role("org.license.manage", "admin"),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> OrgLicenseResponse:
    try:
        org = await get_organisation(session, org_id)
    except ProgrammingError as exc:
        logger.exception(_CODE_ADMIN_ORGS_ADMIN_REMOVE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_ADMIN_ORGS_ADMIN_REMOVE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching org for remove-license.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_remove_org_license (fetch)")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

    settings_json = dict(org.settings_json or {})
    settings_json.pop("license_key", None)

    try:
        await update_organisation(session, org_id, {"settings_json": settings_json})
    except ProgrammingError as exc:
        logger.exception(_CODE_ADMIN_ORGS_ADMIN_REMOVE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception(_CODE_ADMIN_ORGS_ADMIN_REMOVE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while removing org license.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_remove_org_license (remove)")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return OrgLicenseResponse(has_license=False)


# ── Org Authz Kill Switch ───────────────────────────────────────────────


class SetOrgAuthzEnforceRequest(BaseModel):
    enforce: bool


class SetOrgAuthzEnforceResponse(BaseModel):
    org_id: str
    enforce: bool


@router.patch("/{org_id}/authz-enforce", response_model=SetOrgAuthzEnforceResponse)
@handle_db_errors("admin.orgs.admin_set_org_authz_enforce")
async def admin_set_org_authz_enforce(
    org_id: uuid.UUID,
    req: SetOrgAuthzEnforceRequest,
    _: AuthenticatedPrincipal = require_target_org_role("org.authz_enforce.manage", "admin"),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> SetOrgAuthzEnforceResponse:
    # Tenancy-bounded (ADR 017 DECISION 3): only the org's own admin (or a
    # system admin) may flip the flag, and only for their org. Flipping org A
    # never affects org B.

    # Atomic at statement level — a dedicated boolean column, no read-modify-write.
    affected = 0
    try:
        async with session.begin():
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    update(Organisation).where(Organisation.id == org_id).values(authz_enforce=req.enforce)
                ),
            )
            affected = result.rowcount or 0
    except ProgrammingError as exc:
        logger.exception("admin_orgs.admin_set_org_authz_enforce")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("admin_orgs.admin_set_org_authz_enforce")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating org authz-enforce.",
        ) from exc
    except Exception:
        logger.exception("Unexpected error in admin_set_org_authz_enforce")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    if affected == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

    return SetOrgAuthzEnforceResponse(org_id=str(org_id), enforce=req.enforce)


# ── Org-wide "pause all pipeline triggers" kill-switch ───────────────────────


class SetOrgTriggersPausedRequest(BaseModel):
    paused: bool


class SetOrgTriggersPausedResponse(BaseModel):
    paused: bool
    paused_at: str | None


@router.put("/{org_id}/triggers/pause", response_model=SetOrgTriggersPausedResponse)
@handle_db_errors("admin.orgs.admin_set_org_triggers_paused")
async def admin_set_org_triggers_paused(
    org_id: uuid.UUID,
    req: SetOrgTriggersPausedRequest,
    # Tenancy-bounded (ADR 017 DECISION 3 scope pin): the authz kill-switch must
    # NOT be able to lift this gate — ``kill_switch_eligible=False`` mirrors the
    # org.delete immunity in ``require_system_or_org_admin``.
    current_user: AuthenticatedPrincipal = require_target_org_role(  # type: ignore[assignment]
        "org.triggers.pause.manage", "admin", kill_switch_eligible=False
    ),
    session: AsyncSession = Depends(get_db_session),
) -> SetOrgTriggersPausedResponse:
    try:
        async with session.begin():
            # set_rls_org must run in the OUTER transaction BEFORE the audit
            # append — SET LOCAL is reverted by a savepoint rollback.
            await set_rls_org(session, org_id)
            org = await get_organisation(session, org_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

            # Idempotency: toggling to the current state is a no-op (no audit write).
            if org.triggers_paused == req.paused:
                paused_at = org.triggers_paused_at.isoformat() if org.triggers_paused_at else None
                return SetOrgTriggersPausedResponse(paused=org.triggers_paused, paused_at=paused_at)

            org.triggers_paused = req.paused
            org.triggers_paused_at = datetime.now(UTC) if req.paused else None
            await session.flush()

            # Audit is fail-open-with-alert: the toggle ALWAYS commits; a failed
            # audit write is loudly logged and never rolls back the toggle.
            try:
                await append_audit_event(
                    session,
                    org_id=org_id,
                    event_type="triggers_paused",
                    actor_user_id=current_user.user_id,
                    payload_json={"paused": req.paused},
                )
            except SQLAlchemyError:
                logger.exception("admin_orgs.admin_set_org_triggers_paused audit write failed")
            except Exception:
                logger.exception("admin_orgs.admin_set_org_triggers_paused audit write failed (non-DB)")

            return SetOrgTriggersPausedResponse(
                paused=org.triggers_paused,
                paused_at=org.triggers_paused_at.isoformat() if org.triggers_paused_at else None,
            )
    except ProgrammingError as exc:
        logger.exception("admin_orgs.admin_set_org_triggers_paused")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("admin_orgs.admin_set_org_triggers_paused")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating org trigger pause state.",
        ) from exc
    except HTTPException as exc:
        raise exc
    except Exception:
        logger.exception("Unexpected error in admin_set_org_triggers_paused")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


# ── Guardrails org-wide kill-switch (FAR-223 item 9) ─────────────────────────


class GetOrgGuardrailsKillSwitchResponse(BaseModel):
    enabled: bool
    enabled_at: str | None


class SetOrgGuardrailsKillSwitchRequest(BaseModel):
    enabled: bool


class SetOrgGuardrailsKillSwitchResponse(BaseModel):
    enabled: bool
    enabled_at: str | None


@router.get("/{org_id}/guardrails/kill-switch", response_model=GetOrgGuardrailsKillSwitchResponse)
@handle_db_errors("admin.orgs.get_org_guardrails_kill_switch")
async def admin_get_org_guardrails_kill_switch(
    org_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = require_target_org_role(  # type: ignore[assignment]
        "org.guardrails.kill_switch.manage", "admin", kill_switch_eligible=False
    ),
    session: AsyncSession = Depends(get_db_session),
) -> GetOrgGuardrailsKillSwitchResponse:
    """Read the org's guardrails kill-switch state (admin only)."""
    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            org = await get_organisation(session, org_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
            return GetOrgGuardrailsKillSwitchResponse(
                enabled=bool(org.guardrails_kill_switch),
                enabled_at=org.guardrails_kill_switch_at.isoformat() if org.guardrails_kill_switch_at else None,
            )
    except ProgrammingError as exc:
        logger.exception("admin_orgs.admin_get_org_guardrails_kill_switch")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("admin_orgs.admin_get_org_guardrails_kill_switch")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while reading org guardrails kill-switch state.",
        ) from exc
    except HTTPException as exc:
        raise exc
    except Exception:
        logger.exception("Unexpected error in admin_get_org_guardrails_kill_switch")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


@router.put("/{org_id}/guardrails/kill-switch", response_model=SetOrgGuardrailsKillSwitchResponse)
@handle_db_errors("admin.orgs.admin_set_org_guardrails_kill_switch")
async def admin_set_org_guardrails_kill_switch(
    org_id: uuid.UUID,
    req: SetOrgGuardrailsKillSwitchRequest,
    current_user: AuthenticatedPrincipal = require_target_org_role(  # type: ignore[assignment]
        "org.guardrails.kill_switch.manage", "admin", kill_switch_eligible=False
    ),
    session: AsyncSession = Depends(get_db_session),
) -> SetOrgGuardrailsKillSwitchResponse:
    """Set the org's guardrails kill-switch (admin only).

    Enabling downgrades every bound guardrail to observe (shadow-only) at run
    start — never a full disable. Enabling fires an audit event AND a
    paging Notification (``guardrail_kill_switch``) so the downgrade is never
    silent. Disabling restores full enforcement.
    """
    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            org = await get_organisation(session, org_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

            # Idempotency: toggling to the current state is a no-op (no audit write).
            if bool(org.guardrails_kill_switch) == req.enabled:
                return SetOrgGuardrailsKillSwitchResponse(
                    enabled=bool(org.guardrails_kill_switch),
                    enabled_at=org.guardrails_kill_switch_at.isoformat() if org.guardrails_kill_switch_at else None,
                )

            org.guardrails_kill_switch = req.enabled
            org.guardrails_kill_switch_at = datetime.now(UTC) if req.enabled else None
            await session.flush()

            # Audit is fail-open-with-alert: the toggle ALWAYS commits; a failed
            # audit write is loudly logged and never rolls back the toggle.
            try:
                await append_audit_event(
                    session,
                    org_id=org_id,
                    event_type="guardrails_kill_switch",
                    actor_user_id=current_user.user_id,
                    payload_json={"enabled": req.enabled},
                )
            except SQLAlchemyError:
                logger.exception("admin_orgs.admin_set_org_guardrails_kill_switch audit write failed")
            except Exception:
                logger.exception("admin_orgs.admin_set_org_guardrails_kill_switch audit write failed (non-DB)")

            if req.enabled:
                # Alert on enable — the downgrade-to-observe is never silent.
                from modulo.core.guardrails import notify_guardrail_event

                await notify_guardrail_event(
                    org_id,
                    "guardrail_kill_switch",
                    {"org_id": str(org_id), "enabled": True},
                )

            return SetOrgGuardrailsKillSwitchResponse(
                enabled=org.guardrails_kill_switch,
                enabled_at=org.guardrails_kill_switch_at.isoformat() if org.guardrails_kill_switch_at else None,
            )
    except ProgrammingError as exc:
        logger.exception("admin_orgs.admin_set_org_guardrails_kill_switch")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("admin_orgs.admin_set_org_guardrails_kill_switch")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating org guardrails kill-switch state.",
        ) from exc
    except HTTPException as exc:
        raise exc
    except Exception:
        logger.exception("Unexpected error in admin_set_org_guardrails_kill_switch")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
