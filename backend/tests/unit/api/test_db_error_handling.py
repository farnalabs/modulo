"""Unit tests for modulo.api.db_error_handling — the ``handle_db_errors`` decorator.

QA lens pass (correctness, bugs, maintainability, deps) on the decorator that
490+ call sites across the route layer rely on to translate DB/validation
failures into HTTP exceptions. ``handle_db_errors`` is applied to 400+ API route
handlers and is the single point that maps low-level DB/pydantic exceptions to
stable HTTP statuses and user-facing details. These tests lock the decorator
contract directly so a mapping change is caught at the unit layer: exact
exception-type → status-code mapping, the fixed detail strings,
``asyncio.CancelledError`` passthrough, ``HTTPException`` passthrough, the
``from None`` context suppression, the metadata-preserving ``@wraps`` behaviour,
the ``log_prefix`` used in structured logs, and success-path value passthrough.
"""

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

import pydantic
import pytest
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.db_error_handling import handle_db_errors


def _integrity_error() -> IntegrityError:
    return IntegrityError("stmt", {}, Exception("mock constraint violation"))


def _programming_error() -> ProgrammingError:
    return ProgrammingError("stmt", {}, Exception("mock table does not exist"))


async def _run(coro: Awaitable[Any]) -> Any:
    return await coro


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


class TestDecoration:
    def test_preserves_function_metadata(self) -> None:
        @handle_db_errors("test.meta")
        async def my_endpoint() -> str:
            """my endpoint docstring."""
            return "ok"

        assert my_endpoint.__name__ == "my_endpoint"
        assert my_endpoint.__doc__ == "my endpoint docstring."

    async def test_returns_result_on_success(self) -> None:
        @handle_db_errors("test.success")
        async def my_endpoint(value: int) -> int:
            return value * 2

        assert await _run(my_endpoint(21)) == 42

    async def test_passes_through_args_and_kwargs(self) -> None:
        seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

        @handle_db_errors("test.args")
        async def my_endpoint(*args: object, **kwargs: object) -> str:
            seen.append((args, kwargs))
            return "ok"

        await _run(my_endpoint(1, 2, org_id="abc"))
        assert seen == [((1, 2), {"org_id": "abc"})]

    def test_decorator_factory_returns_callable(self) -> None:
        decorator: object = handle_db_errors("test.factory")
        assert callable(decorator)


class TestExceptionMapping:
    async def test_integrity_error_maps_to_409(self) -> None:
        @handle_db_errors("test.integrity")
        async def fail() -> None:
            raise _integrity_error()

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_409_CONFLICT
        assert "Resource conflict" in excinfo.value.detail

    async def test_programming_error_maps_to_501(self) -> None:
        @handle_db_errors("test.programming")
        async def fail() -> None:
            raise _programming_error()

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_501_NOT_IMPLEMENTED
        assert "database migrations" in excinfo.value.detail

    async def test_sqlalchemy_error_maps_to_503(self) -> None:
        @handle_db_errors("test.sqla")
        async def fail() -> None:
            raise SQLAlchemyError("mock", "mock", "mock")

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "unavailable" in excinfo.value.detail.lower()

    async def test_pydantic_validation_error_maps_to_422(self) -> None:
        @handle_db_errors("test.validation")
        async def fail() -> None:
            class _LocalModel(pydantic.BaseModel):
                name: str

            _LocalModel()  # type: ignore[call-arg]

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "validation" in excinfo.value.detail.lower()

    async def test_generic_exception_maps_to_500(self) -> None:
        @handle_db_errors("test.generic")
        async def fail() -> None:
            raise RuntimeError("boom")

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "unexpected error" in excinfo.value.detail.lower()

    async def test_cancelled_error_is_never_wrapped(self) -> None:
        @handle_db_errors("test.cancel")
        async def fail() -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await _run(fail())

    async def test_http_exception_passthrough_preserves_status_and_detail(self) -> None:
        @handle_db_errors("test.http")
        async def fail() -> None:
            raise HTTPException(status_code=418, detail="teapot original")

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == 418
        assert excinfo.value.detail == "teapot original"

    async def test_http_exception_with_headers_passthrough(self) -> None:
        @handle_db_errors("test.http_headers")
        async def fail() -> None:
            raise HTTPException(status_code=429, detail="slow", headers={"Retry-After": "30"})

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.headers == {"Retry-After": "30"}


class TestLogging:
    async def test_uses_log_prefix_in_integrity_log(self, caplog: pytest.LogCaptureFixture) -> None:
        @handle_db_errors("prefix.integrity")
        async def fail() -> None:
            raise _integrity_error()

        with caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"), pytest.raises(HTTPException):
            await _run(fail())

        messages = [r.getMessage() for r in caplog.records]
        assert "prefix.integrity.integrity_error" in messages

    async def test_uses_log_prefix_in_programming_log(self, caplog: pytest.LogCaptureFixture) -> None:
        @handle_db_errors("prefix.prog")
        async def fail() -> None:
            raise _programming_error()

        with caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"), pytest.raises(HTTPException):
            await _run(fail())

        messages = [r.getMessage() for r in caplog.records]
        assert "prefix.prog.programming_error" in messages

    async def test_uses_log_prefix_in_generic_log(self, caplog: pytest.LogCaptureFixture) -> None:
        @handle_db_errors("prefix.generic")
        async def fail() -> None:
            raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"), pytest.raises(HTTPException):
            await _run(fail())

        messages = [r.getMessage() for r in caplog.records]
        assert "prefix.generic.unexpected_error" in messages


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

    async def test_logs_error_with_log_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"),
            pytest.raises(HTTPException),
        ):
            await _endpoint(IntegrityError("stmt", {}, Exception("duplicate")))()
        assert "test.endpoint.integrity_error" in caplog.text
