import asyncio
import logging
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    deny_break_glass_mint,
    get_db_session,
    require_feature,
    require_target_org_role,
)
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.secret_storage import decode_stored_secret_scoped, encrypt_stored_secret
from modulo.core.email_service import (
    EmailSendingError,
    EmailSendLimiter,
    _effective_timeout,
    _is_valid_recipient,
    send_email,
)
from modulo.db.crud.organisation import get_organisation, update_organisation
from modulo.settings import Settings, get_settings

_MSG_ORGANISATION_NOT_FOUND = "Organisation not found"
_CODE_ADMIN_EMAIL_ADMIN_UPDATE = "admin_email.admin_update_email_settings"
_CODE_ADMIN_EMAIL_ADMIN_TEST = "admin_email.admin_test_email_settings"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/org", tags=["admin"])

# The test-send endpoint relays mail to an arbitrary recipient, so it is a
# potential SMTP-relay abuse vector (see the product map's "test-send relay
# abuse" gap). Per-org budget: 3 test emails per rolling 60-minute window.
test_send_limiter = EmailSendLimiter(limit=3, window_seconds=3600)


class EmailSettingsResponse(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = "********"
    email_from: str = ""
    smtp_timeout: int = 30


class EmailSettingsUpdate(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = Field("", max_length=256)
    email_from: str = ""
    clear_password: bool = False
    smtp_timeout: int = Field(30, ge=1, le=120)


class TestEmailRequest(BaseModel):
    to: str = Field(min_length=1, max_length=320)


@router.get(
    "/{org_id}/email-settings",
    dependencies=[require_feature("email_config")],
)
@handle_db_errors("admin.email.admin_get_email_settings")
async def admin_get_email_settings(
    org_id: uuid.UUID,
    _: AuthenticatedPrincipal = require_target_org_role("org.email.view", "operator"),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> EmailSettingsResponse:
    try:
        async with session.begin():
            org = await get_organisation(session, org_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)
            cfg = org.settings_json or {}
    except ProgrammingError:
        logger.exception("admin_email.admin_get_email_settings")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_email.admin_get_email_settings")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Database error while fetching email settings."
                " Check that the latest database migrations have been applied."
            ),
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_get_email_settings")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    email_cfg = cfg.get("email", {})
    timeout_raw = email_cfg.get("smtp_timeout", 30)
    return EmailSettingsResponse(
        smtp_host=email_cfg.get("smtp_host", ""),
        smtp_port=email_cfg.get("smtp_port", 587),
        smtp_username=email_cfg.get("smtp_username", ""),
        email_from=email_cfg.get("email_from", ""),
        smtp_timeout=_effective_timeout(SimpleNamespace(smtp_timeout=timeout_raw)),
    )


@router.put(
    "/{org_id}/email-settings",
    dependencies=[require_feature("email_config"), Depends(deny_break_glass_mint)],
)
@handle_db_errors("admin.email.admin_update_email_settings")
async def admin_update_email_settings(
    org_id: uuid.UUID,
    req: EmailSettingsUpdate,
    _: AuthenticatedPrincipal = require_target_org_role("org.email.manage", "admin"),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> EmailSettingsResponse:
    try:
        async with session.begin():
            org = await get_organisation(session, org_id)
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_EMAIL_ADMIN_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_ADMIN_EMAIL_ADMIN_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching org for email settings.",
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_update_email_settings (fetch)")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

    settings_json = dict(org.settings_json or {})
    existing_email = dict(settings_json.get("email", {}))

    merged = dict(existing_email)
    merged["smtp_host"] = req.smtp_host
    merged["smtp_port"] = req.smtp_port
    merged["smtp_username"] = req.smtp_username
    if req.clear_password:
        merged["smtp_password"] = ""  # nosec B105 -- clears the stored SMTP password (clear_password request); empty string is a clear signal, NOT a hardcoded secret
    elif req.smtp_password:
        merged["smtp_password"] = encrypt_stored_secret(req.smtp_password, settings.fernet_key).decode()
    merged["email_from"] = req.email_from
    merged["smtp_timeout"] = req.smtp_timeout
    settings_json["email"] = merged

    try:
        async with session.begin():
            await update_organisation(session, org_id, {"settings_json": settings_json})
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_EMAIL_ADMIN_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_ADMIN_EMAIL_ADMIN_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating email settings.",
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_update_email_settings (update)")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return EmailSettingsResponse(
        smtp_host=req.smtp_host,
        smtp_port=req.smtp_port,
        smtp_username=req.smtp_username,
        email_from=req.email_from,
        smtp_timeout=req.smtp_timeout,
    )


@router.post(
    "/{org_id}/email-settings/test",
    status_code=status.HTTP_200_OK,
    dependencies=[require_feature("email_config")],
)
@handle_db_errors("admin.email.admin_test_email_settings")
async def admin_test_email_settings(
    org_id: uuid.UUID,
    req: TestEmailRequest,
    _: AuthenticatedPrincipal = require_target_org_role("org.email.manage", "admin"),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        async with session.begin():
            org = await get_organisation(session, org_id)
    except ProgrammingError:
        logger.exception(_CODE_ADMIN_EMAIL_ADMIN_TEST)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_ADMIN_EMAIL_ADMIN_TEST)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching org for test-email.",
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unexpected error in admin_test_email_settings (fetch)")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ORGANISATION_NOT_FOUND)

    if not _is_valid_recipient(req.to):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid test email recipient. Provide a single email address such as admin@example.com.",
        )

    cfg = org.settings_json or {}
    email_cfg = cfg.get("email", {})
    smtp_host = email_cfg.get("smtp_host", "")
    if not smtp_host:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="SMTP is not configured. Save email settings before testing.",
        )

    try:
        retry_after = await test_send_limiter.acquire(org_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A limiter failure must never break a legitimate test-send — fail open.
        logger.exception("admin_email.test_send_limiter_failed")
        retry_after = 0

    if retry_after > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many test emails. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    temp_settings = type("TempSettings", (), {})()
    temp_settings.smtp_host = smtp_host
    temp_settings.smtp_port = email_cfg.get("smtp_port", 587)
    temp_settings.smtp_username = email_cfg.get("smtp_username", "")
    try:
        async with session.begin():
            temp_settings.smtp_password = await decode_stored_secret_scoped(
                session, email_cfg.get("smtp_password", ""), settings.fernet_key, org_id=org_id
            )
    except Exception:
        logger.exception("admin_email.test_send_smtp_password_decrypt_failed")
        temp_settings.smtp_password = email_cfg.get("smtp_password", "")
    temp_settings.email_from = email_cfg.get("email_from", "")
    temp_settings.smtp_timeout = email_cfg.get("smtp_timeout", 30)

    try:
        success = await asyncio.to_thread(
            send_email,
            temp_settings,
            [req.to],
            "Modulo Test Email",
            "<html><body><h1>Test Email</h1><p>If you receive this, your SMTP configuration"
            " is working.</p></body></html>",
            "If you receive this, your SMTP configuration is working.",
        )
        if success:
            return {"ok": True, "message": "Test email sent successfully"}
        return {"ok": False, "message": "SMTP is not configured"}
    except EmailSendingError as exc:
        return {"ok": False, "message": str(exc)}
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(_CODE_ADMIN_EMAIL_ADMIN_TEST)
        return {"ok": False, "message": "Unexpected error while sending the test email"}
