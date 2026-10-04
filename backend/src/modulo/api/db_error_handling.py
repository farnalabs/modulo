import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import NoReturn, ParamSpec, TypeVar

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


def _translate_wrapped_exception(exc: Exception, log_prefix: str) -> NoReturn:
    """Map one exception escaped from the endpoint body to its HTTP response.

    The except-class -> status mapping and its order are the contract this
    module exists to enforce (IntegrityError->409, ProgrammingError->501,
    PendingRollbackError->503, InvalidRequestError->500, SQLAlchemyError->503,
    pydantic.ValidationError->422; passthrough re-raises for CancelledError /
    StorageExhaustedError / HTTPException; Exception->500). The chain below
    preserves the original except order - never reorder it (MRO: specific
    classes before their bases).

    Three within-class refinements sit on the arms below:

    * ``PendingRollbackError`` (a subclass of ``InvalidRequestError``) gets its
      own 503 arm BEFORE the ``InvalidRequestError`` arm: it signals a
      TRANSIENT fault left behind by an earlier failure (the session was never
      rolled back, so the next statement refuses to run), so it maps to the
      same 503 + ``log_service_unavailable("db_transient", ...)`` record as the
      ``SQLAlchemyError`` backstop - NOT the 500 programming-error arm below.
    * ``InvalidRequestError`` (a subclass of ``SQLAlchemyError``) gets its own
      arm BEFORE the base ``SQLAlchemyError`` arm: it is a session-contract
      violation (a programming error), so it maps to 500 with an accurate
      detail and is logged as an unexpected error - it must NOT reach the 503
      backstop's ``log_service_unavailable("db_transient", ...)`` record,
      which would file a local bug as a database outage.
    * SQLSTATE 55P03 (``lock_not_available`` - a bounded ``lock_timeout``
      expiring) on the ``SQLAlchemyError`` arm maps to 409, not 503. It is a
      busy-row conflict rather than a database outage, so the class ordering
      above is unchanged - only the status for that one SQLSTATE differs.
    """
    try:
        raise exc
    except asyncio.CancelledError:
        raise
    except IntegrityError:
        _log.exception("%s.integrity_error", log_prefix)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource conflict. The operation could not be completed.",
        ) from None
    except ProgrammingError:
        _log.exception("%s.programming_error", log_prefix)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except PendingRollbackError as exc:
        # Subclass of InvalidRequestError, but a TRANSIENT-fault signal: an
        # earlier statement failed and the session was never rolled back, so
        # the next statement on it refuses to run. The underlying fault
        # (server disconnect, serialization failure, pool timeout) is often
        # transient, so answer 503 and emit the same structured ``db_transient``
        # record as the SQLAlchemyError backstop. MUST precede the
        # InvalidRequestError arm below (MRO), which would otherwise file this
        # outage as a 500 programming bug - the inverse of the FAR-1408 fix.
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
    except InvalidRequestError:
        # Session-contract violation (subclass of SQLAlchemyError) - a local
        # programming error, NOT a database outage. MUST precede the
        # SQLAlchemyError arm below or the 503 backstop would both mislabel it
        # to the client AND write a misleading ``db_transient`` service-
        # unavailability record (FAR-1408).
        _log.exception("%s.session_contract_error", log_prefix)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_SESSION_CONTRACT,
        ) from None
    except SQLAlchemyError as exc:
        if sqlstate_of(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            # lock_timeout expired on a bounded row lock (55P03): the DB is
            # healthy, this transaction just could not take the lock. Clear,
            # non-generic answer - see _MSG_LOCK_TIMEOUT for why this is 409
            # rather than the generic 503 the rest of this arm returns.
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
    except pydantic.ValidationError:
        _log.exception("%s.validation_error", log_prefix)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Data validation failed.",
        ) from None
    except ManualNodeOutputSchemaError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from None
    except StorageExhaustedError:
        raise
    except HTTPException:
        raise
    except Exception:
        _log.exception("%s.unexpected_error", log_prefix)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


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
