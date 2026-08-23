"""API routes for error forwarder configuration — list, configure, test."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_feature, require_permission
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
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
from modulo.core.ssrf import validate_outbound_url_async
from modulo.db.models.error_event import ErrorEvent
from modulo.db.models.error_forwarder_config import ErrorForwarderConfig
from modulo.db.models.error_group import ErrorGroup
from modulo.db.rls import set_rls_org

_MSG_NO_ORGANISATION = "No organisation"
_MSG_ERROR_TRACKING_NOT_AVAILABLE = "Error tracking is not available. Run database migrations to enable it."
_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE = "Error tracking is temporarily unavailable. Please try again."
_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE = "An unexpected error occurred while processing your request."
_CODE_ERROR_FORWARDER_MANAGE = "error_forwarder.manage"
_CODE_ERROR_FORWARDER_CONFIG_TEST = "error_forwarder_config.test_forwarder"


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

# Per-type config schemas: required keys (non-empty strings) plus optional keys
# with their expected types. Mirrors the config each forwarder consumes.
_FORWARDER_CONFIG_SCHEMAS: dict[str, dict[str, Any]] = {
    "sentry": {
        "required": ["dsn"],
        "optional": {"org_slug": str, "project_slug": str},
    },
    "datadog": {
        "required": ["api_key"],
        "optional": {"site": str},
    },
    "pagerduty": {
        "required": ["routing_key"],
        "optional": {"severity_mapping": dict, "forward_levels": list},
    },
    "rollbar": {
        "required": ["access_token"],
        "optional": {"environment": str},
    },
    "opsgenie": {
        "required": ["api_key"],
        "optional": {"team": str, "priority_mapping": dict},
    },
    "loki": {
        "required": ["push_url"],
        "optional": {"tenant_id": str, "labels": dict},
    },
}


def validate_forwarder_config(forwarder_type: str, config: dict[str, Any] | None) -> list[str]:
    """Validate a forwarder config against its per-type schema.

    Returns a list of human-readable error messages; an empty list means the
    config is valid. Unknown forwarder types have no schema and are considered
    valid (the routes reject unknown types with 404 before this is reached).
    """
    schema = _FORWARDER_CONFIG_SCHEMAS.get(forwarder_type)
    if schema is None:
        return []
    config = config or {}
    errors = [
        f"missing or empty required config key '{key}'"
        for key in schema["required"]
        if not isinstance(config.get(key), str) or not config[key].strip()
    ]
    for key, expected_type in schema["optional"].items():
        if key in config and config[key] is not None and not isinstance(config[key], expected_type):
            expected_name = getattr(expected_type, "__name__", str(expected_type))
            errors.append(f"config key '{key}' must be a {expected_name}")
    return errors


def _url_candidates(forwarder_type: str, config: dict[str, Any]) -> list[tuple[str, str]]:
    """Resolve the URL-bearing fields a forwarder would POST to, for SSRF checks."""
    if forwarder_type == "loki":
        return [("push_url", config.get("push_url", ""))]
    if forwarder_type == "sentry":
        dsn = config.get("dsn")
        if not (isinstance(dsn, str) and dsn):
            return []
        parsed = urlparse(dsn)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            return [("dsn", f"{parsed.scheme}://{parsed.hostname}")]
        return []
    if forwarder_type == "datadog":
        site = config.get("site", "datadoghq.com")
        if isinstance(site, str) and site:
            return [("site", f"https://api.{site}")]
        return []
    return []


async def _validate_forwarder_urls(forwarder_type: str, config: dict[str, Any]) -> None:
    """Validate outbound URLs in forwarder config to prevent SSRF.

    The final outbound target of each forwarder derives from a user-supplied
    URL-bearing field, so each must be guarded, not just Loki's ``push_url``:
      - loki:     ``push_url`` is POSTed to directly.
      - sentry:   the ``dsn``'s hostname becomes the API base
                  (``https://{host}/api/0/...``).
      - datadog:  the ``site`` becomes the API base (``https://api.{site}/...``).
    """
    for key, url_value in _url_candidates(forwarder_type, config):
        if isinstance(url_value, str) and url_value:
            try:
                await validate_outbound_url_async(url_value)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"SSRF check failed for {key}: {exc}",
                ) from exc


def _is_configured(forwarder_type: str, config_json: dict[str, Any] | None) -> bool:
    if not config_json:
        return False
    schema = _FORWARDER_CONFIG_SCHEMAS.get(forwarder_type)
    if schema is None:
        return False
    keys = schema["required"]
    if not keys:
        return False
    return all(config_json.get(k) for k in keys)


def _merge_sensitive_config(current: dict[str, Any] | None, update: dict[str, Any]) -> dict[str, Any]:
    """MERGE forwarder config — a masked placeholder never clobbers a stored secret."""
    merged = dict(current or {})
    for k, v in update.items():
        if isinstance(v, str) and v == SENSITIVE_VALUE_MASK:
            # A masked placeholder must never clobber the stored secret
            # (read-modify-write round-trip guard). Keep the existing value.
            continue
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


def _validate_config_or_raise(forwarder_type: str, config_json: dict[str, Any] | None) -> None:
    """Reject configs that fail the per-type schema with a 422."""
    if config_json is None:
        return
    config_errors = validate_forwarder_config(forwarder_type, config_json)
    if config_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="; ".join(config_errors),
        )


async def _forwarder_config_row(
    session: AsyncSession,
    org_id: uuid.UUID,
    forwarder_type: str,
) -> ErrorForwarderConfig | None:
    """Load the live (non-deleted) forwarder config row for an org, or ``None``."""
    result = await session.execute(
        select(ErrorForwarderConfig).where(
            ErrorForwarderConfig.organisation_id == org_id,
            ErrorForwarderConfig.forwarder_type == forwarder_type,
            ErrorForwarderConfig.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _load_or_create_forwarder_config(
    session: AsyncSession,
    org_id: uuid.UUID,
    forwarder_type: str,
) -> ErrorForwarderConfig:
    """Return the live config row, creating a disabled row when absent."""
    cfg = await _forwarder_config_row(session, org_id, forwarder_type)
    if cfg is None:
        cfg = ErrorForwarderConfig(
            organisation_id=org_id,
            forwarder_type=forwarder_type,
            enabled=False,
        )
        session.add(cfg)
    return cfg


async def _merge_stored_forwarder_config(
    session: AsyncSession,
    org_id: uuid.UUID,
    forwarder_type: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Bootstrap config from stored rows when the request lacks required keys."""
    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            db_cfg = await _forwarder_config_row(session, org_id, forwarder_type)
            if db_cfg and db_cfg.config_json:
                return {**db_cfg.config_json, **config}
            return config
    except ProgrammingError as exc:
        _log.exception(_CODE_ERROR_FORWARDER_CONFIG_TEST)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_ERROR_FORWARDER_CONFIG_TEST)
        _log.warning("error_tracking.test_forwarder_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.test_forwarder_config_read_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc


async def _record_test_result(
    session: AsyncSession,
    org_id: uuid.UUID,
    forwarder_type: str,
    ok: bool,
) -> None:
    """Persist the last test outcome onto the config row (best-effort)."""
    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            db_cfg = await _forwarder_config_row(session, org_id, forwarder_type)
            if db_cfg is None:
                return
            db_cfg.last_test_at = datetime.now(UTC)
            db_cfg.last_test_ok = ok
            await session.flush()
    except ProgrammingError as exc:
        _log.exception(_CODE_ERROR_FORWARDER_CONFIG_TEST)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_ERROR_FORWARDER_CONFIG_TEST)
        _log.warning("error_tracking.test_forwarder_save_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.test_forwarder_save_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc


@router.get("", dependencies=[require_feature("error_forwarders")])
async def list_forwarders(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ForwarderListResponse:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)

            result = await session.execute(
                select(ErrorForwarderConfig).where(
                    ErrorForwarderConfig.organisation_id == org_id,
                    ErrorForwarderConfig.deleted_at.is_(None),
                )
            )
            existing = {r.forwarder_type: r for r in result.scalars().all()}
    except ProgrammingError as exc:
        _log.exception("error_forwarder_config.list_forwarders")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("error_forwarder_config.list_forwarders")
        _log.warning("error_tracking.list_forwarders_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.list_forwarders_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
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
    dependencies=[require_feature("error_forwarders")],
)
async def configure_forwarder(
    forwarder_type: str,
    req: ForwarderConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERROR_FORWARDER_MANAGE),
) -> ForwarderConfigResponse:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    if forwarder_type not in _FORWARDER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown forwarder type: {forwarder_type}",
        )

    _validate_config_or_raise(forwarder_type, req.config_json)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            cfg = await _load_or_create_forwarder_config(session, org_id, forwarder_type)

            if req.enabled is not None:
                cfg.enabled = req.enabled
            if req.config_json is not None:
                cfg.config_json = _merge_sensitive_config(cfg.config_json, req.config_json)

            cfg.updated_at = datetime.now(UTC)
            await session.flush()
    except ProgrammingError as exc:
        _log.exception("error_forwarder_config.configure_forwarder")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("error_forwarder_config.configure_forwarder")
        _log.warning("error_tracking.configure_forwarder_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.configure_forwarder_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc

    return ForwarderConfigResponse.from_orm_model(cfg)


@router.post(
    "/{forwarder_type}/test",
    dependencies=[require_feature("error_forwarders")],
)
async def test_forwarder(
    forwarder_type: str,
    req: TestConnectionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERROR_FORWARDER_MANAGE),
) -> ForwarderTestResult:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    if forwarder_type not in _FORWARDER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown forwarder type: {forwarder_type}",
        )

    forwarder: BaseForwarder | None = get_forwarder(forwarder_type)
    if forwarder is None:
        return ForwarderTestResult(ok=False, message=f"Forwarder implementation not found for {forwarder_type}")

    config = req.config_json or {}
    if not _is_configured(forwarder_type, config):
        config = await _merge_stored_forwarder_config(session, org_id, forwarder_type, config)

    # SSRF guard: validate outbound URL before forward()
    await _validate_forwarder_urls(forwarder_type, config)

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

    await _record_test_result(session, org_id, forwarder_type, ok)

    name = _FORWARDER_DISPLAY_NAMES.get(forwarder_type, forwarder_type)
    if ok:
        return ForwarderTestResult(ok=True, message=f"Successfully connected to {name}")
    return ForwarderTestResult(ok=False, message=f"Failed to connect to {name}. Check your configuration.")


