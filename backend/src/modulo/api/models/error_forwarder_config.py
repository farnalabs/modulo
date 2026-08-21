from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from modulo.db.models.error_forwarder_config import ErrorForwarderConfig

_SENSITIVE_KEYS = frozenset(
    {
        "dsn",
        "api_key",
        "access_token",
        "routing_key",
        "secret",
    }
)


def _mask_sensitive(config: dict[str, Any] | None) -> dict[str, Any]:
    if not config:
        return {}
    return {k: ("••••••" if k in _SENSITIVE_KEYS else v) for k, v in config.items()}


class ForwarderConfigUpdate(BaseModel):
    enabled: bool | None = None
    config_json: dict[str, Any] | None = None


class ForwarderConfigResponse(BaseModel):
    forwarder_type: str
    enabled: bool
    config_summary: dict[str, Any]
    last_test_at: datetime | None = None
    last_test_ok: bool | None = None

    @classmethod
    def from_orm_model(cls, obj: ErrorForwarderConfig) -> ForwarderConfigResponse:
        return cls(
            forwarder_type=obj.forwarder_type,
            enabled=obj.enabled,
            config_summary=_mask_sensitive(obj.config_json),
            last_test_at=obj.last_test_at,
            last_test_ok=obj.last_test_ok,
        )


class ForwarderListItem(BaseModel):
    forwarder_type: str
    display_name: str
    enabled: bool
    configured: bool
    last_test_at: datetime | None = None
    last_test_ok: bool | None = None


class ForwarderListResponse(BaseModel):
    forwarders: list[ForwarderListItem]


class TestConnectionRequest(BaseModel):
    config_json: dict[str, Any] = Field(default_factory=dict[str, Any])


class ForwarderTestResult(BaseModel):
    ok: bool
    message: str
