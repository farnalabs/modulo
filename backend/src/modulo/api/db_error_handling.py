import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import wraps
from typing import Any, NoReturn, ParamSpec, TypeVar

import pydantic
from fastapi import HTTPException, status
from sqlalchemy.exc import (
    IntegrityError,
    InvalidRequestError,
    PendingRollbackError,
    ProgrammingError,
    SQLAlchemyError,
)

from modulo.api.constants import MSG_UNEXPECTED_ERROR
from modulo.api.db_error_reporting import log_service_unavailable
from modulo.core.exceptions import OrgDeletedError, PipelineNotRunnableError, TriggersPausedError
from modulo.db.capacity import StorageExhaustedError
from modulo.db.crud.pipeline import ManualNodeOutputSchemaError
from modulo.db.sqlstates import LOCK_NOT_AVAILABLE_SQLSTATE, sqlstate_of

_log = logging.getLogger(__name__)
_P = ParamSpec("_P")
_R = TypeVar("_R")

# A lock timeout is NOT a database outage: the engine is healthy, this request
# simply could not acquire the row lock because another transaction holds it.
# 409 Conflict states both facts - the mutation did NOT apply, and re-issuing
# it later may succeed - where the generic 503 below would read as "retry now"
# and invite a client retry storm against the very lock that is busy.
_MSG_LOCK_TIMEOUT = (
    "Timed out waiting for a lock on this resource; another change is in progress. "
    "Re-issue the request once the other change completes."
)

# ``InvalidRequestError`` is a SQLAlchemy SESSION-CONTRACT violation (e.g. a
# query issued on an ``autobegin=False`` session before ``session.begin()``) —
# a local programming error, never a database outage. The detail must say
# neither "temporarily unavailable" nor invite a retry: the request will fail
# identically until the code is fixed. FAR-1408: this class previously fell
# through to the generic 503 arm below, so a route-level bug was reported as a
# database outage (and logged via ``log_service_unavailable("db_transient")``),
# which cost five days of misdiagnosis.
#
# ONE subclass is exempt from that mapping: ``PendingRollbackError``. It IS an
# ``InvalidRequestError``, but it signals a TRANSIENT fault — the session was
# left in a failed-transaction state by an earlier error (server disconnect,
# serialization failure, pool timeout) and the next statement refuses to run.
# Filing it as a 500 "server-side bug, retrying will not help" would invert the
# FAR-1408 misclassification on a common outage path, so it gets its own 503
# arm ahead of this one.
#
# PUBLIC (not module-private) because the same misclassification exists in the
# route-local ``except SQLAlchemyError`` arms, which never reach
# ``handle_db_errors`` at all — e.g. every ``hitl.py`` route arm imports this
# constant rather than re-typing the literal (FAR-1408).
MSG_SESSION_CONTRACT = (
    "Internal server error: a database session was used outside an active transaction. "
    "This is a server-side bug, not a database outage; retrying will not help."
)


#: Signature of one ``_EXCEPTION_HANDLERS`` arm: it either raises the mapped
#: ``HTTPException`` or re-raises the original exception. Never returns.
_ExcHandler = Callable[[Any, str], NoReturn]


def _reraise(exc: BaseException, _log_prefix: str) -> NoReturn:
    """Propagate a passthrough exception untouched.

    ``CancelledError`` (a ``BaseException``, not an ``Exception``),
    ``StorageExhaustedError`` and ``HTTPException`` are not DB faults - they
    must reach the caller unchanged.
    """
    raise exc


def _handle_integrity_error(_exc: IntegrityError, log_prefix: str) -> NoReturn:
    """Map a constraint violation to 409 Conflict."""
    _log.exception("%s.integrity_error", log_prefix)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Resource conflict. The operation could not be completed.",
    ) from None


def _handle_programming_error(_exc: ProgrammingError, log_prefix: str) -> NoReturn:
    """Map a missing-schema / unsupported-feature error to 501."""
    _log.exception("%s.programming_error", log_prefix)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Feature is not available. Run database migrations to enable it.",
    ) from None


