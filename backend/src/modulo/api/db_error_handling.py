import logging

from fastapi import HTTPException, status

_log = logging.getLogger(__name__)


def handle_db_errors(log_prefix: str = "api"):
    """Decorator that catches common DB errors and maps them to HTTP exceptions.

    Usage:
        @handle_db_errors("pipelines.list")
        async def my_endpoint(...):
            ...
    """
    from functools import wraps

    import pydantic
    from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

    def decorator(func):

        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except ProgrammingError:
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail="Feature is not available. Run database migrations to enable it.",
                )
            except SQLAlchemyError:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Database temporarily unavailable.",
                )
            except pydantic.ValidationError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Data validation failed.",
                )
            except HTTPException:
                raise
            except Exception:
                _log.exception("%s.unexpected_error", log_prefix)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="An unexpected error occurred.",
                ) from None

        return wrapper

    return decorator
