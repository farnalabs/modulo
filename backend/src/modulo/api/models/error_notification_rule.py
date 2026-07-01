from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ErrorNotificationRuleCreate(BaseModel):
    name: str = Field(..., max_length=100)
    enabled: bool = True
    condition_level: str = "error"
    condition_min_count: int = Field(1, ge=1)
    condition_window_seconds: int = Field(300, ge=1)
    action_type: str = "in_app"
    webhook_url: str | None = None
    cooldown_seconds: int = Field(300, ge=0)

    @field_validator("condition_level")
    @classmethod
    def _validate_level(cls, v: str) -> str:
        if v not in ("error", "warning", "critical"):
            raise ValueError("condition_level must be one of: error, warning, critical")
        return v

    @field_validator("action_type")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        if v not in ("in_app", "email", "webhook"):
            raise ValueError("action_type must be one of: in_app, email, webhook")
        return v

    @field_validator("webhook_url")
    @classmethod
    def _validate_webhook(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("webhook_url must start with http:// or https://")
        return v


class ErrorNotificationRuleUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    enabled: bool | None = None
    condition_level: str | None = None
    condition_min_count: int | None = Field(None, ge=1)
    condition_window_seconds: int | None = Field(None, ge=1)
    action_type: str | None = None
    webhook_url: str | None = None
    cooldown_seconds: int | None = Field(None, ge=0)

    @field_validator("condition_level")
    @classmethod
    def _validate_level(cls, v: str | None) -> str | None:
        if v is not None and v not in ("error", "warning", "critical"):
            raise ValueError("condition_level must be one of: error, warning, critical")
        return v

    @field_validator("action_type")
    @classmethod
    def _validate_action(cls, v: str | None) -> str | None:
        if v is not None and v not in ("in_app", "email", "webhook"):
            raise ValueError("action_type must be one of: in_app, email, webhook")
        return v

    @field_validator("webhook_url")
    @classmethod
    def _validate_webhook(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("webhook_url must start with http:// or https://")
        return v


class ErrorNotificationRuleResponse(BaseModel):
    id: str
    name: str
    enabled: bool
    condition_level: str
    condition_min_count: int
    condition_window_seconds: int
    action_type: str
    webhook_url: str | None = None
    cooldown_seconds: int
    created_at: str
    updated_at: str


class ErrorNotificationRuleListResponse(BaseModel):
    items: list[ErrorNotificationRuleResponse]
    total: int
    limit: int
    offset: int