def _handle_pending_rollback(exc: PendingRollbackError, log_prefix: str) -> NoReturn:
    """Map a failed-transaction session state to a TRANSIENT 503.

    Subclass of ``InvalidRequestError``, but a TRANSIENT-fault signal: an
    earlier statement failed and the session was never rolled back, so the
    next statement on it refuses to run. The underlying fault (server
    disconnect, serialization failure, pool timeout) is often transient, so
    answer 503 and emit the same structured ``db_transient`` record as the
    SQLAlchemyError backstop. MUST stay ahead of the InvalidRequestError arm
    (MRO), which would otherwise file this outage as a 500 programming bug -
    the inverse of the FAR-1408 fix.
    """
    _log.exception("%s.pending_rollback_error", log_prefix)
    log_service_unavailable(
        "db_transient",
        exc,
        route=log_prefix,
        detail="transient database error (PendingRollbackError)",
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Database temporarily unavailable.",
    ) from None


def _handle_invalid_request(_exc: InvalidRequestError, log_prefix: str) -> NoReturn:
    """Map a session-contract violation to 500 - a local programming error.

    ``InvalidRequestError`` is a subclass of ``SQLAlchemyError`` and is a
    session-contract violation, NOT a database outage. MUST stay ahead of the
    SQLAlchemyError arm or the 503 backstop would both mislabel it to the
    client AND write a misleading ``db_transient`` service-unavailability
    record (FAR-1408).
    """
    _log.exception("%s.session_contract_error", log_prefix)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=MSG_SESSION_CONTRACT,
    ) from None


def _handle_sqlalchemy_error(exc: SQLAlchemyError, log_prefix: str) -> NoReturn:
    """Map a generic database error to 503, except a bounded lock timeout (409).

    SQLSTATE 55P03 (``lock_not_available`` - a bounded ``lock_timeout``
    expiring) is a busy-row conflict rather than an outage: the engine is
    healthy and re-issuing may succeed, so it answers 409 (see
    ``_MSG_LOCK_TIMEOUT``) instead of the generic 503 the rest of this arm
    returns. Only the status for that one SQLSTATE differs; the class ordering
    above is unchanged.
    """
    if sqlstate_of(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
        _log.warning("%s.lock_timeout", log_prefix)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_LOCK_TIMEOUT,
        ) from None
    _log.exception("%s.db_error", log_prefix)
    log_service_unavailable(
        "db_transient",
        exc,
        route=log_prefix,
        detail="transient database error (handle_db_errors backstop)",
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Database temporarily unavailable.",
    ) from None


def _handle_pydantic_error(_exc: pydantic.ValidationError, log_prefix: str) -> NoReturn:
    """Map a Pydantic validation failure to 422."""
    _log.exception("%s.validation_error", log_prefix)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="Data validation failed.",
    ) from None


def _handle_manual_node_output_schema_error(exc: ManualNodeOutputSchemaError, _log_prefix: str) -> NoReturn:
    """Surface a manual-node output-schema refusal as 422 with its own detail."""
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=str(exc),
    ) from None


def _handle_pipeline_not_runnable(exc: PipelineNotRunnableError, log_prefix: str) -> NoReturn:
    """Map the ``create_run`` pipeline-state gate refusal to 409 (FAR-1528/FAR-1552).

    A domain refusal, NOT a server-side bug: 409 Conflict with the same detail
    shape every sibling route that translates it itself already answers
    (``pipeline_not_runnable_http``), so a chain-less route such as
    ``variant_batches.re_fire_batch`` reads identically instead of falling
    through to the generic 500 backstop.
    """
    _log.warning(
        "%s.pipeline_not_runnable pipeline=%s state=%s",
        log_prefix,
        exc.pipeline_id,
        exc.state,
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Cannot create run: pipeline {exc.pipeline_id} is {exc.state}",
    ) from None


