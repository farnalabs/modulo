"""Unit tests for modulo.api.db_error_handling — the handle_db_errors decorator.

QA lens pass (correctness, bugs, maintainability, deps) on the cross-cutting DB
error decorator. ``handle_db_errors`` is applied to 400+ API route handlers and
is the single point that maps low-level DB/pydantic exceptions to stable HTTP
statuses and user-facing details. Because routes branch on nothing else, a
status-code or detail-string change here silently ripples across every admin,
pipeline, connector and cost route. These tests lock the mapping table, the
``from None`` context suppression, HTTPException/CancelledError passthrough, the
metadata-preserving ``@wraps`` behaviour, and the log-prefix contract.
"""

import asyncio
import logging
from typing import Any

import pydantic
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.db_error_handling import handle_db_errors


class _Model(pydantic.BaseModel):
    value: int


def _validation_error() -> pydantic.ValidationError:
    with pytest.raises(pydantic.ValidationError) as excinfo:
        _Model.model_validate({"value": "not-an-int"})
    return excinfo.value


def _endpoint(exc: BaseException | None = None) -> Any:
    @handle_db_errors("test.endpoint")
    async def endpoint(value: int = 1) -> int:
        if exc is not None:
            raise exc
        return value

    return endpoint


class TestHandleDbErrors:
    async def test_success_returns_value_and_forwards_args(self) -> None:
        endpoint = _endpoint()
        assert await endpoint(value=7) == 7

    async def test_integrity_error_maps_to_409(self) -> None:
        endpoint = _endpoint(IntegrityError("stmt", {}, Exception("duplicate")))
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == 409
        assert excinfo.value.detail == "Resource conflict. The operation could not be completed."

    async def test_programming_error_maps_to_501(self) -> None:
        endpoint = _endpoint(ProgrammingError("stmt", {}, Exception("no column")))
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == 501
        assert excinfo.value.detail == "Feature is not available. Run database migrations to enable it."

    async def test_generic_sqlalchemy_error_maps_to_503(self) -> None:
        endpoint = _endpoint(SQLAlchemyError("db down"))
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail == "Database temporarily unavailable."

    async def test_pydantic_validation_error_maps_to_422(self) -> None:
        endpoint = _endpoint(_validation_error())
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail == "Data validation failed."

    async def test_http_exception_passes_through_unchanged(self) -> None:
        original = HTTPException(status_code=418, detail="teapot")
        endpoint = _endpoint(original)
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value is original

    async def test_cancelled_error_is_not_swallowed(self) -> None:
        endpoint = _endpoint(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await endpoint()

    async def test_unexpected_error_maps_to_500(self) -> None:
        endpoint = _endpoint(RuntimeError("boom"))
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == 500
        assert excinfo.value.detail == "An unexpected error occurred."

    async def test_http_exception_suppresses_original_context(self) -> None:
        endpoint = _endpoint(IntegrityError("stmt", {}, Exception("orig")))
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == 409
        assert excinfo.value.__suppress_context__ is True

    def test_wraps_preserves_endpoint_metadata(self) -> None:
        @handle_db_errors("test.endpoint")
        async def documented_endpoint() -> None:
            """Locked by QA lens pass."""

        assert documented_endpoint.__name__ == "documented_endpoint"
        assert "Locked by QA lens pass." in (documented_endpoint.__doc__ or "")

    def test_logs_error_with_log_prefix(self, caplog: Any) -> None:
        with (
            caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"),
            pytest.raises(HTTPException),
        ):
            asyncio.run(_endpoint(IntegrityError("stmt", {}, Exception("duplicate")))())
        assert "test.endpoint.integrity_error" in caplog.text
