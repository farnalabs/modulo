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
the ``log_prefix`` used in structured logs, the structured 503 backstop record
emitted on the ``SQLAlchemyError`` path, and success-path value passthrough.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pydantic
import pytest
from fastapi import HTTPException, status
from sqlalchemy.exc import (
    IntegrityError,
    InvalidRequestError,
    OperationalError,
    PendingRollbackError,
    ProgrammingError,
    SQLAlchemyError,
)

from modulo.api.db_error_handling import handle_db_errors
from modulo.core.exceptions import OrgDeletedError, PipelineNotRunnableError, TriggersPausedError


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


class TestStructured503Backstop:
    """The shared ``SQLAlchemyError`` backstop is the dominant 503 chokepoint —
    it wraps 400+ route handlers across the codebase, so a transient DB
    failure on ANY route funnels through it. A 503 raised here must emit the
    shared structured ``service_unavailable`` record (reason ``db_transient``,
    ERROR level) so a recurrence of the 2026-09-04 webhook 503 incident on a
    non-webhook route is diagnosable from the persisted reason trail."""

    async def test_sqlalchemy_error_emits_structured_service_unavailable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(HTTPException) as excinfo,
        ):
            await _run(_endpoint(SQLAlchemyError("pool timeout"))())

        assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert excinfo.value.detail == "Database temporarily unavailable."

        records = [r for r in caplog.records if r.name == "modulo.api.db_error_reporting"]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.ERROR
        payload = record.__dict__["service_unavailable"]
        assert payload["reason"] == "db_transient"
        assert payload["route"] == "test.endpoint"
        assert payload["exception_class"] == "SQLAlchemyError"
        assert payload["detail"] == "transient database error (handle_db_errors backstop)"

    async def test_typed_db_errors_do_not_emit_structured_503_record(self, caplog: pytest.LogCaptureFixture) -> None:
        """Only the 503 backstop emits the structured record: IntegrityError
        (409) and ProgrammingError (501) map to different outcomes and must
        not pollute the ``service_unavailable`` reason trail."""
        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(HTTPException) as integrity_exc,
        ):
            await _run(_endpoint(_integrity_error())())

        assert integrity_exc.value.status_code == status.HTTP_409_CONFLICT

        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(HTTPException) as programming_exc,
        ):
            await _run(_endpoint(_programming_error())())

        assert programming_exc.value.status_code == status.HTTP_501_NOT_IMPLEMENTED
        records = [r for r in caplog.records if r.name == "modulo.api.db_error_reporting"]
        assert not records


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


class _DriverError(Exception):
    """Stand-in for an asyncpg/psycopg driver error carrying a SQLSTATE."""

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class TestLockTimeoutMapping:
    """SQLSTATE 55P03 (``lock_not_available``) -> 409, not the generic 503.

    ``_reapply_team_gate_inside_mutation_txn`` now sets a bounded
    ``SET LOCAL lock_timeout`` before its ``FOR UPDATE``; when that expires the
    database raises 55P03. The DB is healthy in that case - only the row lock
    was busy - so the answer must say so (409 conflict, retry after the other
    change) instead of reading as a transient outage the client should retry
    immediately.
    """

    async def test_lock_not_available_maps_to_409_with_a_clear_detail(self) -> None:
        endpoint = _endpoint(OperationalError("stmt", {}, _DriverError("lock timeout", "55P03")))
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == status.HTTP_409_CONFLICT
        detail = excinfo.value.detail
        assert "lock" in detail.lower(), detail
        assert "another change is in progress" in detail, detail
        assert detail != "Database temporarily unavailable."

    async def test_other_sqlstates_keep_the_generic_503(self) -> None:
        """The refinement is state-specific: a real outage still reads as one."""
        endpoint = _endpoint(OperationalError("stmt", {}, _DriverError("query canceled", "57014")))
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert excinfo.value.detail == "Database temporarily unavailable."


