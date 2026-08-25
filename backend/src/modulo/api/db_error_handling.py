import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import pydantic
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.constants import MSG_UNEXPECTED_ERROR
from modulo.db.capacity import StorageExhaustedError

_log = logging.getLogger(__name__)
_P = ParamSpec("_P")
_R = TypeVar("_R")


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
            except SQLAlchemyError:
                _log.exception("%s.db_error", log_prefix)
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

        return wrapper

    return decorator
