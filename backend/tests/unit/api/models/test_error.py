"""Unit tests for modulo.api.models.error.

QA lens pass (correctness, bugs, maintainability, deps) on the error-ingestion
and error-dashboard schemas in ``api/models/error.py``. These schemas sit on
the public ingestion boundary (``/api/v1/errors``) and the dashboard/admin
surface; they had no dedicated unit test file. These tests lock the validator
surface (level/source allow-lists, non-empty message trimming, breadcrumbs cap,
batch size bounds) and the backward-compatible ``ErrorResponse.model_dump``
override so a schema drift is caught at the unit layer.
"""

from typing import Any

import pydantic
import pytest

from modulo.api.models.error import (
    ErrorDetail,
    ErrorEventInput,
    ErrorGroupDetail,
    ErrorGroupSummary,
    ErrorIngestRequest,
    ErrorIngestResponse,
    ErrorListResponse,
    ErrorResponse,
)


class TestErrorDetail:
    def test_requires_code_and_message(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ErrorDetail()  # type: ignore[call-arg]

    def test_optional_fields_default_to_none(self) -> None:
        err = ErrorDetail(code="x", message="y")
        assert err.detail is None
        assert err.request_id is None

    def test_round_trips_all_fields(self) -> None:
        err = ErrorDetail(code="auth", message="denied", detail="no token", request_id="rid-1")
        assert err.model_dump() == {
            "code": "auth",
            "message": "denied",
            "detail": "no token",
            "request_id": "rid-1",
        }


class TestErrorResponse:
    def test_nests_error(self) -> None:
        resp = ErrorResponse(error=ErrorDetail(code="c", message="m"))
        assert resp.error.code == "c"
        assert resp.error.message == "m"

    def test_model_dump_includes_backward_compatible_detail(self) -> None:
        resp = ErrorResponse(error=ErrorDetail(code="c", message="m"))
        dumped = resp.model_dump()
        assert dumped["error"]["message"] == "m"
        assert dumped["detail"] == "m"

    def test_model_dump_keeps_top_level_detail_even_with_extra_fields(self) -> None:
        resp = ErrorResponse(error=ErrorDetail(code="c", message="boom", detail="nested"))
        dumped = resp.model_dump()
        assert dumped["detail"] == "boom"

    def test_model_dump_json_surface(self) -> None:
        resp = ErrorResponse(error=ErrorDetail(code="c", message="m"))
        dumped = resp.model_dump(mode="json")
        assert dumped["detail"] == "m"


class TestErrorEventInput:
    @pytest.mark.parametrize("level", ["error", "warning", "critical"])
    def test_accepts_valid_levels(self, level: str) -> None:
        err = ErrorEventInput(level=level, message="m", source="backend")
        assert err.level == level

    @pytest.mark.parametrize("level", ["info", "debug", "ERROR", ""])
    def test_rejects_invalid_levels(self, level: str) -> None:
        with pytest.raises(pydantic.ValidationError):
            ErrorEventInput(level=level, message="m", source="backend")

    @pytest.mark.parametrize("source", ["backend", "frontend", "celery", "saq"])
    def test_accepts_valid_sources(self, source: str) -> None:
        err = ErrorEventInput(level="error", message="m", source=source)
        assert err.source == source

    @pytest.mark.parametrize("source", ["worker", "mobile", "BACKEND"])
    def test_rejects_invalid_sources(self, source: str) -> None:
        with pytest.raises(pydantic.ValidationError):
            ErrorEventInput(level="error", message="m", source=source)

    def test_message_is_stripped(self) -> None:
        err = ErrorEventInput(level="error", message="  hello world  ", source="backend")
        assert err.message == "hello world"

    def test_empty_and_whitespace_messages_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ErrorEventInput(level="error", message="", source="backend")
        with pytest.raises(pydantic.ValidationError):
            ErrorEventInput(level="error", message="   ", source="backend")

    def test_optional_fields_default_to_none(self) -> None:
        err = ErrorEventInput(level="error", message="m", source="backend")
        assert err.stacktrace is None
        assert err.context_json is None
        assert err.environment is None
        assert err.version is None
        assert err.breadcrumbs is None

    def test_breadcrumbs_cap_at_50(self) -> None:
        many = [{"type": "log", "message": "x"} for _ in range(50)]
        err = ErrorEventInput(level="error", message="m", source="backend", breadcrumbs=many)
        assert len(err.breadcrumbs or []) == 50

        too_many = [*many, {"type": "log", "message": "x"}]
        with pytest.raises(pydantic.ValidationError):
            ErrorEventInput(level="error", message="m", source="backend", breadcrumbs=too_many)

    def test_none_breadcrumbs_allowed(self) -> None:
        err = ErrorEventInput(level="error", message="m", source="backend", breadcrumbs=None)
        assert err.breadcrumbs is None


class TestErrorIngestRequest:
    def test_events_required_with_min_one(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ErrorIngestRequest(events=[])

    def test_accepts_single_event(self) -> None:
        req = ErrorIngestRequest(events=[ErrorEventInput(level="error", message="m", source="backend")])
        assert len(req.events) == 1

    def test_batch_cap_at_20(self) -> None:
        events = [ErrorEventInput(level="error", message="m", source="backend") for _ in range(20)]
        req = ErrorIngestRequest(events=events)
        assert len(req.events) == 20

        with pytest.raises(pydantic.ValidationError):
            ErrorIngestRequest(events=[*events, ErrorEventInput(level="error", message="m", source="backend")])


class TestIngestAndSessionSchemas:
    def test_error_group_result_surface(self) -> None:
        from modulo.api.models.error import ErrorGroupResult

        result = ErrorGroupResult(group_id="g-1", is_new=True)
        assert result.group_id == "g-1"
        assert result.is_new is True

    def test_ingest_response_surface(self) -> None:
        from modulo.api.models.error import ErrorGroupResult

        resp = ErrorIngestResponse(results=[ErrorGroupResult(group_id="g-1", is_new=True)])
        assert resp.results[0].group_id == "g-1"

    def test_session_key_default_expiry(self) -> None:
        from modulo.api.models.error import SessionKeyResponse

        resp = SessionKeyResponse(key="abc")
        assert resp.expires_in_seconds == 3600


class TestDashboardSchemas:
    def _summary(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "id": "g-1",
            "fingerprint": "fp-1",
            "status": "open",
            "level_peak": "error",
            "count": 3,
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-02T00:00:00Z",
            "sample_message": "boom",
        }
        base.update(overrides)
        return base

    def test_group_summary_surface(self) -> None:
        summary = ErrorGroupSummary(**self._summary())
        assert summary.fingerprint == "fp-1"
        assert summary.count == 3

    def test_list_response_surface(self) -> None:
        resp = ErrorListResponse(items=[ErrorGroupSummary(**self._summary())], total=1, limit=20, offset=0)
        assert resp.total == 1
        assert resp.limit == 20
        assert resp.offset == 0

    def test_group_detail_optional_sample_event(self) -> None:
        detail = ErrorGroupDetail(
            id="g-1",
            fingerprint="fp-1",
            status="open",
            level_peak="error",
            count=1,
            first_seen="2026-01-01T00:00:00Z",
            last_seen="2026-01-02T00:00:00Z",
        )
        assert detail.sample_event is None
        assert detail.assigned_to is None
