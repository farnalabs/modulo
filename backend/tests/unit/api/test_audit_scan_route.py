"""Unit tests for the /api/v1/admin/audit/scan route and its stream helpers.

The scan surface (``GET /api/v1/admin/audit/scan``) is the deferral companion
to the paginated ``/export``: it streams the WHOLE org audit chain in ONE
response, using the same typed filters and ``audit_viewer`` + ``audit.manage``
gates. The full HTTP behaviour (RLS isolation, 401/403 gates, CSV/NDJSON wire
shape) is exercised in ``tests/unit/api/test_audit.py`` (TestClient) and the
core keyset-pagination contract in ``tests/unit/audit_logger/test_audit_logger.py``.
This module pins the route-level contract hermeticly (DB and session boundary
patched away):

  * ``_csv_stream_line`` — header vs row vs blank, properly CSV-quoted.
  * ``_scan_body`` — NDJSON (default) and CSV framing, including the primed
    first row that surfaces pre-stream errors and must never be dropped.
  * ``scan_chain_endpoint`` — prime-then-stream error mapping (invalid
    user_id -> 422, ProgrammingError -> 501, SQLAlchemyError -> 503) and the
    two response shapes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from starlette.responses import StreamingResponse

from modulo.api.routes import audit as route
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_logger import SCAN_CSV_COLUMNS

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
    base: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "event_type": "pipeline.run",
        "actor_user_id": str(_ACCOUNT),
        "resource_type": "pipeline",
        "resource_id": str(uuid.uuid4()),
        "payload_json": {"key": "value"},
        "request_id": "req-1",
        "previous_hash": "abc",
        "created_at": "2025-06-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


async def _stream(*items: Any) -> AsyncIterator[dict[str, Any]]:
    for item in items:
        yield item


async def _raising(exc: Exception) -> AsyncIterator[dict[str, Any]]:
    raise exc
    yield  # pragma: no cover - unreachable, keeps this an async generator


async def _call_scan(*, format: str, stream_factory: Any) -> StreamingResponse:
    """Invoke the route handler directly with the DB boundary patched out."""
    with (
        patch.object(route, "_audit_session_factory", return_value=MagicMock()),
        patch.object(route, "stream_export_chain", side_effect=stream_factory),
    ):
        response = await route.scan_chain_endpoint(
            format=format,
            event_type=None,
            actor_user_id=None,
            resource_type=None,
            from_date=None,
            to_date=None,
            settings=MagicMock(),
            principal=_principal(),
        )
    assert isinstance(response, StreamingResponse)
    return response


async def _body(response: StreamingResponse) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


class TestCsvStreamLine:
    """One CSV line, properly quoted — header, row, and the blank non-case."""

    def test_header_emits_the_scan_columns(self) -> None:
        line = route._csv_stream_line(header=True)
        assert line.strip().split(",") == list(SCAN_CSV_COLUMNS)
        assert line.endswith(("\r\n", "\n"))

    def test_row_quotes_and_fills_missing_columns(self) -> None:
        line = route._csv_stream_line(header=False, item={"id": "e1", "event_type": "pipeline.run"})
        cells = line.rstrip("\r\n").split(",")
        assert cells[SCAN_CSV_COLUMNS.index("id")] == "e1"
        assert cells[SCAN_CSV_COLUMNS.index("event_type")] == "pipeline.run"
        assert not cells[SCAN_CSV_COLUMNS.index("actor_user_id")]

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
        first = {"id": "e1"}
        rest = [{"id": "e2"}]
        body = "".join([chunk async for chunk in route._scan_body(_stream(*rest), first=first, format="csv")])
        lines = body.splitlines()
        assert lines[0].split(",")[0] == "created_at"
        assert lines[1].split(",")[1] == "e1"
        assert lines[2].split(",")[1] == "e2"

    async def test_csv_without_primed_row_emits_header_only(self) -> None:
        body = "".join([chunk async for chunk in route._scan_body(_stream(), first=None, format="csv")])
        assert body.splitlines()[0].split(",")[0] == "created_at"
        assert len(body.splitlines()) == 1


class TestScanRoute:
    """The route primes the service then streams; DB is patched out."""

    async def test_json_response_is_ndjson_and_keeps_the_primed_row(self) -> None:
        first = _row(id="e1")
        second = _row(id="e2")
        captured: dict[str, Any] = {}

        def _factory(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
            captured.update(kwargs)
            return _stream(first, second)

        response = await _call_scan(format="json", stream_factory=_factory)
        assert response.media_type == route._SCAN_NDJSON_MEDIA_TYPE
        payloads = [json.loads(line) for line in (await _body(response)).splitlines()]
        assert [p["id"] for p in payloads] == ["e1", "e2"]
        assert captured["org_id"] == _ORG

    async def test_csv_response_is_a_named_attachment(self) -> None:
        response = await _call_scan(format="csv", stream_factory=lambda **_: _stream(_row(id="e1")))
        assert response.media_type == "text/csv"
        assert response.headers["content-disposition"] == 'attachment; filename="audit-scan.csv"'
        body = await _body(response)
        assert body.splitlines()[0].split(",")[0] == "created_at"
        assert "e1" in body

    async def test_empty_stream_returns_an_empty_body(self) -> None:
        response = await _call_scan(format="json", stream_factory=lambda **_: _stream())
        assert not await _body(response)

    async def test_empty_stream_csv_returns_header_only(self) -> None:
        response = await _call_scan(format="csv", stream_factory=lambda **_: _stream())
        assert (await _body(response)).splitlines()[0].split(",")[0] == "created_at"

    async def test_invalid_user_id_maps_to_422_before_streaming(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await route.scan_chain_endpoint(
                format="json",
                event_type=None,
                actor_user_id="not-a-uuid",
                resource_type=None,
                from_date=None,
                to_date=None,
                settings=MagicMock(),
                principal=_principal(),
            )
        assert exc_info.value.status_code == 422

    async def test_programming_error_maps_to_501(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await _call_scan(
                format="json",
                stream_factory=lambda **_: _raising(ProgrammingError("stmt", {}, "table not found")),
            )
        assert exc_info.value.status_code == 501

    async def test_sqlalchemy_error_maps_to_503(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await _call_scan(
                format="json",
                stream_factory=lambda **_: _raising(SQLAlchemyError("connection failed")),
            )
        assert exc_info.value.status_code == 503

    async def test_http_exception_passes_through_untouched(self) -> None:
        original = HTTPException(status_code=418, detail="teapot")
        with pytest.raises(HTTPException) as exc_info:
            await _call_scan(format="json", stream_factory=lambda **_: _raising(original))
        assert exc_info.value is original

    async def test_unexpected_error_maps_to_500(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await _call_scan(format="json", stream_factory=lambda **_: _raising(RuntimeError("boom")))
        assert exc_info.value.status_code == 500
