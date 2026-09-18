"""Unit tests for modulo.api.middleware.request_id — legacy wrapper.

QA lens pass (maintainability, deps) on the backward-compat alias. The wrapper
exists so pre-existing imports of ``RequestIdMiddleware`` keep working after the
module was folded into the correlation_id middleware. These tests lock the
alias contract: the middleware class and header name must stay importable from
this path, and the alias must remain the same object as the real middleware.
"""


class TestRequestIdAlias:
    def test_header_constant_importable(self) -> None:
        from modulo.api.middleware.request_id import REQUEST_ID_HEADER

        assert REQUEST_ID_HEADER == "X-Request-ID"

    def test_middleware_alias_is_the_real_middleware(self) -> None:
        from modulo.api.middleware.correlation_id import CorrelationIdMiddleware
        from modulo.api.middleware.request_id import RequestIdMiddleware

        assert RequestIdMiddleware is CorrelationIdMiddleware

    def test_middleware_is_importable_from_legacy_path(self) -> None:
        from modulo.api.middleware.request_id import RequestIdMiddleware

        assert callable(RequestIdMiddleware)

    def test_header_matches_correlation_id_contract(self) -> None:
        from modulo.api.middleware.correlation_id import REQUEST_ID_HEADER as CANONICAL_HEADER
        from modulo.api.middleware.request_id import REQUEST_ID_HEADER

        assert REQUEST_ID_HEADER == CANONICAL_HEADER
