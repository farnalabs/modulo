import asyncio
import logging
import os
import time as _time
import uuid
from typing import Any, ClassVar

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE
from modulo.api.dependencies import get_db_session, require_feature, require_permission
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.auth.jwt import TenantPrincipal
from modulo.core.ssrf import pinned_async_client, validate_outbound_url_async
from modulo.db.crud.observability import get_otel_config, update_otel_config
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_CODE_OBSERVABILITY_VIEW = "observability.view"
_CODE_OBSERVABILITY_MANAGE = "observability.manage"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/settings/observability", tags=["observability"])

_DB_TIMEOUT = 10  # seconds — max time for DB operations per request
_CACHE_TTL = 60  # seconds — how long to serve stale cache after DB failure

_SENSITIVE_HEADER_KEYS = frozenset({"authorization", "x-api-key", "api-key", "x-otlp-token"})


def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: SENSITIVE_VALUE_MASK if k.lower() in _SENSITIVE_HEADER_KEYS else v for k, v in headers.items()}


class OtelSettingsUpdate(BaseModel):
    otlp_endpoint: str | None = None
    otlp_headers: dict[str, str] | None = None
    export_interval_seconds: int | None = Field(None, ge=1)
    langsmith_enabled: bool | None = None
    langsmith_api_key: str | None = None


class OtelSettingsResponse(BaseModel):
    otlp_endpoint: str
    otlp_headers: dict[str, str]
    export_interval_seconds: int
    langsmith_enabled: bool
    has_langsmith_api_key: bool
    effective_otlp_endpoint: str
    env_override_active: bool

    model_config = {"from_attributes": False}


class TestOtelConfig(BaseModel):
    otlp_endpoint: str
    otlp_headers: ClassVar[dict[str, str]] = {}


class TestSpanResult(BaseModel):
    success: bool
    message: str


class ExportPreviewResponse(BaseModel):
    sample_span: dict[str, Any]
    config_used: dict[str, Any]


_DEFAULT_OTEL_CONFIG: dict[str, Any] = {
    "otlp_endpoint": "",
    "otlp_headers": {},
    "export_interval_seconds": 10,
    "langsmith_enabled": False,
    "langsmith_api_key_ciphertext": None,
}

# In-memory cache: last successfully-read config per org_id.
# Falls back to these values when the database is unreachable.
_config_cache: dict[str, dict[str, Any]] = {}
_config_cache_ts: dict[str, float] = {}


def _cached_config(org_id: str) -> dict[str, Any] | None:
    entry = _config_cache.get(org_id)
    ts = _config_cache_ts.get(org_id, 0.0)
    if entry is not None and (_time.monotonic() - ts) < _CACHE_TTL:
        return dict(entry)
    return None


def _update_cache(org_id: str, config: dict[str, Any]) -> None:
    _config_cache[org_id] = dict(config)
    _config_cache_ts[org_id] = _time.monotonic()


def _invalidate_cache(org_id: str) -> None:
    _config_cache.pop(org_id, None)
    _config_cache_ts.pop(org_id, None)


def _config_to_response(
    config: dict[str, Any],
) -> OtelSettingsResponse:
    env_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    db_endpoint = config.get("otlp_endpoint", "")
    env_override = bool(env_endpoint)
    return OtelSettingsResponse(
        otlp_endpoint=db_endpoint,
        otlp_headers=_mask_headers(config.get("otlp_headers", {})),
        export_interval_seconds=config.get("export_interval_seconds", 10),
        langsmith_enabled=config.get("langsmith_enabled", False),
        has_langsmith_api_key=bool(config.get("langsmith_api_key_ciphertext")),
        effective_otlp_endpoint=env_endpoint or db_endpoint,
        env_override_active=env_override,
    )


