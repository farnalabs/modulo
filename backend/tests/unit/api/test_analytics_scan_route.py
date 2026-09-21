"""Unit tests for the /api/v1/analytics/scan route and its stream helpers.

The full HTTP behaviour of the scan surface (RLS isolation, 401/402 gates,
CSV/NDJSON wire shape) is exercised against a real Postgres in
``tests/integration/test_analytics_endpoint.py``. That suite is not part of the
``coverage.xml`` the changed-lines coverage gate consumes (the gate reads the
``tests/unit`` run), so this module pins the route's unit-level contract:

  * ``_analytics_scan_filters`` — the scan's typed filter surface has no
    pagination knob (``limit=0``) and binds pipeline ids as a tuple.
  * ``_csv_stream_line`` — header vs row vs blank, properly CSV-quoted.
  * ``_scan_body`` — NDJSON (default) and CSV framing, including the primed
    first row that surfaces pre-stream errors and must never be dropped.
  * ``analytics_scan`` — prime-then-stream error mapping and the two response
    shapes, with the service/session boundary patched (no DB).

Hermetic: the route is a thin adapter, so ``stream_export_facts`` and the
session factory are patched at the route-module boundary.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.responses import StreamingResponse

from modulo.api.routes import analytics as route
from modulo.auth.jwt import TenantPrincipal
from modulo.core.analytics.builder import AnalyticsGroupBy, AnalyticsStatus
from modulo.core.analytics.service import (
    EXPORT_COLUMN_NAMES,
    AnalyticsParams,
    AnalyticsRateLimitedError,
)

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000a2")


def _principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="scan-user",
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        org_role="admin",
    )


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"run_id": str(uuid.uuid4()), "status": "complete", "error_code": None}
    base.update(overrides)
    return base


async def _stream(*items: Any) -> AsyncIterator[dict[str, Any]]:
    for item in items:
        yield item


async def _raising(exc: Exception) -> AsyncIterator[dict[str, Any]]:
    raise exc
    yield  # pragma: no cover - unreachable, keeps this an async generator


async def _call_scan(
    *,
    format: str,
    stream_factory: Any,
    team_ids: Any = None,
) -> StreamingResponse:
    """Invoke the route handler directly with the DB boundary patched out."""
    with (
        patch.object(route, "_resolve_scoped_team_ids", new=AsyncMock(return_value=team_ids)),
        patch.object(route, "_analytics_session_factory", return_value=MagicMock()),
        patch.object(route, "stream_export_facts", side_effect=stream_factory),
    ):
        response = await route.analytics_scan(
            format=format,
            params=AnalyticsParams(),
            settings=MagicMock(),
            principal=_principal(),
            _=None,
        )
    assert isinstance(response, StreamingResponse)
    return response


async def _body(response: StreamingResponse) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


class TestScanFilters:
    """The scan filter dependency is export's surface minus pagination."""

    def test_defaults_are_unpaged_day_granularity(self) -> None:
        params = route._analytics_scan_filters(
            dimension=None,
            trigger_type=None,
            status=None,
            pipeline_id=None,
            error_code=None,
            folder_id=None,
            date_from=None,
            date_to=None,
        )
        assert params.group_by == AnalyticsGroupBy.DAY
        assert params.auto_granularity is False
        assert params.limit == 0, "a scan streams the whole set — there is no pagination knob"
        assert not params.pipeline_ids

    def test_carries_typed_filters_and_binds_pipeline_ids(self) -> None:
        pipeline_id = uuid.uuid4()
        params = route._analytics_scan_filters(
            status=AnalyticsStatus.FAILED,
            pipeline_id=[pipeline_id],
            error_code="executor_stalled",
        )
        assert params.status == AnalyticsStatus.FAILED
        assert params.pipeline_ids == (pipeline_id,)
        assert params.error_code == "executor_stalled"
        assert params.limit == 0


class TestCsvStreamLine:
    """One CSV line, properly quoted — header, row, and the blank non-case."""

    def test_header_emits_the_export_columns(self) -> None:
        line = route._csv_stream_line(header=True)
        assert line.strip().split(",") == list(EXPORT_COLUMN_NAMES)
        assert line.endswith(("\r\n", "\n"))

    def test_row_quotes_and_fills_missing_columns(self) -> None:
        line = route._csv_stream_line(header=False, item={"run_id": "r1", "status": "complete"})
        cells = line.rstrip("\r\n").split(",")
        assert cells[EXPORT_COLUMN_NAMES.index("run_id")] == "r1"
        assert cells[EXPORT_COLUMN_NAMES.index("status")] == "complete"
        assert not cells[EXPORT_COLUMN_NAMES.index("team_name")]

    def test_neither_header_nor_item_is_blank(self) -> None:
        assert not route._csv_stream_line(header=False, item=None)


