"""API routes for error forwarder configuration — list, configure, test."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_feature
from modulo.api.models.error_forwarder_config import (
    ForwarderConfigResponse,
    ForwarderConfigUpdate,
    ForwarderListItem,
    ForwarderListResponse,
    ForwarderTestResult,
    TestConnectionRequest,
)
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.error_tracking.forwarders import BaseForwarder, get_forwarder
from modulo.db.models.error_event import ErrorEvent
from modulo.db.models.error_forwarder_config import ErrorForwarderConfig
from modulo.db.models.error_group import ErrorGroup
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/errors/forwarders", tags=["error-forwarders"])

_FORWARDER_DISPLAY_NAMES: dict[str, str] = {
    "sentry": "Sentry",
    "datadog": "DataDog",
    "pagerduty": "PagerDuty",
    "rollbar": "Rollbar",
    "opsgenie": "OpsGenie",
    "loki": "Loki",
}

_FORWARDER_TYPES = list(_FORWARDER_DISPLAY_NAMES)


def _require_admin(principal: TenantPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required to manage error forwarders",
        )


def _is_configured(forwarder_type: str, config_json: dict[str, Any] | None) -> bool:
    if not config_json:
        return False
    required_keys: dict[str, list[str]] = {
        "sentry": ["dsn"],
        "datadog": ["api_key"],
        "pagerduty": ["routing_key"],
        "rollbar": ["access_token"],
        "opsgenie": ["api_key"],
        "loki": ["push_url"],
    }
    keys = required_keys.get(forwarder_type, [])
    if not keys:
        return False
    return all(config_json.get(k) for k in keys)


@router.get("", response_model=ForwarderListResponse, dependencies=[require_feature("error_forwarders")])
async def list_forwarders(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ForwarderListResponse:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

    try:
        async with session.begin():
            await set_rls_org(session, org_id)

            result = await session.execute(
                select(ErrorForwarderConfig).where(ErrorForwarderConfig.organisation_id == org_id)
            )
            existing = {r.forwarder_type: r for r in result.scalars().all()}
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Error tracking is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("error_tracking.list_forwarders_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Error tracking is temporarily unavailable. Please try again.",
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.list_forwarders_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing your request.",
        ) from exc

    items: list[ForwarderListItem] = []
    for ftype in _FORWARDER_TYPES:
        cfg = existing.get(ftype)
        items.append(
            ForwarderListItem(
                forwarder_type=ftype,
                display_name=_FORWARDER_DISPLAY_NAMES[ftype],
                enabled=cfg.enabled if cfg else False,
                configured=_is_configured(ftype, cfg.config_json if cfg else None),
                last_test_at=cfg.last_test_at if cfg else None,
                last_test_ok=cfg.last_test_ok if cfg else None,
            )
        )

    return ForwarderListResponse(forwarders=items)


@router.put(
    "/{forwarder_type}",
    response_model=ForwarderConfigResponse,
    dependencies=[require_feature("error_forwarders")],
)
async def configure_forwarder(
    forwarder_type: str,
    req: ForwarderConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ForwarderConfigResponse:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

    if forwarder_type not in _FORWARDER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown forwarder type: {forwarder_type}",
        )

    _require_admin(principal)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)

            result = await session.execute(
                select(ErrorForwarderConfig).where(
                    ErrorForwarderConfig.organisation_id == org_id,
                    ErrorForwarderConfig.forwarder_type == forwarder_type,
                )
            )
            cfg = result.scalar_one_or_none()

            if cfg is None:
                cfg = ErrorForwarderConfig(
                    organisation_id=org_id,
                    forwarder_type=forwarder_type,
                    enabled=False,
                )
                session.add(cfg)

            if req.enabled is not None:
                cfg.enabled = req.enabled
            if req.config_json is not None:
                cfg.config_json = req.config_json

            cfg.updated_at = datetime.now(UTC)
            await session.flush()
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Error tracking is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("error_tracking.configure_forwarder_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Error tracking is temporarily unavailable. Please try again.",
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.configure_forwarder_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing your request.",
        ) from exc

    return ForwarderConfigResponse.from_orm_model(cfg)


@router.post(
    "/{forwarder_type}/test",
    response_model=ForwarderTestResult,
    dependencies=[require_feature("error_forwarders")],
)
async def test_forwarder(
    forwarder_type: str,
    req: TestConnectionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ForwarderTestResult:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

    if forwarder_type not in _FORWARDER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown forwarder type: {forwarder_type}",
        )

    forwarder: BaseForwarder | None = get_forwarder(forwarder_type)
    if forwarder is None:
        return ForwarderTestResult(ok=False, message=f"Forwarder implementation not found for {forwarder_type}")

    config = req.config_json or {}
    if _is_configured(forwarder_type, config):
        pass
    else:
        try:
            async with session.begin():
                await set_rls_org(session, org_id)
                result = await session.execute(
                    select(ErrorForwarderConfig).where(
                        ErrorForwarderConfig.organisation_id == org_id,
                        ErrorForwarderConfig.forwarder_type == forwarder_type,
                    )
                )
                db_cfg = result.scalar_one_or_none()
                if db_cfg and db_cfg.config_json:
                    config = {**db_cfg.config_json, **config}
        except ProgrammingError as exc:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Error tracking is not available. Run database migrations to enable it.",
            ) from exc
        except SQLAlchemyError as exc:
            _log.warning("error_tracking.test_forwarder_db_error")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Error tracking is temporarily unavailable. Please try again.",
            ) from exc
        except Exception as exc:
            _log.exception("error_tracking.test_forwarder_config_read_error")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An unexpected error occurred while processing your request.",
            ) from exc

    test_group = ErrorGroup(
        organisation_id=org_id,
        fingerprint="test-connection-" + str(uuid.uuid4()),
        level_peak="error",
        count=1,
    )
    test_event = ErrorEvent(
        organisation_id=org_id,
        fingerprint=test_group.fingerprint,
        level="error",
        message="Test error from Modulo forwarder configuration",
        source="modulo-test",
        environment="test",
    )

    try:
        ok = await asyncio.wait_for(forwarder.forward(org_id, test_group, test_event, config), timeout=15.0)
    except TimeoutError:
        _log.warning("forwarder.test_connection_timeout", extra={"type": forwarder_type})
        ok = False
    except Exception:
        _log.exception("forwarder.test_connection_failed", extra={"type": forwarder_type})
        ok = False

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            result = await session.execute(
                select(ErrorForwarderConfig).where(
                    ErrorForwarderConfig.organisation_id == org_id,
                    ErrorForwarderConfig.forwarder_type == forwarder_type,
                )
            )
            db_cfg = result.scalar_one_or_none()
            if db_cfg:
                db_cfg.last_test_at = datetime.now(UTC)
                db_cfg.last_test_ok = ok
                await session.flush()
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Error tracking is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.warning("error_tracking.test_forwarder_save_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Error tracking is temporarily unavailable. Please try again.",
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.test_forwarder_save_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing your request.",
        ) from exc

    name = _FORWARDER_DISPLAY_NAMES.get(forwarder_type, forwarder_type)
    if ok:
        return ForwarderTestResult(ok=True, message=f"Successfully connected to {name}")
    return ForwarderTestResult(ok=False, message=f"Failed to connect to {name}. Check your configuration.")