def _handle_triggers_paused(exc: TriggersPausedError, log_prefix: str) -> NoReturn:
    """Map the org-wide ``triggers_paused`` kill-switch refusal to 409 (FAR-1589).

    A domain refusal an admin can lift, NOT a server-side bug: the same status
    family as the sibling ``create_run``-gate refusals. The routes that handle
    pause themselves (webhooks/slack) swallow it into their own
    ``{"status": "paused"}`` payload inside the endpoint body, so they never
    reach this arm with the exception type; a route that converts it to an
    ``HTTPException`` passes through the passthrough arm untouched regardless.
    """
    _log.warning(
        "%s.triggers_paused org=%s trigger=%s",
        log_prefix,
        exc.org_id,
        exc.trigger_id,
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Cannot create run: triggers are paused for organisation {exc.org_id}",
    ) from None


def _handle_org_deleted(exc: OrgDeletedError, log_prefix: str) -> NoReturn:
    """Map the soft-deleted / missing-org guard to 409 / 404 (FAR-1589).

    The guard in ``create_run`` fires for EVERY run origin (manual included -
    it is deliberately not pause-exempt), and ``variant_batches.re_fire_batch``
    carries no route-local chain, so this previously fell through to the
    generic 500 backstop. Statuses and detail strings are byte-identical to the
    sibling ``except OrgDeletedError`` arms in routes/runs.py (trigger + rerun)
    and routes/triggers.py (test trigger).
    """
    _log.warning(
        "%s.org_deleted org=%s deleted=%s",
        log_prefix,
        exc.org_id,
        exc.deleted,
    )
    if exc.deleted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot create run: organisation {exc.org_id} is deleted",
        ) from None
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Cannot create run: organisation {exc.org_id} not found",
    ) from None


def _handle_unexpected_error(_exc: Exception, log_prefix: str) -> NoReturn:
    """Backstop: log the unexpected failure and answer a generic 500."""
    _log.exception("%s.unexpected_error", log_prefix)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=MSG_UNEXPECTED_ERROR,
    ) from None


#: Ordered exception-class -> handler table for :func:`_translate_wrapped_exception`.
#:
#: The order IS the contract (MRO: specific classes before their bases) and is
#: identical to the ``except`` chain it replaced: ``IntegrityError`` -> 409,
#: ``ProgrammingError`` -> 501, ``PendingRollbackError`` -> 503,
#: ``InvalidRequestError`` -> 500, ``SQLAlchemyError`` -> 503,
#: ``pydantic.ValidationError`` -> 422, ``ManualNodeOutputSchemaError`` -> 422,
#: ``PipelineNotRunnableError`` -> 409, ``TriggersPausedError`` -> 409,
#: ``OrgDeletedError`` -> 409/404. Passthrough arms re-raise for
#: ``CancelledError`` / ``StorageExhaustedError`` / ``HTTPException``; anything
#: unmatched falls through to the ``Exception`` -> 500 backstop.
#: The classifier walks this in sequence, so the ``isinstance`` semantics and the
#: arm order are exactly those of the original chain - never reorder it.
_EXCEPTION_HANDLERS: tuple[tuple[type[BaseException], _ExcHandler], ...] = (
    (asyncio.CancelledError, _reraise),
    (IntegrityError, _handle_integrity_error),
    (ProgrammingError, _handle_programming_error),
    (PendingRollbackError, _handle_pending_rollback),
    (InvalidRequestError, _handle_invalid_request),
    (SQLAlchemyError, _handle_sqlalchemy_error),
    (pydantic.ValidationError, _handle_pydantic_error),
    (ManualNodeOutputSchemaError, _handle_manual_node_output_schema_error),
    (StorageExhaustedError, _reraise),
    (HTTPException, _reraise),
    (PipelineNotRunnableError, _handle_pipeline_not_runnable),
    (TriggersPausedError, _handle_triggers_paused),
    (OrgDeletedError, _handle_org_deleted),
)


