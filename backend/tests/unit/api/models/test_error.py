"""Unit tests for modulo.api.models.error — error response + dashboard schemas.

QA lens pass (correctness, bugs, maintainability, deps) on the error API
schemas. The validation contracts of ``ErrorEventInput`` / ``ErrorIngestRequest``
are already exercised by ``tests/unit/error_tracking/test_error_ingestion.py``;
this file locks the remaining uncovered contracts: the backward-compatible
``ErrorResponse`` top-level ``detail`` key, and the dashboard/admin schemas
(``ErrorGroupSummary``, ``ErrorEventDetail``, ``ErrorGroupDetail``,
``ErrorGroupUpdate``, list responses) that had no direct coverage.
"""

from __future__ import annotations

from modulo.api.models.error import (
    ErrorDetail,
    ErrorEventDetail,
    ErrorEventListResponse,
    ErrorGroupDetail,
    ErrorGroupSummary,
    ErrorGroupUpdate,
    ErrorListResponse,
    ErrorResponse,
)

# ---------------------------------------------------------------------------
# ErrorDetail / ErrorResponse (backward-compatible top-level detail key)
# ---------------------------------------------------------------------------


class TestErrorDetail:
    def test_full_fields(self) -> None:
        detail = ErrorDetail(code="not_found", message="Missing", detail="Extra", request_id="req-1")
        assert detail.code == "not_found"
        assert detail.message == "Missing"
        assert detail.detail == "Extra"
        assert detail.request_id == "req-1"

    def test_optional_fields_default_to_none(self) -> None:
        detail = ErrorDetail(code="bad_request", message="Bad")
        assert detail.detail is None
        assert detail.request_id is None

    def test_dumped_without_omitting_optionals(self) -> None:
        detail = ErrorDetail(code="bad_request", message="Bad")
        assert detail.model_dump() == {"code": "bad_request", "message": "Bad", "detail": None, "request_id": None}


class TestErrorResponse:
    def test_nests_error(self) -> None:
        resp = ErrorResponse(error=ErrorDetail(code="conflict", message="Exists"))
        assert resp.error.code == "conflict"
        assert resp.error.message == "Exists"

    def test_model_dump_keeps_backward_compatible_top_level_detail(self) -> None:
        resp = ErrorResponse(error=ErrorDetail(code="unauthorized", message="Login required", request_id="req-9"))
        dumped = resp.model_dump()
        assert dumped["error"]["message"] == "Login required"
        assert dumped["detail"] == "Login required"

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


# ---------------------------------------------------------------------------
# Dashboard / admin schemas
# ---------------------------------------------------------------------------


class TestErrorGroupSummary:
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
        update = ErrorGroupUpdate(status="resolved", unknown_field=True)  # type: ignore[call-arg]
        assert update.model_dump() == {"status": "resolved", "assigned_to": None}


class TestErrorListResponse:
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
        assert resp.items == []


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
        assert resp.items == []
