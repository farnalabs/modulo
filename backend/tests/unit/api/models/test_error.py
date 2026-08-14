"""Unit tests for modulo.api.models.error.

QA lens pass (correctness, bugs, maintainability, deps) on the error-ingestion
and error-dashboard schemas in ``api/models/error.py``. These schemas sit on
the public ingestion boundary (``/api/v1/errors``) and the dashboard/admin
surface; they had no dedicated unit test file. These tests lock the validator
surface (level/source allow-lists, non-empty message trimming, breadcrumbs cap,
batch size bounds), the backward-compatible ``ErrorResponse.model_dump``
override, and the dashboard/admin response shapes so a schema drift is caught
at the unit layer.
"""

from __future__ import annotations

from typing import Any

import pydantic
import pytest

from modulo.api.models.error import (
    ErrorDetail,
    ErrorEventDetail,
    ErrorEventInput,
    ErrorEventListResponse,
    ErrorGroupDetail,
    ErrorGroupSummary,
    ErrorGroupUpdate,
    ErrorIngestRequest,
    ErrorIngestResponse,
    ErrorListResponse,
    ErrorResponse,
)


class TestErrorDetail:
    def test_requires_code_and_message(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ErrorDetail()  # type: ignore[call-arg]

    def test_full_fields(self) -> None:
        detail = ErrorDetail(code="not_found", message="Missing", detail="Extra", request_id="req-1")
        assert detail.code == "not_found"
        assert detail.message == "Missing"
        assert detail.detail == "Extra"
        assert detail.request_id == "req-1"

    def test_optional_fields_default_to_none(self) -> None:
        err = ErrorDetail(code="x", message="y")
        assert err.detail is None
        assert err.request_id is None

    def test_dumped_without_omitting_optionals(self) -> None:
        detail = ErrorDetail(code="bad_request", message="Bad")
        assert detail.model_dump() == {"code": "bad_request", "message": "Bad", "detail": None, "request_id": None}

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

    def test_model_dump_preserves_top_level_error_block(self) -> None:
        resp = ErrorResponse(error=ErrorDetail(code="forbidden", message="No access"))
        dumped = resp.model_dump()
        assert dumped["error"] == {"code": "forbidden", "message": "No access", "detail": None, "request_id": None}
        assert dumped["detail"] == "No access"

    def test_model_dump_json_mode_keeps_backward_compatible_detail(self) -> None:
        resp = ErrorResponse(error=ErrorDetail(code="error", message="boom", request_id="r-1"))
        import json

        dumped = resp.model_dump(mode="json")
        assert dumped["detail"] == "boom"
        assert json.dumps(dumped)


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


def _summary(**overrides: Any) -> dict[str, Any]:
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


class TestErrorGroupSummary:
    def test_group_summary_surface(self) -> None:
        summary = ErrorGroupSummary(**_summary())
        assert summary.fingerprint == "fp-1"
        assert summary.count == 3

    def test_round_trip(self) -> None:
        summary = ErrorGroupSummary(
            id="grp-1",
            fingerprint="fp:abc",
            status="open",
            level_peak="critical",
            count=3,
            first_seen="2026-01-01T00:00:00Z",
            last_seen="2026-01-02T00:00:00Z",
            sample_message="boom",
        )
        assert summary.id == "grp-1"
        assert summary.fingerprint == "fp:abc"
        assert summary.status == "open"
        assert summary.count == 3


class TestErrorEventDetail:
    def test_all_fields(self) -> None:
        event = ErrorEventDetail(
            id="evt-1",
            level="error",
            message="boom",
            stacktrace="trace",
            context_json={"path": "/x"},
            source="backend",
            environment="production",
            version="1.0.0",
            breadcrumbs=[{"msg": "step"}],
            created_at="2026-01-01T00:00:00Z",
        )
        assert event.stacktrace == "trace"
        assert event.context_json == {"path": "/x"}
        assert event.breadcrumbs == [{"msg": "step"}]

    def test_optional_fields_default_to_none(self) -> None:
        event = ErrorEventDetail(
            id="evt-1",
            level="error",
            message="boom",
            source="backend",
            created_at="2026-01-01T00:00:00Z",
        )
        assert event.stacktrace is None
        assert event.context_json is None
        assert event.environment is None
        assert event.breadcrumbs is None


class TestErrorGroupDetail:
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

    def test_round_trip_with_sample_event(self) -> None:
        detail = ErrorGroupDetail(
            id="grp-1",
            fingerprint="fp",
            status="open",
            level_peak="error",
            count=1,
            first_seen="2026-01-01T00:00:00Z",
            last_seen="2026-01-01T00:00:00Z",
            sample_event=ErrorEventDetail(
                id="evt-1",
                level="error",
                message="boom",
                source="backend",
                created_at="2026-01-01T00:00:00Z",
            ),
            assigned_to="user-1",
        )
        assert detail.sample_event is not None
        assert detail.sample_event.message == "boom"
        assert detail.assigned_to == "user-1"

    def test_sample_event_defaults_to_none(self) -> None:
        detail = ErrorGroupDetail(
            id="grp-1",
            fingerprint="fp",
            status="open",
            level_peak="error",
            count=1,
            first_seen="2026-01-01T00:00:00Z",
            last_seen="2026-01-01T00:00:00Z",
        )
        assert detail.sample_event is None
        assert detail.assigned_to is None


class TestErrorGroupUpdate:
    def test_all_optional_and_empty_by_default(self) -> None:
        update = ErrorGroupUpdate()
        assert update.status is None
        assert update.assigned_to is None

    def test_partial_update(self) -> None:
        update = ErrorGroupUpdate(status="resolved")
        assert update.status == "resolved"
        assert update.assigned_to is None

    def test_unknown_fields_silently_ignored(self) -> None:
        update = ErrorGroupUpdate(status="resolved", unknown_field=True)
        assert update.model_dump() == {"status": "resolved", "assigned_to": None}


class TestErrorListResponse:
    def test_list_response_surface(self) -> None:
        resp = ErrorListResponse(items=[ErrorGroupSummary(**_summary())], total=1, limit=20, offset=0)
        assert resp.total == 1
        assert resp.limit == 20
        assert resp.offset == 0

    def test_round_trip(self) -> None:
        items = [
            ErrorGroupSummary(
                id="grp-1",
                fingerprint="fp:1",
                status="open",
                level_peak="error",
                count=1,
                first_seen="2026-01-01T00:00:00Z",
                last_seen="2026-01-01T00:00:00Z",
                sample_message="a",
            )
        ]
        resp = ErrorListResponse(items=items, total=1, limit=10, offset=0)
        assert resp.total == 1
        assert resp.limit == 10
        assert resp.offset == 0
        assert len(resp.items) == 1

    def test_empty_items(self) -> None:
        resp = ErrorListResponse(items=[], total=0, limit=10, offset=0)
        assert not resp.items


class TestErrorEventListResponse:
    def test_round_trip(self) -> None:
        items = [
            ErrorEventDetail(
                id="evt-1",
                level="error",
                message="boom",
                source="backend",
                created_at="2026-01-01T00:00:00Z",
            )
        ]
        resp = ErrorEventListResponse(items=items, total=1, limit=25, offset=0)
        assert resp.total == 1
        assert resp.limit == 25
        assert resp.offset == 0
        assert len(resp.items) == 1

    def test_empty_items(self) -> None:
        resp = ErrorEventListResponse(items=[], total=0, limit=25, offset=0)
        assert not resp.items
