"""Request ID middleware (legacy wrapper).

Delegates to CorrelationIdMiddleware. Kept for backward compat imports.
New code should import CorrelationIdMiddleware from correlation_id directly.
"""

from modulo.api.middleware.correlation_id import REQUEST_ID_HEADER, CorrelationIdMiddleware

RequestIdMiddleware = CorrelationIdMiddleware

__all__ = ["REQUEST_ID_HEADER", "RequestIdMiddleware"]
