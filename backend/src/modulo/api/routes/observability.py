import os
import uuid
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.observability import get_otel_config, update_otel_config
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

router = APIRouter(prefix="/api/v1/settings/observability", tags=["observability"])


_SENSITIVE_HEADER_KEYS = frozenset({"authorization", "x-api-key", "api-key", "x-otlp-token"})


def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: "••••••" if k.lower() in _SENSITIVE_HEADER_KEYS else v for k, v in headers.items()}


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
    otlp_headers: dict[str, str] = {}


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


@router.get("", response_model=OtelSettingsResponse)
async def get_observability_settings(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> OtelSettingsResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        raw = await get_otel_config(session, principal.organisation_id)
    merged = {**_DEFAULT_OTEL_CONFIG, **raw}
    return _config_to_response(merged)


@router.put("", response_model=OtelSettingsResponse)
async def update_observability_settings(
    body: OtelSettingsUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> OtelSettingsResponse:
    updates: dict[str, Any] = {}
    if body.otlp_endpoint is not None:
        updates["otlp_endpoint"] = body.otlp_endpoint
    if body.otlp_headers is not None:
        updates["otlp_headers"] = body.otlp_headers
    if body.export_interval_seconds is not None:
        updates["export_interval_seconds"] = body.export_interval_seconds
    if body.langsmith_enabled is not None:
        updates["langsmith_enabled"] = body.langsmith_enabled
    if body.langsmith_api_key is not None:
        if body.langsmith_api_key == "":
            updates["langsmith_api_key_ciphertext"] = None
        else:
            fernet = Fernet(settings.fernet_key.encode())
            updates["langsmith_api_key_ciphertext"] = fernet.encrypt(body.langsmith_api_key.encode()).decode()

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        merged = await update_otel_config(session, principal.organisation_id, updates)
    return _config_to_response(merged)


@router.post("/test", response_model=TestSpanResult)
async def test_otel_connection(
    body: TestOtelConfig,
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> TestSpanResult:
    endpoint = body.otlp_endpoint.rstrip("/")
    if not endpoint:
        return TestSpanResult(success=False, message="OTLP endpoint is required")

    url = f"{endpoint}/v1/traces"
    trace_id = uuid.uuid4().hex[:32]
    span_id = uuid.uuid4().hex[:16]
    import time as _time

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
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=test_span, headers=body.otlp_headers or {})
        if resp.status_code < 500:
            return TestSpanResult(
                success=True,
                message=f"Test span exported successfully (HTTP {resp.status_code})",
            )
        return TestSpanResult(
            success=False,
            message=f"OTLP endpoint returned HTTP {resp.status_code}: {resp.text[:200]}",
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
        return TestSpanResult(success=False, message=f"Connection failed: {exc}")


@router.get("/preview", response_model=ExportPreviewResponse)
async def get_export_preview(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> ExportPreviewResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        raw = await get_otel_config(session, principal.organisation_id)
    merged = {**_DEFAULT_OTEL_CONFIG, **raw}

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