async def _fetch_config_from_db(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    raw = await asyncio.wait_for(get_otel_config(session, org_id), timeout=_DB_TIMEOUT)
    return {**_DEFAULT_OTEL_CONFIG, **raw}


async def _fetch_and_cache(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    raw = await _fetch_config_from_db(session, org_id)
    _update_cache(str(org_id), raw)
    return raw


def _build_degraded_response(org_id: str) -> OtelSettingsResponse:
    cached = _cached_config(org_id)
    merged = cached if cached is not None else dict(_DEFAULT_OTEL_CONFIG)
    return _config_to_response(merged)


@router.get("", dependencies=[require_feature("observability")])
async def get_observability_settings(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_OBSERVABILITY_VIEW),
) -> OtelSettingsResponse:
    try:
        async with asyncio.timeout(_DB_TIMEOUT):
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                merged = await _fetch_and_cache(session, principal.organisation_id)
        return _config_to_response(merged)
    except ProgrammingError as exc:
        _log.exception("observability.get_observability_settings")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except TimeoutError:
        _log.warning(
            "observability.get.timeout",
            extra={"org_id": str(principal.organisation_id)},
        )
    except Exception:
        _log.exception(
            "observability.get.failed",
            extra={"org_id": str(principal.organisation_id)},
        )
    return _build_degraded_response(str(principal.organisation_id))


@router.put(
    "",
    dependencies=[require_feature("observability")],
    responses={
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
        504: {"description": "Gateway Timeout"},
    },
)
async def update_observability_settings(
    req: OtelSettingsUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_OBSERVABILITY_MANAGE),
    settings: Settings = Depends(get_settings),
) -> OtelSettingsResponse:
    updates: dict[str, Any] = {}
    if req.otlp_endpoint is not None:
        updates["otlp_endpoint"] = req.otlp_endpoint
    if req.otlp_headers is not None:
        updates["otlp_headers"] = req.otlp_headers
    if req.export_interval_seconds is not None:
        updates["export_interval_seconds"] = req.export_interval_seconds
    if req.langsmith_enabled is not None:
        updates["langsmith_enabled"] = req.langsmith_enabled
    if req.langsmith_api_key is not None:
        if not req.langsmith_api_key:
            updates["langsmith_api_key_ciphertext"] = None
        else:
            fernet = Fernet(settings.fernet_key.encode())
            updates["langsmith_api_key_ciphertext"] = fernet.encrypt(req.langsmith_api_key.encode()).decode()

    try:
        async with asyncio.timeout(_DB_TIMEOUT):
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                merged = await update_otel_config(session, principal.organisation_id, updates)
        _invalidate_cache(str(principal.organisation_id))
        return _config_to_response(merged)
    except ProgrammingError as exc:
        _log.exception("observability.update_observability_settings")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("observability.update_observability_settings")
        _log.warning(
            "observability.put.db_error",
            extra={"org_id": str(principal.organisation_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service is temporarily unavailable. Please try again later.",
        ) from exc
    except TimeoutError:
        _log.warning(
            "observability.put.timeout",
            extra={"org_id": str(principal.organisation_id)},
        )
        raise HTTPException(status_code=504, detail="Gateway timeout.") from None
    except Exception:
        _log.exception(
            "observability.put.failed",
            extra={"org_id": str(principal.organisation_id)},
        )
        raise HTTPException(status_code=500, detail="Internal server error.") from None


@router.post("/test", dependencies=[require_feature("observability")])
async def test_otel_connection(
    req: TestOtelConfig,
    _principal: TenantPrincipal = require_permission(_CODE_OBSERVABILITY_VIEW),
) -> TestSpanResult:
    endpoint = req.otlp_endpoint.rstrip("/")
    if not endpoint:
        return TestSpanResult(success=False, message="OTLP endpoint is required")

    url = f"{endpoint}/v1/traces"
    try:
        await validate_outbound_url_async(url)
    except ValueError as exc:
        return TestSpanResult(success=False, message=f"Rejected: {exc}")
    trace_id = uuid.uuid4().hex[:32]
    span_id = uuid.uuid4().hex[:16]

    now_ns = str(int(_time.time() * 1_000_000_000))
    service_attr = {"key": "service.name", "value": {"stringValue": "modulo-test"}}
    test_span = {
        "resourceSpans": [
            {
                "resource": {"attributes": [service_attr]},
                "scopeSpans": [
                    {
                        "scope": {"name": "modulo.test"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": "modulo.test-connection",
                                "kind": 1,
                                "startTimeUnixNano": now_ns,
                                "endTimeUnixNano": now_ns,
                                "attributes": [
                                    {"key": "test", "value": {"boolValue": True}},
                                    {"key": "modulo.version", "value": {"stringValue": "0.1.0"}},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    try:
        client = await pinned_async_client(url)
        client.timeout = httpx.Timeout(10.0)
    except ValueError as exc:
        return TestSpanResult(success=False, message=f"Rejected: {exc}")

    try:
        resp = await client.post(url, json=test_span, headers=req.otlp_headers or {})
        if resp.status_code < 500:
            return TestSpanResult(
                success=True,
                message=f"Test span exported successfully (HTTP {resp.status_code})",
            )
        return TestSpanResult(
            success=False,
            message=f"OTLP endpoint returned HTTP {resp.status_code}",
        )
    except httpx.TimeoutException:
        return TestSpanResult(
            success=False,
            message="Connection timed out — check endpoint URL and network",
        )
    except httpx.ConnectError:
        return TestSpanResult(
            success=False,
            message="Connection refused — check endpoint URL and firewall",
        )
    except Exception as exc:
        _log.exception("observability.test_otel_connection")
        return TestSpanResult(success=False, message=f"Connection failed: {exc}")
    finally:
        await client.aclose()


@router.get("/preview", dependencies=[require_feature("observability")])
async def get_export_preview(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_OBSERVABILITY_VIEW),
) -> ExportPreviewResponse:
    try:
        async with asyncio.timeout(_DB_TIMEOUT):
            async with session.begin():
                await set_rls_org(session, principal.organisation_id)
                merged = await _fetch_and_cache(session, principal.organisation_id)
    except ProgrammingError as exc:
        _log.exception("observability.get_export_preview")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except TimeoutError:
        _log.warning(
            "observability.preview.timeout",
            extra={"org_id": str(principal.organisation_id)},
        )
        cached = _cached_config(str(principal.organisation_id))
        merged = cached if cached is not None else dict(_DEFAULT_OTEL_CONFIG)
    except Exception:
        _log.exception(
            "observability.preview.failed",
            extra={"org_id": str(principal.organisation_id)},
        )
        cached = _cached_config(str(principal.organisation_id))
        merged = cached if cached is not None else dict(_DEFAULT_OTEL_CONFIG)

    trace_id = uuid.uuid4().hex[:32]
    span_id = uuid.uuid4().hex[:16]

    sample_id = "00000000-0000-0000-0000-000000000000"
    sample_span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": "modulo.pipeline.run",
        "kind": 1,
        "startTimeUnixNano": "1719000000000000000",
        "endTimeUnixNano": "1719000005000000000",
        "attributes": [
            {"key": "pipeline.id", "value": {"stringValue": sample_id}},
            {"key": "pipeline.name", "value": {"stringValue": "My Pipeline"}},
            {"key": "node.name", "value": {"stringValue": "analyze"}},
            {"key": "langgraph.llm.prompt_tokens", "value": {"intValue": "450"}},
            {"key": "langgraph.llm.completion_tokens", "value": {"intValue": "120"}},
        ],
    }

    config_used: dict[str, Any] = {
        "otlp_endpoint": merged.get("otlp_endpoint", ""),
        "otlp_headers": _mask_headers(merged.get("otlp_headers", {})),
        "export_interval_seconds": merged.get("export_interval_seconds", 10),
        "langsmith_enabled": merged.get("langsmith_enabled", False),
        "has_langsmith_api_key": bool(merged.get("langsmith_api_key_ciphertext")),
    }

    return ExportPreviewResponse(sample_span=sample_span, config_used=config_used)
