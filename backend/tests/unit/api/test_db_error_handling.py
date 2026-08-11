"""Unit tests for modulo.api.db_error_handling — the ``handle_db_errors`` decorator.

QA lens pass (correctness, bugs, maintainability, deps) on the decorator that
490+ call sites across the route layer rely on to translate DB/validation
failures into HTTP exceptions. The decorator was only exercised indirectly
through end-to-end endpoint tests (``test_error_handling.py``); these tests
lock the decorator contract directly so a mapping change is caught at the unit
layer: exact exception-type → status-code mapping, the fixed detail strings,
``asyncio.CancelledError`` passthrough, ``HTTPException`` passthrough, the
``log_prefix`` used in structured logs, and success-path value passthrough.
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


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


class TestDecoration:
    def test_preserves_function_metadata(self) -> None:
        @handle_db_errors("test.meta")
        async def my_endpoint() -> str:
            """my endpoint docstring."""
            return "ok"

        assert my_endpoint.__name__ == "my_endpoint"
        assert my_endpoint.__doc__ == "my endpoint docstring."

    def test_returns_result_on_success(self) -> None:
        @handle_db_errors("test.success")
        async def my_endpoint(value: int) -> int:
            return value * 2

        assert _run(my_endpoint(21)) == 42

    def test_passes_through_args_and_kwargs(self) -> None:
        seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

        @handle_db_errors("test.args")
        async def my_endpoint(*args: object, **kwargs: object) -> str:
            seen.append((args, kwargs))
            return "ok"

        _run(my_endpoint(1, 2, org_id="abc"))
        assert seen == [((1, 2), {"org_id": "abc"})]

    def test_decorator_factory_returns_callable(self) -> None:
        decorator: object = handle_db_errors("test.factory")
        assert callable(decorator)


class TestExceptionMapping:
    def test_integrity_error_maps_to_409(self) -> None:
        @handle_db_errors("test.integrity")
        async def fail() -> None:
            raise _integrity_error()

        with pytest.raises(HTTPException) as excinfo:
            _run(fail())
        assert excinfo.value.status_code == status.HTTP_409_CONFLICT
        assert "Resource conflict" in excinfo.value.detail

    def test_programming_error_maps_to_501(self) -> None:
        @handle_db_errors("test.programming")
        async def fail() -> None:
            raise _programming_error()

        with pytest.raises(HTTPException) as excinfo:
            _run(fail())
        assert excinfo.value.status_code == status.HTTP_501_NOT_IMPLEMENTED
        assert "database migrations" in excinfo.value.detail

    def test_sqlalchemy_error_maps_to_503(self) -> None:
        @handle_db_errors("test.sqla")
        async def fail() -> None:
            raise SQLAlchemyError("mock", "mock", "mock")

        with pytest.raises(HTTPException) as excinfo:
            _run(fail())
        assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "unavailable" in excinfo.value.detail.lower()

    def test_pydantic_validation_error_maps_to_422(self) -> None:
        @handle_db_errors("test.validation")
        async def fail() -> None:
            class _Model(pydantic.BaseModel):
                name: str

            _Model()  # type: ignore[call-arg]

        with pytest.raises(HTTPException) as excinfo:
            _run(fail())
        assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "validation" in excinfo.value.detail.lower()

    def test_generic_exception_maps_to_500(self) -> None:
        @handle_db_errors("test.generic")
        async def fail() -> None:
            raise RuntimeError("boom")

        with pytest.raises(HTTPException) as excinfo:
            _run(fail())
        assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "unexpected error" in excinfo.value.detail.lower()

    def test_cancelled_error_is_never_wrapped(self) -> None:
        @handle_db_errors("test.cancel")
        async def fail() -> None:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            _run(fail())

    def test_http_exception_passthrough_preserves_status_and_detail(self) -> None:
        @handle_db_errors("test.http")
        async def fail() -> None:
            raise HTTPException(status_code=418, detail="teapot original")

        with pytest.raises(HTTPException) as excinfo:
            _run(fail())
        assert excinfo.value.status_code == 418
        assert excinfo.value.detail == "teapot original"

    def test_http_exception_with_headers_passthrough(self) -> None:
        @handle_db_errors("test.http_headers")
        async def fail() -> None:
            raise HTTPException(status_code=429, detail="slow", headers={"Retry-After": "30"})

        with pytest.raises(HTTPException) as excinfo:
            _run(fail())
        assert excinfo.value.headers == {"Retry-After": "30"}


class TestLogging:
    def test_uses_log_prefix_in_integrity_log(self, caplog: pytest.LogCaptureFixture) -> None:
        @handle_db_errors("prefix.integrity")
        async def fail() -> None:
            raise _integrity_error()

        with caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"), pytest.raises(HTTPException):
            _run(fail())

        messages = [r.getMessage() for r in caplog.records]
        assert "prefix.integrity.integrity_error" in messages

    def test_uses_log_prefix_in_programming_log(self, caplog: pytest.LogCaptureFixture) -> None:
        @handle_db_errors("prefix.prog")
        async def fail() -> None:
            raise _programming_error()

        with caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"), pytest.raises(HTTPException):
            _run(fail())

        messages = [r.getMessage() for r in caplog.records]
        assert "prefix.prog.programming_error" in messages

    def test_uses_log_prefix_in_generic_log(self, caplog: pytest.LogCaptureFixture) -> None:
        @handle_db_errors("prefix.generic")
        async def fail() -> None:
            raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"), pytest.raises(HTTPException):
            _run(fail())

        messages = [r.getMessage() for r in caplog.records]
        assert "prefix.generic.unexpected_error" in messages
