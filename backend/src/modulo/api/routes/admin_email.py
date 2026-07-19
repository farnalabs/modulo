import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.email_service import EmailSendingError, send_email
from modulo.db.crud.organisation import get_organisation, update_organisation

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/org", tags=["admin"])


class EmailSettingsResponse(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = "********"
    email_from: str = ""


class EmailSettingsUpdate(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = ""
    clear_password: bool = False


class TestEmailRequest(BaseModel):
    to: str = Field(min_length=1)


@handle_db_errors("admin.email.admin_get_email_settings")
@router.get("/{org_id}/email-settings", response_model=EmailSettingsResponse)
async def admin_get_email_settings(
    org_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> EmailSettingsResponse:
    if not current_user.is_system_admin and current_user.organisation_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    try:
        async with session.begin():
            org = await get_organisation(session, org_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
            cfg = org.settings_json or {}
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
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
            detail="Internal server error",
        ) from None

    email_cfg = cfg.get("email", {})
    return EmailSettingsResponse(
        smtp_host=email_cfg.get("smtp_host", ""),
        smtp_port=email_cfg.get("smtp_port", 587),
        smtp_username=email_cfg.get("smtp_username", ""),
        email_from=email_cfg.get("email_from", ""),
    )


@handle_db_errors("admin.email.admin_update_email_settings")
@router.put("/{org_id}/email-settings", response_model=EmailSettingsResponse)
async def admin_update_email_settings(
    org_id: uuid.UUID,
    req: EmailSettingsUpdate,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> EmailSettingsResponse:
    if not current_user.is_system_admin and current_user.organisation_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    try:
        org = await get_organisation(session, org_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
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
            detail="Internal server error",
        ) from None

    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    settings_json = dict(org.settings_json or {})
    existing_email = dict(settings_json.get("email", {}))

    merged = dict(existing_email)
    merged["smtp_host"] = req.smtp_host
    merged["smtp_port"] = req.smtp_port
    merged["smtp_username"] = req.smtp_username
    if req.clear_password:
        merged["smtp_password"] = ""
    elif req.smtp_password:
        merged["smtp_password"] = req.smtp_password
    merged["email_from"] = req.email_from
    settings_json["email"] = merged

    try:
        await update_organisation(session, org_id, {"settings_json": settings_json})
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
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
            detail="Internal server error",
        ) from None

    return EmailSettingsResponse(
        smtp_host=req.smtp_host,
        smtp_port=req.smtp_port,
        smtp_username=req.smtp_username,
        email_from=req.email_from,
    )


@handle_db_errors("admin.email.admin_test_email_settings")
@router.post("/{org_id}/email-settings/test", status_code=status.HTTP_200_OK)
async def admin_test_email_settings(
    org_id: uuid.UUID,
    req: TestEmailRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    if not current_user.is_system_admin and current_user.organisation_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    try:
        org = await get_organisation(session, org_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
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
            detail="Internal server error",
        ) from None

    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    cfg = org.settings_json or {}
    email_cfg = cfg.get("email", {})
    smtp_host = email_cfg.get("smtp_host", "")
    if not smtp_host:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="SMTP is not configured. Save email settings before testing.",
        )

    temp_settings = type("TempSettings", (), {})()
    temp_settings.smtp_host = smtp_host
    temp_settings.smtp_port = email_cfg.get("smtp_port", 587)
    temp_settings.smtp_username = email_cfg.get("smtp_username", "")
    temp_settings.smtp_password = email_cfg.get("smtp_password", "")
    temp_settings.email_from = email_cfg.get("email_from", "")

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
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