class TestInvalidRequestErrorMapping:
    """``InvalidRequestError`` -> 500, never 503 (FAR-1408).

    ``InvalidRequestError`` subclasses ``SQLAlchemyError``, so before this arm
    existed a session-contract violation (a query issued on an
    ``autobegin=False`` session outside ``session.begin()``) fell through to
    the generic 503 backstop: the client was told "Database temporarily
    unavailable." AND the structured ``db_transient`` service-unavailability
    record was written — a LOCAL programming error filed as a database outage.
    That misclassification is what made FAR-1408 look like an infra incident
    for five days.
    """

    async def test_invalid_request_error_maps_to_500_not_503(self) -> None:
        endpoint = _endpoint(InvalidRequestError("Autobegin is disabled on this Session"))
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = excinfo.value.detail
        assert detail != "Database temporarily unavailable.", detail
        assert "outside an active transaction" in detail, detail

    async def test_invalid_request_error_does_not_emit_the_structured_503_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ``db_transient`` reason trail must stay clean of programming errors."""
        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(HTTPException) as excinfo,
        ):
            await _endpoint(InvalidRequestError("Autobegin is disabled on this Session"))()

        assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        records = [r for r in caplog.records if r.name == "modulo.api.db_error_reporting"]
        assert not records, f"InvalidRequestError must not write a service_unavailable record: {records}"

    async def test_invalid_request_error_logs_as_a_session_contract_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """It IS still logged (loudly) — just under the programming-error key."""
        with (
            caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"),
            pytest.raises(HTTPException),
        ):
            await _endpoint(InvalidRequestError("Autobegin is disabled on this Session"))()

        messages = [r.getMessage() for r in caplog.records]
        assert "test.endpoint.session_contract_error" in messages

    async def test_other_sqlalchemy_errors_still_map_to_503(self) -> None:
        """The new arm is a refinement of the base, not a replacement for it."""
        endpoint = _endpoint(SQLAlchemyError("connection lost"))
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert excinfo.value.detail == "Database temporarily unavailable."


class TestPendingRollbackErrorMapping:
    """``PendingRollbackError`` -> 503 with the transient record (FAR-1408 follow-up).

    ``PendingRollbackError`` subclasses ``InvalidRequestError``, but unlike a
    plain session-contract violation it signals a TRANSIENT fault: an earlier
    statement failed and the session was never rolled back, so the next
    statement refuses to run. During a genuine outage that earlier fault
    (server disconnect, serialization failure, pool timeout) is usually the
    real cause, so this must keep the 503 + structured ``db_transient``
    treatment rather than the 500 programming-error arm.
    """

    _MSG = "This Session's transaction has been rolled back due to a previous exception"

    async def test_pending_rollback_maps_to_503_not_500(self) -> None:
        endpoint = _endpoint(PendingRollbackError(self._MSG))
        with pytest.raises(HTTPException) as excinfo:
            await endpoint()
        assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert excinfo.value.detail == "Database temporarily unavailable."
        assert (
            excinfo.value.detail != "Internal server error: a database session was used outside an active transaction."
        )

    async def test_pending_rollback_emits_the_structured_503_record(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(HTTPException) as excinfo,
        ):
            await _endpoint(PendingRollbackError(self._MSG))()

        assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        records = [r for r in caplog.records if r.name == "modulo.api.db_error_reporting"]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.ERROR
        payload = record.__dict__["service_unavailable"]
        assert payload["reason"] == "db_transient"
        assert payload["route"] == "test.endpoint"
        assert payload["exception_class"] == "PendingRollbackError"
        assert payload["detail"] == "transient database error (PendingRollbackError)"

    async def test_pending_rollback_logs_under_its_own_key(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            caplog.at_level(logging.ERROR, logger="modulo.api.db_error_handling"),
            pytest.raises(HTTPException),
        ):
            await _endpoint(PendingRollbackError(self._MSG))()

        messages = [r.getMessage() for r in caplog.records]
        assert "test.endpoint.pending_rollback_error" in messages


class TestPipelineNotRunnableMapping:
    """``PipelineNotRunnableError`` -> 409, never the generic 500 (FAR-1552).

    The pipeline-state gate (archived / soft-deleted today, Paused when
    FAR-1530 lands) sits in ``create_run``. Routes that translate the refusal
    themselves (runs, triggers, webhooks, slack, variants, feedback) raise
    ``pipeline_not_runnable_http`` from their own ``except`` arm and reach
    this module as an ``HTTPException`` — the passthrough arm below keeps
    them byte-identical. A route WITHOUT such an arm, observed on
    ``variant_batches.re_fire_batch``, fell through every arm to the
    ``Exception->500`` backstop: a client-visible 409 reported as a generic
    500. These tests pin the chain-less mapping and its parity with the
    sibling routes' helper.
    """

    async def test_chain_less_refusal_maps_to_409_not_500(self) -> None:
        pipeline_id = uuid.uuid4()

        @handle_db_errors("test.pipeline_not_runnable")
        async def fail() -> None:
            raise PipelineNotRunnableError(pipeline_id=pipeline_id, state="archived")

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_409_CONFLICT
        assert excinfo.value.detail == f"Cannot create run: pipeline {pipeline_id} is archived"

    async def test_detail_is_byte_identical_to_the_sibling_route_helper(self) -> None:
        """The shared arm must read exactly like ``pipeline_not_runnable_http``."""
        from modulo.api.routes.runs import pipeline_not_runnable_http

        refusal = PipelineNotRunnableError(pipeline_id=uuid.uuid4(), state="deleted")

        @handle_db_errors("test.pipeline_not_runnable.parity")
        async def fail() -> None:
            raise refusal

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        sibling = pipeline_not_runnable_http(refusal)
        assert excinfo.value.status_code == sibling.status_code
        assert excinfo.value.detail == sibling.detail

    async def test_route_that_translates_itself_is_not_double_handled(self) -> None:
        """A route-local ``except`` arm already raises HTTPException; the
        shared arm must leave it untouched (one conversion, never two)."""
        from modulo.api.routes.runs import pipeline_not_runnable_http

        refusal = PipelineNotRunnableError(pipeline_id=uuid.uuid4(), state="archived")

        @handle_db_errors("test.pipeline_not_runnable.chain")
        async def fail() -> None:
            try:
                raise refusal
            except PipelineNotRunnableError as exc:
                raise pipeline_not_runnable_http(exc) from None

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_409_CONFLICT
        assert excinfo.value.detail == pipeline_not_runnable_http(refusal).detail

    async def test_refusal_logs_under_its_own_key(self, caplog: pytest.LogCaptureFixture) -> None:
        pipeline_id = uuid.uuid4()

        @handle_db_errors("prefix.pipeline_not_runnable")
        async def fail() -> None:
            raise PipelineNotRunnableError(pipeline_id=pipeline_id, state="archived")

        with (
            caplog.at_level(logging.WARNING, logger="modulo.api.db_error_handling"),
            pytest.raises(HTTPException),
        ):
            await _run(fail())

        messages = [r.getMessage() for r in caplog.records]
        assert any("prefix.pipeline_not_runnable.pipeline_not_runnable" in m for m in messages)

    async def test_generic_exception_still_maps_to_500(self) -> None:
        """The backstop is unchanged — only the domain refusal is lifted out of it."""

        @handle_db_errors("test.generic_still_500")
        async def fail() -> None:
            raise RuntimeError("boom")

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


class TestTriggersPausedMapping:
    """``TriggersPausedError`` -> 409, never the generic 500 (FAR-1589).

    The org-wide ``triggers_paused`` kill-switch gate lives in
    ``ensure_triggers_resumable``, reached from ``create_run`` and from the
    webhook/slack pre-flights. It is a domain refusal an admin can lift, never
    a server-side bug. The webhook/slack routes swallow it inside their own
    endpoint body and answer a ``{"status": "paused"}`` payload, so they never
    reach the classifier with the type; a route WITHOUT such a chain falls
    through every arm to the ``Exception->500`` backstop. These tests pin the
    chain-less mapping and prove a handler that resolves pause itself is not
    double-handled.
    """

    _ORG = uuid.UUID("00000000-0000-0000-0000-00000000f158")

    async def test_chain_less_refusal_maps_to_409_not_500(self) -> None:
        @handle_db_errors("test.triggers_paused")
        async def fail() -> None:
            raise TriggersPausedError(org_id=self._ORG, trigger_type="webhook")

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_409_CONFLICT
        detail = excinfo.value.detail
        assert detail == f"Cannot create run: triggers are paused for organisation {self._ORG}", detail
        assert detail != "An unexpected error occurred.", detail

    async def test_handler_that_resolves_pause_itself_is_not_double_handled(self) -> None:
        """The webhook/slack shape: catch in the body, answer the own payload.

        The shared arm must never see the exception, so the endpoint's own
        result passes through untouched.
        """

        @handle_db_errors("test.triggers_paused.chain")
        async def endpoint() -> dict[str, str]:
            try:
                raise TriggersPausedError(org_id=self._ORG, trigger_type="webhook")
            except TriggersPausedError:
                return {"status": "paused"}

        assert await _run(endpoint()) == {"status": "paused"}

    async def test_refusal_logs_under_its_own_key(self, caplog: pytest.LogCaptureFixture) -> None:
        @handle_db_errors("prefix.triggers_paused")
        async def fail() -> None:
            raise TriggersPausedError(org_id=self._ORG, trigger_type="cron")

        with (
            caplog.at_level(logging.WARNING, logger="modulo.api.db_error_handling"),
            pytest.raises(HTTPException),
        ):
            await _run(fail())

        messages = [r.getMessage() for r in caplog.records]
        assert any("prefix.triggers_paused.triggers_paused" in m for m in messages)

    async def test_generic_exception_still_maps_to_500(self) -> None:
        """The backstop is unchanged — only the pause refusal is lifted out of it."""

        @handle_db_errors("test.triggers_paused.generic")
        async def fail() -> None:
            raise RuntimeError("boom")

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


class TestOrgDeletedMapping:
    """``OrgDeletedError`` -> 409 (deleted) / 404 (missing), never 500 (FAR-1589).

    ``_ensure_org_not_deleted`` sits in ``create_run`` and fires for EVERY run
    origin — manual included — so a chain-less route such as
    ``variant_batches.re_fire_batch`` answered a generic 500 for a refusal the
    sibling routes (``routes/runs.py`` trigger + rerun, ``routes/triggers.py``
    test trigger) translate to a typed 4xx. The statuses and detail strings
    here are byte-identical to those route-local arms.
    """

    _ORG = uuid.UUID("00000000-0000-0000-0000-00000000f159")

    async def test_deleted_org_chain_less_refusal_maps_to_409_not_500(self) -> None:
        @handle_db_errors("test.org_deleted")
        async def fail() -> None:
            raise OrgDeletedError(org_id=self._ORG, deleted=True)

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_409_CONFLICT
        detail = excinfo.value.detail
        assert detail == f"Cannot create run: organisation {self._ORG} is deleted", detail
        assert detail != "An unexpected error occurred.", detail

    async def test_missing_org_chain_less_refusal_maps_to_404_not_500(self) -> None:
        @handle_db_errors("test.org_missing")
        async def fail() -> None:
            raise OrgDeletedError(org_id=self._ORG, deleted=False)

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
        assert excinfo.value.detail == f"Cannot create run: organisation {self._ORG} not found"

    def test_details_match_the_sibling_route_arms(self) -> None:
        """Byte-parity with the route-local ``except OrgDeletedError`` arms.

        There is no importable helper for this mapping (unlike
        ``pipeline_not_runnable_http``), so parity is asserted against the
        detail templates the sibling routes actually contain: if a route
        rewords its detail, this fails and the shared arm must be updated with
        it — the two must never drift apart.
        """
        import modulo.api.routes.runs as runs_route
        import modulo.api.routes.triggers as triggers_route

        deleted_template = 'detail=f"Cannot create run: organisation {exc.org_id} is deleted"'
        missing_template = 'detail=f"Cannot create run: organisation {exc.org_id} not found"'

        runs_src = Path(runs_route.__file__).read_text(encoding="utf-8")
        triggers_src = Path(triggers_route.__file__).read_text(encoding="utf-8")
        assert deleted_template in runs_src, "routes/runs.py no longer answers this detail — update the shared arm"
        assert missing_template in runs_src, "routes/runs.py no longer answers this detail — update the shared arm"
        assert deleted_template in triggers_src, "routes/triggers.py no longer answers this detail"
        assert missing_template in triggers_src, "routes/triggers.py no longer answers this detail"

    async def test_route_that_translates_itself_is_not_double_handled(self) -> None:
        """A route-local ``except`` arm already raises HTTPException; the
        shared arm must leave it untouched (one conversion, never two)."""
        org_id = self._ORG
        refusal = OrgDeletedError(org_id=org_id, deleted=True)

        @handle_db_errors("test.org_deleted.chain")
        async def fail() -> None:
            try:
                raise refusal
            except OrgDeletedError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot create run: organisation {exc.org_id} is deleted",
                ) from None

        with pytest.raises(HTTPException) as excinfo:
            await _run(fail())
        assert excinfo.value.status_code == status.HTTP_409_CONFLICT
        assert excinfo.value.detail == f"Cannot create run: organisation {org_id} is deleted"

    async def test_refusal_logs_under_its_own_key(self, caplog: pytest.LogCaptureFixture) -> None:
        @handle_db_errors("prefix.org_deleted")
        async def fail() -> None:
            raise OrgDeletedError(org_id=self._ORG, deleted=True)

        with (
            caplog.at_level(logging.WARNING, logger="modulo.api.db_error_handling"),
            pytest.raises(HTTPException),
        ):
            await _run(fail())

        messages = [r.getMessage() for r in caplog.records]
        assert any("prefix.org_deleted.org_deleted" in m for m in messages)