@router.delete(
    "/{forwarder_type}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_feature("error_forwarders")],
)
async def delete_forwarder(
    forwarder_type: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERROR_FORWARDER_MANAGE),
) -> None:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    if forwarder_type not in _FORWARDER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown forwarder type: {forwarder_type}",
        )

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            result = await session.execute(
                update(ErrorForwarderConfig)
                .where(
                    ErrorForwarderConfig.organisation_id == org_id,
                    ErrorForwarderConfig.forwarder_type == forwarder_type,
                    ErrorForwarderConfig.deleted_at.is_(None),
                )
                .values(deleted_at=func.now())
                .returning(ErrorForwarderConfig.id)
            )
            deleted = result.scalar_one_or_none()
    except ProgrammingError as exc:
        _log.exception("error_forwarder_config.delete_forwarder")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("error_forwarder_config.delete_forwarder")
        _log.warning("error_tracking.delete_forwarder_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.delete_forwarder_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Forwarder configuration not found")


@router.post(
    "/{forwarder_type}/restore",
    dependencies=[require_feature("error_forwarders")],
)
async def restore_forwarder(
    forwarder_type: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERROR_FORWARDER_MANAGE),
) -> ForwarderConfigResponse:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    if forwarder_type not in _FORWARDER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown forwarder type: {forwarder_type}",
        )

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            result = await session.execute(
                update(ErrorForwarderConfig)
                .where(
                    ErrorForwarderConfig.organisation_id == org_id,
                    ErrorForwarderConfig.forwarder_type == forwarder_type,
                    ErrorForwarderConfig.deleted_at.is_not(None),
                )
                .values(deleted_at=None)
                .returning(ErrorForwarderConfig)
            )
            cfg = result.scalar_one_or_none()
    except ProgrammingError as exc:
        _log.exception("error_forwarder_config.restore_forwarder")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("error_forwarder_config.restore_forwarder")
        _log.warning("error_tracking.restore_forwarder_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.restore_forwarder_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Forwarder configuration not found")
    return ForwarderConfigResponse.from_orm_model(cfg)