def _translate_wrapped_exception(exc: Exception, log_prefix: str) -> NoReturn:
    """Map one exception escaped from the endpoint body to its HTTP response.

    The except-class -> status mapping and its order are the contract this
    module exists to enforce - see :data:`_EXCEPTION_HANDLERS`, whose sequence
    reproduces the original ``except`` chain exactly (MRO: specific classes
    before their bases; never reorder it). Each arm's within-class rationale
    lives on its handler; the parity tests in
    ``tests/unit/api/test_db_error_handling.py`` fail if any detail string
    drifts from its sibling route.

    The ``try: raise exc`` wrapper installs *exc* as the thread's currently
    handled exception, so each handler's ``_log.exception`` records the real
    traceback even when this classifier is called from outside an ``except``
    block (several payload surfaces call it that way).
    """
    try:
        raise exc
    except Exception:  # NOSONAR - installs exc so _log.exception keeps the traceback; arm classifies, never swallows
        # ``Exception``, not ``BaseException`` (S5754): every caller of the
        # classifier passes an ``Exception`` instance (all of them catch
        # ``Exception``), and the passthrough arms' action IS to re-raise
        # unchanged - so an exception that is not an ``Exception`` escapes
        # here identically to how the ``_reraise`` arm would return it.
        for exc_type, handler in _EXCEPTION_HANDLERS:
            if isinstance(exc, exc_type):
                handler(exc, log_prefix)
        # No arm matched: the ``Exception`` -> 500 backstop. Every caller passes
        # an ``Exception`` (the type contract above), so this is always reached
        # for an unclassified failure.
        _handle_unexpected_error(exc, log_prefix)


async def prime_stream[T](
    stream: AsyncIterator[T],
    log_prefix: str,
    *,
    map_domain_error: Callable[[Exception], HTTPException | None] | None = None,
) -> T | None:
    """Pull a streaming body's FIRST item out of *stream*, mapping any failure.

    Streaming routes return a ``StreamingResponse`` whose generator is consumed
    AFTER the handler returns, so a route-level ``@handle_db_errors`` decorator
    cannot see an error raised by the generator's first step — it can only
    answer a truncated 200. Awaiting the first item HERE pulls that work forward
    to where the handler can still raise a real HTTP status.

    Every escaped exception is routed through the module's ONE classifier
    (:func:`_translate_wrapped_exception`), so a start-up failure gets exactly
    the same arm it would from a non-streaming endpoint — IntegrityError -> 409,
    ProgrammingError -> 501, SQLSTATE 55P03 lock timeout -> 409,
    pydantic.ValidationError -> 422, PendingRollbackError/InvalidRequestError
    refinements, the domain refusals, ``CancelledError``/``StorageExhaustedError``
    re-raise, ``HTTPException`` passthrough, and the ``Exception`` -> 500
    backstop. The previous hand-written copies (one per streaming route) mapped
    only a subset of these and silently downgraded the rest to 503/500.

    ``log_prefix`` keys the classifier's structured records
    (``<log_prefix>.programming_error`` etc.). ``map_domain_error`` is the seam
    for a route whose module owns a DOMAIN translator ahead of the classifier
    (analytics' typed ``AnalyticsError`` -> 429/422/501/503): it returns the
    route's ``HTTPException`` (raised here, preserving its exact status/detail)
    or ``None`` to fall through to the shared classifier. ``HTTPException`` is
    never handed to it — it always passes through untouched.

    Returns the first item, or ``None`` when *stream* is already empty
    (``StopAsyncIteration``) — re-emit the returned item (when non-``None``)
    ahead of the rest via :func:`reemit_primed_stream` so no record is lost.
    """
    try:
        return await stream.__anext__()
    except StopAsyncIteration:
        return None
    except Exception as exc:  # routed through the module classifier
        if map_domain_error is not None and not isinstance(exc, HTTPException):
            mapped = map_domain_error(exc)
            if mapped is not None:
                raise mapped from None
        _translate_wrapped_exception(exc, log_prefix)


async def reemit_primed_stream[T](
    stream: AsyncIterator[T],
    first: T | None,
    render: Callable[[T], str],
    *,
    header: str | None = None,
    trailer: str | None = None,
) -> AsyncIterator[str]:
    """Re-emit a primed stream as the wire body, preserving order.

    The single implementation of the first-item-then-the-rest chunker the three
    streaming exports (run-retention export, audit scan, analytics scan) used to
    duplicate. ``first`` is the item already pulled by :func:`prime_stream` (to
    surface pre-stream errors); it is emitted BEFORE ``stream`` so no record is
    lost. ``render`` serialises one item to its wire chunk (an NDJSON line, a CSV
    row, ...).

    ``header`` (e.g. the CSV column header) is emitted once, before any item.
    ``trailer`` (e.g. a completeness sentinel) is emitted ONLY after the DB
    iteration runs to exhaustion — an error mid-stream propagates out of the
    ``async for`` and the trailer is never reached, so its absence is the
    truncation signal a compliance consumer checks.
    """
    if header is not None:
        yield header
    if first is not None:
        yield render(first)
    async for item in stream:
        yield render(item)
    if trailer is not None:
        yield trailer


