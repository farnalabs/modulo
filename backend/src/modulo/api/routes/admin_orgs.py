"""Admin-only routes for cross-tenant organisation management."""

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    deny_break_glass_mint,
    get_db_session,
    require_system_permission,
    require_target_org_role,
)
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.passwords import hash_password, validate_password_strength
from modulo.core.audit_logger import append_audit_event
from modulo.core.seed_data.cost_components import seed_cost_components_for_org
from modulo.db.crud.account import create_account, get_account_by_email
from modulo.db.crud.org_membership import (
    create_membership,
    get_membership_by_account_and_org,
)
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
_CODE_ADMIN_ORGS_SET_ORG_TRIGGERS_PAUSED = "admin_orgs.admin_set_org_triggers_paused"
_CODE_ADMIN_ORGS_SET_ORG_GUARDRAILS_KILL_SWITCH = "admin_orgs.admin_set_org_guardrails_kill_switch"

_MSG_MIGRATIONS_REQUIRED = "Feature is not available. Run database migrations to enable it."

_ALLOWED_ORG_ROLES = ("admin", "operator", "runner", "viewer")

_MSG_EMAIL_ACCOUNT_EXISTS = (
    "EMAIL_ACCOUNT_EXISTS: An account with this email exists"
    " in another organisation. Password-based adoption is not allowed."
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/orgs", tags=["admin"])


# --- Error helpers ---


def _raise_programming_error(code: str, detail: str, exc: ProgrammingError) -> NoReturn:
    """501 -- schema/migration missing (feature not yet available)."""
    logger.exception(code)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=detail,
    ) from exc


def _raise_db_unavailable(code: str, detail: str, exc: SQLAlchemyError) -> NoReturn:
    """503 -- database error while servicing the request."""
    logger.exception(code)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    ) from exc


def _raise_internal_error(code: str) -> NoReturn:
    """500 -- unexpected non-DB error."""
    logger.exception(code)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=MSG_INTERNAL_SERVER_ERROR,
    ) from None


# --- Org response builders ---


def _create_org_response(org: Organisation) -> "CreateOrgResponse":
    return CreateOrgResponse(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        status=org.status,
        created_at=org.created_at.isoformat(),
    )


def _list_org_item(org: Organisation) -> "ListOrgItem":
    return ListOrgItem(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        plan_id=org.plan_id,
        status=org.status,
        created_at=org.created_at.isoformat(),
    )


# --- Org-scoped org fetch (set RLS, then load, 404 if absent) ---


async def _load_org_in_org_scope(session: AsyncSession, org_id: uuid.UUID) -> Organisation:
    """Set RLS to the target org, then load the org or 404.

    ``set_rls_org`` must run in the OUTER transaction BEFORE any read -- SET
    LOCAL is reverted by a savepoint rollback.
    """
    await set_rls_org(session, org_id)
    org = await get_organisation(session, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)
    return org


async def _append_fail_open_audit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    current_user: AuthenticatedPrincipal,
    event_type: str,
    payload: dict[str, Any],
    log_prefix: str,
) -> None:
    """Append an audit event fail-open-with-alert: the toggle ALWAYS commits.

    A failed audit write is loudly logged and never rolls back the caller's
    mutation.
    """
    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=event_type,
            actor_user_id=current_user.user_id,
            payload_json=payload,
        )
    except SQLAlchemyError:
        logger.exception("%s audit write failed", log_prefix)
    except Exception:
        logger.exception("%s audit write failed (non-DB)", log_prefix)


def _timestamp_response(field: Any) -> str | None:
    return field.isoformat() if field else None


# --- Create Org ---


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


