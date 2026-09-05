from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

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
        if v not in ("backend", "frontend", "celery", "saq"):
            msg = f"Invalid source '{v}'. Must be one of: backend, frontend, celery, saq"
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


class SchedulerStarvationItem(BaseModel):
    """One pipeline with capacity-starved pending runs (FAR-604).

    Surfaced on the error dashboard because pre-terminal pending runs never
    produce error events (the dashboard keys off ingested errors), so a
    pipeline stuck at its capacity cap is otherwise invisible.
    """

    pipeline_id: str
    pipeline_name: str | None = None
    pending_count: int
    oldest_created_at: str
    oldest_age_minutes: float


class SchedulerStarvationResponse(BaseModel):
    items: list[SchedulerStarvationItem]
    total: int
    threshold_minutes: int