def raise_lock_timeout_conflict_if(exc: SQLAlchemyError, log_key: str) -> None:
    """Re-raise *exc* as an attributable 409 when it is a bounded lock timeout.

    SQLSTATE 55P03 (``lock_not_available`` — the bounded ``lock_timeout`` from
    ``set_mutation_row_lock_timeout`` expiring on a contended row lock) is a
    busy-row CONFLICT, not a database outage: the engine is healthy and
    re-issuing the request once the other change completes may succeed.
    Mapping it to the generic 503 ("Database temporarily unavailable") would
    read as "retry now" and invite a client retry storm against the very lock
    that is busy — see the ``_MSG_LOCK_TIMEOUT`` rationale at the top of this
    module, which is exactly the 409 this helper raises.

    FAR-1832 QA F2: the org-settings patch helper bounds every converted
    writer's row-lock wait, so 55P03 became REACHABLE on the admin /
    admin-orgs / product-analytics routes whose local ``except
    SQLAlchemyError`` / ``except Exception`` arms collapsed it into a generic
    503 or 500. Each of those arms calls this shared classifier FIRST (the
    same pattern ``hitl.py`` ``_raise_lock_timeout_if`` and
    ``product_analytics_identity`` already use).

    FAR-1832 QA F10 converted five more writer boundaries (the two
    feature-flag org-override writers, ``costs.update_cost_controls``, the
    email-settings write arm and the OTel
    ``update_observability_settings``). QA F10's claim that the sweep was then
    COMPLETE was falsified: two writer boundaries still collapsed 55P03 to a
    generic 503. **QA F16** converts them — ``admin_feature_flags``
    ``toggle_feature_flag`` (PUT ``/admin/feature-flags/{flag_name}``, which
    writes through ``_apply_org_flag_override`` → ``_write_org_override``) and
    ``admin_evidence_retention`` ``update_evidence_retention`` (PUT
    ``/admin/evidence-retention``, which writes through
    ``core.evidence_retention.save_policy``; that route carries no
    ``@handle_db_errors`` backstop at all, so its local arm was the only
    translation). With those two converted, every route arm fronting a bounded
    org-settings/OTel JSON-column patch classifies 55P03 here, so the 409
    mapping has ONE source of truth alongside the ``handle_db_errors``
    backstop above; a genuine outage still falls through to that arm's 503.

    This completeness claim is now a TESTED invariant, not prose: the F16 guard
    in ``tests/unit/api/test_far1832_lock_timeout.py`` enumerates every route
    module that reaches a patch helper (directly or transitively) and fails if
    one of them lacks this classifier — because the "sweep is complete"
    assertion has been falsified twice (F10 then F16).

    For every other ``SQLAlchemyError`` this RETURNS (a no-op) so the caller's
    existing 503 / 500 handling continues unchanged. Callers invoke it from an
    ``except SQLAlchemyError`` arm (or an arm that has already narrowed to it).
    ``log_key`` is the route key; the record is ``<log_key>.lock_timeout``.
    """
    if sqlstate_of(exc) != LOCK_NOT_AVAILABLE_SQLSTATE:
        return
    _log.warning("%s.lock_timeout", log_key, exc_info=exc)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=_MSG_LOCK_TIMEOUT,
    ) from None