@router.post("", status_code=status.HTTP_201_CREATED)
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
            # block org creation -- log it loudly instead.
            try:
                await seed_cost_components_for_org(session, org.id)
            except Exception:
                logger.exception("admin_orgs.cost_components_seed_failed")

            return _create_org_response(org)
    except ProgrammingError as exc:
        _raise_programming_error("admin_orgs.admin_create_org", MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable("admin_orgs.admin_create_org", "Database error while creating organisation.", exc)
    except HTTPException:
        raise
    except Exception:
        _raise_internal_error("Unexpected error in admin_create_org")


# --- List Orgs ---


class ListOrgItem(BaseModel):
    id: str
    name: str
    slug: str
    plan_id: str | None = None
    status: str
    created_at: str


@router.get("")
@handle_db_errors("admin.orgs.admin_list_orgs")
async def admin_list_orgs(
    _: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_ORG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> list[ListOrgItem]:
    try:
        async with session.begin():
            orgs = await list_organisations(session)
    except ProgrammingError as exc:
        _raise_programming_error("admin_orgs.admin_list_orgs", MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable("admin_orgs.admin_list_orgs", "Database error while listing organisations.", exc)
    except HTTPException:
        raise
    except Exception:
        _raise_internal_error("Unexpected error in admin_list_orgs")
    return [_list_org_item(o) for o in orgs]


# --- Create User in Org ---


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


def _validate_org_role(role: str) -> None:
    if role not in _ALLOWED_ORG_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid role: {role}. Must be one of: admin, operator, runner, viewer",
        )


def _validate_password(req: CreateOrgUserRequest) -> None:
    try:
        validate_password_strength(req.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


async def _existing_membership_conflict(session: AsyncSession, existing: Any, org_id: uuid.UUID) -> None:
    """Guard against cross-tenant account takeover (SECURITY #1189).

    If the email already belongs to an account, refuse when the account is
    already in this org, or when it is a local account with an existing
    password (password hash overwrite would adopt it cross-tenant).
    """
    if existing is None:
        return

    membership = await get_membership_by_account_and_org(session, existing.id, org_id)
    if membership is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists in this organisation",
        )
    # SECURITY (#1189): refuse password hash overwrite when the account
    # belongs to other orgs -- prevents cross-tenant takeover.
    if existing.password_hash is not None and existing.auth_provider == "local":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_EMAIL_ACCOUNT_EXISTS,
        )


async def _resolve_account(
    session: AsyncSession,
    req: CreateOrgUserRequest,
    pw_hash: str,
    existing: Any,
) -> Any:
    """Reuse the account when safe (SSO/SCIM JIT), otherwise create a new one."""
    if existing is not None:
        # SECURITY (#1189): only allow password hash overwrite for accounts
        # that have NO existing password (SSO/SCIM JIT).
        if existing.password_hash is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_MSG_EMAIL_ACCOUNT_EXISTS,
            )
        existing.password_hash = pw_hash
        return existing

    return await create_account(
        session,
        email=req.email,
        display_name=req.display_name,
        password_hash=pw_hash,
    )


def _create_org_user_response(account: Any, membership: Any) -> "CreateOrgUserResponse":
    return CreateOrgUserResponse(
        id=str(account.id),
        email=account.email,
        display_name=account.display_name,
        org_role=membership.role,
        auth_provider=account.auth_provider,
        created_at=account.created_at.isoformat(),
    )


