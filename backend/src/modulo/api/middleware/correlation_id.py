"""Correlation ID middleware — generates UUID for each request.

Sets the correlation_id on:
- request.state.correlation_id
- request.state.request_id (backward compat)
- logging contextvar for structured log injection
- X-Request-ID response header

Thread-safe via contextvars.
"""

import uuid
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from modulo.core.logging_config import correlation_id_var

REQUEST_ID_HEADER = "X-Request-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        correlation_id = str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        request.state.request_id = correlation_id
        token = correlation_id_var.set(correlation_id)
        try:
            response: Response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = correlation_id
            return response
        finally:
            correlation_id_var.reset(token)
