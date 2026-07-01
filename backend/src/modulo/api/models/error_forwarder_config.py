from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

_SENSITIVE_KEYS = frozenset({
    "dsn", "api_key", "access_token", "routing_key", "secret",
})


def _mask_sensitive(config: dict | None) -> dict:
    if not config:
        return {}
    return {k: ("••••••" if k in _SENSITIVE_KEYS else v) for k, v in config.items()}


class ForwarderConfigUpdate(BaseModel):
    enabled: bool | None = None
    config_json: dict | None = None


class ForwarderConfigResponse(BaseModel):
    forwarder_type: str
    enabled: bool
    config_summary: dict
    last_test_at: datetime | None = None
    last_test_ok: bool | None = None

    @classmethod
    def from_orm_model(cls, obj) -> ForwarderConfigResponse:
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
    config_json: dict = Field(default_factory=dict)


class ForwarderTestResult(BaseModel):
    ok: bool
    message: str