@router.post("/{org_id}/users", status_code=status.HTTP_201_CREATED)
@handle_db_errors("admin.orgs.admin_create_org_user")
async def admin_create_org_user(
    org_id: uuid.UUID,
    req: CreateOrgUserRequest,
    _current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_ORG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> CreateOrgUserResponse:
    _validate_org_role(req.org_role)
    _validate_password(req)

    try:
        async with session.begin():
            org = await get_organisation(session, org_id)
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_ORGANISATION_NOT_FOUND,
                )

            existing = await get_account_by_email(session, req.email)
            await _existing_membership_conflict(session, existing, org_id)

            pw_hash = hash_password(req.password)
            account = await _resolve_account(session, req, pw_hash, existing)

            membership = await create_membership(
                session,
                account_id=account.id,
                org_id=org_id,
                role=req.org_role,
            )

            return _create_org_user_response(account, membership)
    except ProgrammingError as exc:
        _raise_programming_error("admin_orgs.admin_create_org_user", MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable("admin_orgs.admin_create_org_user", "Database error while creating org user.", exc)
    except HTTPException:
        raise
    except Exception:
        _raise_internal_error("Unexpected error in admin_create_org_user")


# --- Delete Org ---


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
        _raise_programming_error("admin_orgs.admin_delete_org", MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable("admin_orgs.admin_delete_org", "Database error while deleting organisation.", exc)
    except HTTPException:
        raise
    except Exception:
        _raise_internal_error("Unexpected error in admin_delete_org")


# --- Org License Management ---


class OrgLicenseResponse(BaseModel):
    has_license: bool
    tier: str = "community"
    features: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    org_id: str | None = None


class SetOrgLicenseRequest(BaseModel):
    license_key: str = Field(min_length=1)


def _license_response_from_data(d: Any) -> OrgLicenseResponse:
    return OrgLicenseResponse(
        has_license=True,
        tier=d.tier,
        features=d.features,
        expires_at=d.expires_at or None,
        org_id=d.org_id or None,
    )


def _resolve_org_license(org: Organisation) -> OrgLicenseResponse:
    """Resolve the effective license for an org: org key first, then system."""
    from modulo.core.license import get_license as get_sys_license
    from modulo.core.license import parse_and_verify

    org_key = org.settings_json.get("license_key") if org.settings_json else None
    if org_key:
        validation = parse_and_verify(org_key)
        if validation.valid and validation.license_data is not None:
            return _license_response_from_data(validation.license_data)

    lic = get_sys_license()
    if lic is not None:
        return _license_response_from_data(lic)

    return OrgLicenseResponse(has_license=False)


@router.get("/{org_id}/license")
@handle_db_errors("admin.orgs.admin_get_org_license")
async def admin_get_org_license(
    org_id: uuid.UUID,
    _: AuthenticatedPrincipal = require_target_org_role("org.license.view", "operator"),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> OrgLicenseResponse:
    try:
        org = await get_organisation(session, org_id)
    except ProgrammingError as exc:
        _raise_programming_error("admin_orgs.admin_get_org_license", MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable("admin_orgs.admin_get_org_license", "Database error while fetching org license.", exc)
    except HTTPException:
        raise
    except Exception:
        _raise_internal_error("Unexpected error in admin_get_org_license")
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

    return _resolve_org_license(org)


def _verify_license_key(req: SetOrgLicenseRequest) -> Any:
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
    return validation.license_data


def _set_org_license_key(settings_json: dict[str, Any], license_key: str) -> dict[str, Any]:
    merged = dict(settings_json)
    merged["license_key"] = license_key
    return merged


def _clear_org_license_key(settings_json: dict[str, Any]) -> dict[str, Any]:
    merged = dict(settings_json)
    merged.pop("license_key", None)
    return merged


@router.put("/{org_id}/license")
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
        _raise_programming_error(_CODE_ADMIN_ORGS_ADMIN_SET, MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable(_CODE_ADMIN_ORGS_ADMIN_SET, "Database error while fetching org for set-license.", exc)
    except HTTPException:
        raise
    except Exception:
        _raise_internal_error("Unexpected error in admin_set_org_license (fetch)")
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

    d = _verify_license_key(req)

    settings_json = _set_org_license_key(org.settings_json or {}, req.license_key)

    try:
        await update_organisation(session, org_id, {"settings_json": settings_json})
    except ProgrammingError as exc:
        _raise_programming_error(_CODE_ADMIN_ORGS_ADMIN_SET, MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable(_CODE_ADMIN_ORGS_ADMIN_SET, "Database error while updating org license.", exc)
    except HTTPException:
        raise
    except Exception:
        _raise_internal_error("Unexpected error in admin_set_org_license (update)")

    return _license_response_from_data(d)


@router.delete("/{org_id}/license")
@handle_db_errors("admin.orgs.admin_remove_org_license")
async def admin_remove_org_license(
    org_id: uuid.UUID,
    _: AuthenticatedPrincipal = require_target_org_role("org.license.manage", "admin"),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> OrgLicenseResponse:
    try:
        org = await get_organisation(session, org_id)
    except ProgrammingError as exc:
        _raise_programming_error(_CODE_ADMIN_ORGS_ADMIN_REMOVE, MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable(
            _CODE_ADMIN_ORGS_ADMIN_REMOVE, "Database error while fetching org for remove-license.", exc
        )
    except HTTPException:
        raise
    except Exception:
        _raise_internal_error("Unexpected error in admin_remove_org_license (fetch)")
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

    settings_json = _clear_org_license_key(org.settings_json or {})

    try:
        await update_organisation(session, org_id, {"settings_json": settings_json})
    except ProgrammingError as exc:
        _raise_programming_error(_CODE_ADMIN_ORGS_ADMIN_REMOVE, MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable(_CODE_ADMIN_ORGS_ADMIN_REMOVE, "Database error while removing org license.", exc)
    except HTTPException:
        raise
    except Exception:
        _raise_internal_error("Unexpected error in admin_remove_org_license (remove)")

    return OrgLicenseResponse(has_license=False)


# --- Org Authz Kill Switch ---


class SetOrgAuthzEnforceRequest(BaseModel):
    enforce: bool


class SetOrgAuthzEnforceResponse(BaseModel):
    org_id: str
    enforce: bool


@router.patch("/{org_id}/authz-enforce")
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

    # Atomic at statement level -- a dedicated boolean column, no read-modify-write.
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
        _raise_programming_error("admin_orgs.admin_set_org_authz_enforce", MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable(
            "admin_orgs.admin_set_org_authz_enforce", "Database error while updating org authz-enforce.", exc
        )
    except Exception:
        _raise_internal_error("Unexpected error in admin_set_org_authz_enforce")

    if affected == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

    return SetOrgAuthzEnforceResponse(org_id=str(org_id), enforce=req.enforce)


# --- Org-wide "pause all pipeline triggers" kill-switch ---


class SetOrgTriggersPausedRequest(BaseModel):
    paused: bool


class SetOrgTriggersPausedResponse(BaseModel):
    paused: bool
    paused_at: str | None


@router.put("/{org_id}/triggers/pause")
@handle_db_errors("admin.orgs.admin_set_org_triggers_paused")
async def admin_set_org_triggers_paused(
    org_id: uuid.UUID,
    req: SetOrgTriggersPausedRequest,
    # Tenancy-bounded (ADR 017 DECISION 3 scope pin): the authz kill-switch must
    # NOT be able to lift this gate -- ``kill_switch_eligible=False`` mirrors the
    # org.delete immunity in ``require_system_or_org_admin``.
    current_user: AuthenticatedPrincipal = require_target_org_role(  # type: ignore[assignment]
        "org.triggers.pause.manage", "admin", kill_switch_eligible=False
    ),
    session: AsyncSession = Depends(get_db_session),
) -> SetOrgTriggersPausedResponse:
    try:
        async with session.begin():
            org = await _load_org_in_org_scope(session, org_id)

            # Idempotency: toggling to the current state is a no-op (no audit write).
            if org.triggers_paused == req.paused:
                return SetOrgTriggersPausedResponse(
                    paused=org.triggers_paused,
                    paused_at=_timestamp_response(org.triggers_paused_at),
                )

            org.triggers_paused = req.paused
            org.triggers_paused_at = datetime.now(UTC) if req.paused else None
            await session.flush()

            await _append_fail_open_audit(
                session,
                org_id=org_id,
                current_user=current_user,
                event_type="triggers_paused",
                payload={"paused": req.paused},
                log_prefix="admin_orgs.admin_set_org_triggers_paused",
            )

            return SetOrgTriggersPausedResponse(
                paused=org.triggers_paused,
                paused_at=_timestamp_response(org.triggers_paused_at),
            )
    except ProgrammingError as exc:
        _raise_programming_error(_CODE_ADMIN_ORGS_SET_ORG_TRIGGERS_PAUSED, MSG_FEATURE_NOT_AVAILABLE, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable(
            _CODE_ADMIN_ORGS_SET_ORG_TRIGGERS_PAUSED,
            "Database error while updating org trigger pause state.",
            exc,
        )
    except HTTPException as exc:
        raise exc
    except Exception:
        _raise_internal_error("Unexpected error in admin_set_org_triggers_paused")


# --- Guardrails org-wide kill-switch (FAR-223 item 9) ---


class GetOrgGuardrailsKillSwitchResponse(BaseModel):
    enabled: bool
    enabled_at: str | None


class SetOrgGuardrailsKillSwitchRequest(BaseModel):
    enabled: bool


class SetOrgGuardrailsKillSwitchResponse(BaseModel):
    enabled: bool
    enabled_at: str | None


@router.get(
    "/{org_id}/guardrails/kill-switch",
    # FAR-309 PR B org-global invariant: the kill-switch is the org-global
    # guardrail safety control -- a break-glass account must never be able to
    # disable it (or read it) even though it is an admin-scoped endpoint.
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("admin.orgs.get_org_guardrails_kill_switch")
async def admin_get_org_guardrails_kill_switch(
    org_id: uuid.UUID,
    _current_user: AuthenticatedPrincipal = require_target_org_role(  # type: ignore[assignment]
        "org.guardrails.kill_switch.manage", "admin", kill_switch_eligible=False
    ),
    session: AsyncSession = Depends(get_db_session),
) -> GetOrgGuardrailsKillSwitchResponse:
    """Read the org's guardrails kill-switch state (admin only)."""
    try:
        async with session.begin():
            org = await _load_org_in_org_scope(session, org_id)
            return GetOrgGuardrailsKillSwitchResponse(
                enabled=bool(org.guardrails_kill_switch),
                enabled_at=_timestamp_response(org.guardrails_kill_switch_at),
            )
    except ProgrammingError as exc:
        _raise_programming_error("admin_orgs.admin_get_org_guardrails_kill_switch", _MSG_MIGRATIONS_REQUIRED, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable(
            "admin_orgs.admin_get_org_guardrails_kill_switch",
            "Database error while reading org guardrails kill-switch state.",
            exc,
        )
    except HTTPException as exc:
        raise exc
    except Exception:
        _raise_internal_error("Unexpected error in admin_get_org_guardrails_kill_switch")


@router.put(
    "/{org_id}/guardrails/kill-switch",
    # FAR-309 PR B org-global invariant: the kill-switch is the org-global
    # guardrail safety control -- a break-glass account must NEVER be able to
    # disable it (or read it) even though it is an admin-scoped endpoint.
    dependencies=[Depends(deny_break_glass_mint)],
)
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
    start -- never a full disable. Enabling fires an audit event AND a
    paging Notification (``guardrail_kill_switch``) so the downgrade is never
    silent. Disabling restores full enforcement.
    """
    try:
        async with session.begin():
            org = await _load_org_in_org_scope(session, org_id)

            # Idempotency: toggling to the current state is a no-op (no audit write).
            if bool(org.guardrails_kill_switch) == req.enabled:
                return SetOrgGuardrailsKillSwitchResponse(
                    enabled=bool(org.guardrails_kill_switch),
                    enabled_at=_timestamp_response(org.guardrails_kill_switch_at),
                )

            org.guardrails_kill_switch = req.enabled
            org.guardrails_kill_switch_at = datetime.now(UTC) if req.enabled else None
            await session.flush()

            await _append_fail_open_audit(
                session,
                org_id=org_id,
                current_user=current_user,
                event_type="guardrails_kill_switch",
                payload={"enabled": req.enabled},
                log_prefix="admin_orgs.admin_set_org_guardrails_kill_switch",
            )

            if req.enabled:
                # Alert on enable -- the downgrade-to-observe is never silent.
                from modulo.core.guardrails import notify_guardrail_event

                await notify_guardrail_event(
                    org_id,
                    "guardrail_kill_switch",
                    {"org_id": str(org_id), "enabled": True},
                )

            return SetOrgGuardrailsKillSwitchResponse(
                enabled=org.guardrails_kill_switch,
                enabled_at=_timestamp_response(org.guardrails_kill_switch_at),
            )
    except ProgrammingError as exc:
        _raise_programming_error(_CODE_ADMIN_ORGS_SET_ORG_GUARDRAILS_KILL_SWITCH, _MSG_MIGRATIONS_REQUIRED, exc)
    except SQLAlchemyError as exc:
        _raise_db_unavailable(
            _CODE_ADMIN_ORGS_SET_ORG_GUARDRAILS_KILL_SWITCH,
            "Database error while updating org guardrails kill-switch state.",
            exc,
        )
    except HTTPException as exc:
        raise exc
    except Exception:
        _raise_internal_error("Unexpected error in admin_set_org_guardrails_kill_switch")
