import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from modulo.api.models.problem import (
    ProblemException,
    problem_from_http_exception,
    problem_from_validation_error,
)

_log = logging.getLogger(__name__)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if isinstance(exc, ProblemException):
        problem = exc.problem
        problem.request_id = getattr(request.state, "request_id", None)
        return problem.to_response()
    problem = problem_from_http_exception(request, exc)
    return problem.to_response()


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    problem = problem_from_validation_error(request, exc.errors())
    return problem.to_response()
