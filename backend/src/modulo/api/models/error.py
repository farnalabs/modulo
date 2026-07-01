from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: str | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Override to include backward-compatible top-level keys."""
        result = super().model_dump(*args, **kwargs)
        result["detail"] = self.error.message
        return result


# ---------------------------------------------------------------------------
# Error ingestion schemas
# ---------------------------------------------------------------------------


class ErrorEventInput(BaseModel):
    level: str
    message: str
    stacktrace: str | None = None
    context_json: dict[str, Any] | None = None
    source: str
    environment: str | None = None
    version: str | None = None
    breadcrumbs: list[dict[str, Any]] | None = None

    @field_validator("level")
    @classmethod
    def _validate_level(cls, v: str) -> str:
        if v not in ("error", "warning", "critical"):
            msg = f"Invalid level '{v}'. Must be one of: error, warning, critical"
            raise ValueError(msg)
        return v

    @field_validator("source")
    @classmethod
    def _validate_source(cls, v: str) -> str:
        if v not in ("backend", "frontend", "celery"):
            msg = f"Invalid source '{v}'. Must be one of: backend, frontend, celery"
            raise ValueError(msg)
        return v

    @field_validator("message")
    @classmethod
    def _validate_message(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message must not be empty")
        return v.strip()

    @field_validator("breadcrumbs")
    @classmethod
    def _validate_breadcrumbs(cls, v: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if v is not None and len(v) > 50:
            raise ValueError("breadcrumbs must not exceed 50 items")
        return v


class ErrorIngestRequest(BaseModel):
    events: list[ErrorEventInput] = Field(..., min_length=1, max_length=20)


class ErrorGroupResult(BaseModel):
    group_id: str
    is_new: bool


class ErrorIngestResponse(BaseModel):
    results: list[ErrorGroupResult]


class SessionKeyResponse(BaseModel):
    key: str
    expires_in_seconds: int = 3600


# ---------------------------------------------------------------------------
# Error dashboard / admin schemas
# ---------------------------------------------------------------------------


class ErrorGroupSummary(BaseModel):
    id: str
    fingerprint: str
    status: str
    level_peak: str
    count: int
    first_seen: str
    last_seen: str
    sample_message: str


class ErrorEventDetail(BaseModel):
    id: str
    level: str
    message: str
    stacktrace: str | None = None
    context_json: dict[str, Any] | None = None
    source: str
    environment: str | None = None
    version: str | None = None
    breadcrumbs: list[dict[str, Any]] | None = None
    created_at: str


class ErrorGroupDetail(BaseModel):
    id: str
    fingerprint: str
    status: str
    level_peak: str
    count: int
    first_seen: str
    last_seen: str
    sample_event: ErrorEventDetail | None = None
    assigned_to: str | None = None


class ErrorGroupUpdate(BaseModel):
    status: str | None = None
    assigned_to: str | None = None


class ErrorListResponse(BaseModel):
    items: list[ErrorGroupSummary]
    total: int
    limit: int
    offset: int


class ErrorEventListResponse(BaseModel):
    items: list[ErrorEventDetail]
    total: int
    limit: int
    offset: int