class TestScanBody:
    """``_scan_body`` frames the primed generator as NDJSON or CSV."""

    async def test_json_emits_primed_first_then_rest(self) -> None:
        first = _row()
        rest = [_row(), _row()]
        chunks = [chunk async for chunk in route._scan_body(_stream(*rest), first=first, format="json")]
        assert [json.loads(chunk.rstrip("\n")) for chunk in chunks] == [first, *rest]

    async def test_json_without_primed_row_starts_at_the_stream(self) -> None:
        rest = [_row()]
        chunks = [chunk async for chunk in route._scan_body(_stream(*rest), first=None, format="json")]
        assert [json.loads(chunk.rstrip("\n")) for chunk in chunks] == rest

    async def test_csv_emits_header_primed_row_then_rest(self) -> None:
        first = {"run_id": "r1"}
        rest = [{"run_id": "r2"}]
        body = "".join([chunk async for chunk in route._scan_body(_stream(*rest), first=first, format="csv")])
        lines = body.splitlines()
        assert lines[0].startswith("run_id,")
        assert lines[1].startswith("r1")
        assert lines[2].startswith("r2")

    async def test_csv_without_primed_row_emits_header_only(self) -> None:
        body = "".join([chunk async for chunk in route._scan_body(_stream(), first=None, format="csv")])
        assert body.splitlines()[0].startswith("run_id,")
        assert len(body.splitlines()) == 1


class TestScanRoute:
    """The route primes the service then streams; DB is patched out."""

    async def test_json_response_is_ndjson_and_keeps_the_primed_row(self) -> None:
        first = _row(run_id="r1")
        second = _row(run_id="r2")
        captured: dict[str, Any] = {}

        def _factory(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
            captured.update(kwargs)
            return _stream(first, second)

        response = await _call_scan(format="json", stream_factory=_factory)
        assert response.media_type == route._SCAN_NDJSON_MEDIA_TYPE
        payloads = [json.loads(line) for line in (await _body(response)).splitlines()]
        assert [p["run_id"] for p in payloads] == ["r1", "r2"]
        assert captured["org_id"] == _ORG
        assert captured["account_id"] == _ACCOUNT

    async def test_csv_response_is_a_named_attachment(self) -> None:
        response = await _call_scan(format="csv", stream_factory=lambda **_: _stream(_row(run_id="r1")))
        assert response.media_type == "text/csv"
        assert response.headers["content-disposition"] == 'attachment; filename="analytics-scan.csv"'
        body = await _body(response)
        assert body.splitlines()[0].startswith("run_id,")
        assert "r1" in body

    async def test_empty_stream_returns_an_empty_body(self) -> None:
        response = await _call_scan(format="json", stream_factory=lambda **_: _stream())
        assert not await _body(response)

    async def test_empty_stream_csv_returns_header_only(self) -> None:
        response = await _call_scan(format="csv", stream_factory=lambda **_: _stream())
        assert (await _body(response)).splitlines()[0].startswith("run_id,")

    async def test_team_ids_are_threaded_from_the_resolver(self) -> None:
        team_ids = (uuid.uuid4(),)
        captured: dict[str, Any] = {}

        def _factory(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
            captured.update(kwargs)
            return _stream()

        response = await _call_scan(format="json", stream_factory=_factory, team_ids=team_ids)
        await _body(response)
        assert captured["team_ids"] == team_ids

    async def test_service_error_maps_to_an_http_status(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await _call_scan(
                format="json",
                stream_factory=lambda **_: _raising(AnalyticsRateLimitedError("over budget")),
            )
        assert exc_info.value.status_code == 429

    async def test_http_exception_passes_through_untouched(self) -> None:
        original = HTTPException(status_code=418, detail="teapot")
        with pytest.raises(HTTPException) as exc_info:
            await _call_scan(format="json", stream_factory=lambda **_: _raising(original))
        assert exc_info.value is original