def raise_session_contract_error(exc: Exception, log_key: str) -> None:
    """Raise HTTP 500 for a client-side session-contract violation
    (``InvalidRequestError`` / ``MissingGreenlet``); RETURN for any other
    exception so the caller's existing SQLAlchemyError handling continues.

    FAR-1464: the misclassification FAR-1408 fixed on ``hitl.py``'s five
    route-local arms existed across the whole route layer — a local
    ``except SQLAlchemyError`` arm never reaches ``handle_db_errors``, so a
    query issued outside the transaction on the ``autobegin=False`` DI session
    (``InvalidRequestError``) was reported as ``503 "Database temporarily
    unavailable."`` with ``reason=db_transient`` — a retry-inviting outage
    reply for a non-retryable local programming bug. Every converted arm calls
    this guard as its FIRST statement:

    .. code-block:: python

        except SQLAlchemyError as exc:
            raise_session_contract_error(exc, "admin.admin_create_team")
            logger.exception("admin_create_team SQLAlchemyError", ...)
            _raise_db_temporarily_unavailable()

    * A session-contract violation is delegated to
      ``_translate_wrapped_exception`` so the 500 status, the
      ``MSG_SESSION_CONTRACT`` detail and the ``<log_key>.session_contract_error``
      record have ONE source of truth (the shared classifier). That path never
      calls ``log_service_unavailable`` — a programming error must not be
      filed as a database outage.
    * ``PendingRollbackError`` (the TRANSIENT subclass of
      ``InvalidRequestError``) RETURNS: it is not a contract violation, so the
      caller's own 503 handling runs exactly as before FAR-1464 (arm-specific
      message, logging, and structured record where one exists). Returning is
      what keeps every converted arm's transient behaviour byte-identical.
    * Anything else (``OperationalError``, ``IntegrityError``, ...) RETURNS so
      the caller's existing SQLAlchemyError handling — 503 message, log key,
      structured records — continues unchanged.

    The helper never calls ``log_service_unavailable("db_transient", ...)``.
    Callers must invoke it from an ``except SQLAlchemyError`` arm (returning
    falls through to that arm's own 503 logic); calling it from a narrower
    ``except InvalidRequestError`` arm would swallow ``PendingRollbackError``.
    """
    if isinstance(exc, PendingRollbackError):
        return
    if isinstance(exc, InvalidRequestError):
        # NoReturn: raises HTTP 500 (MSG_SESSION_CONTRACT) for a session-
        # contract violation. ``MissingGreenlet`` is an ``InvalidRequestError``
        # subclass, so it is covered here too.
        _translate_wrapped_exception(exc, log_key)


def session_contract_error_payload(exc: Exception, log_key: str) -> dict[str, str] | None:
    """Payload rendering of :func:`raise_session_contract_error` (FAR-1482/FAR-1502).

    Code-bearing surfaces that carry no status code — MCP tool results and the
    run WebSocket control frames — express the classifier's verdict as a
    payload rather than a 500 response. This adapter reads that verdict off the
    raise and re-emits the classifier's OWN message as the surface's specific
    ``session_contract_error`` payload (FAR-1502: a branchable code, never the
    generic ``internal_error``)::

        {"error": "session_contract_error", "detail": MSG_SESSION_CONTRACT}

    One classifier, two renderings: REST answers 500, a payload surface answers
    ``session_contract_error`` carrying the same ``MSG_SESSION_CONTRACT`` text,
    so a caller reading either surface gets the same verdict. The detail is
    taken from the exception the classifier raised rather than restated here,
    so the two renderings cannot drift apart. It lives next to the classifier
    (not on any one surface) so MCP and WebSocket share ONE implementation and
    neither has to import the other's module.

    Returns ``None`` for any exception the classifier does not flag, so the
    caller's own database-unavailable handling runs unchanged — a genuine
    transient ``OperationalError`` / ``PendingRollbackError`` still reports
    ``database_unavailable``.
    """
    try:
        raise_session_contract_error(exc, log_key)
    except HTTPException as http_exc:
        return {"error": "session_contract_error", "detail": str(http_exc.detail)}
    return None


def handle_db_errors(
    log_prefix: str = "api",
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Decorator that catches common DB errors and maps them to HTTP exceptions.

    Usage:
        @handle_db_errors("pipelines.list")
        async def my_endpoint(...):
            ...
    """

    def decorator(func: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:

        @wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                _translate_wrapped_exception(exc, log_prefix)

        return wrapper

    return decorator
