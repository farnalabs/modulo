"""White-box unit tests for RateLimitMiddleware internals.

Covers branches of _client_key (scope auth_principal, unknown-IP fallback)
that are not reachable through a TestClient, the no-match fallback of
_rule_for, and the set_rules classmethod.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from tests.unit.rate_limiter.helpers import make_mock_request, make_settings

from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.core.rate_limiter import RateLimiterRegistry, RateLimitRule


@pytest.fixture
def middleware():
    return RateLimitMiddleware(
        app=FastAPI(),
        settings=make_settings(redis_url=""),
        registry=MagicMock(spec=RateLimiterRegistry),
    )


class TestClientKey:
    def test_auth_principal_api_key(self):
        scope = {"auth_principal": {"type": "api_key", "org_id": "org-1", "prefix": "abcd1234"}}
        request = make_mock_request(scope=scope)
        assert RateLimitMiddleware._client_key(request) == "ak:org-1:abcd1234:/api/v1/runs"

    def test_auth_principal_user(self):
        request = make_mock_request(scope={"auth_principal": {"type": "user", "org_id": "org-1", "user_id": "user-7"}})
        assert RateLimitMiddleware._client_key(request) == "user:org-1:user-7:/api/v1/runs"

    def test_auth_principal_takes_precedence_over_authorization_header(self):
        request = make_mock_request(
            scope={"auth_principal": {"type": "api_key", "org_id": "org-1", "prefix": "abcd1234"}},
            headers={"Authorization": "Bearer mk_abcdefgh_test1234567890123456789012"},
        )
        assert RateLimitMiddleware._client_key(request) == "ak:org-1:abcd1234:/api/v1/runs"

    def test_no_client_and_no_xff_falls_back_to_unknown(self):
        request = make_mock_request(headers={}, client=None)
        assert RateLimitMiddleware._client_key(request) == "ip:unknown:/api/v1/runs"


class TestRuleFor:
    def test_matching_rule(self, middleware):
        request = make_mock_request(path="/api/v1/runs/123")
        assert middleware._rule_for(request) == RateLimitRule(path_prefix="/api/v1/runs", max_requests=60, window_s=60)

    def test_no_matching_rule_returns_empty_rule(self, middleware):
        request = make_mock_request(path="/api/v1/other")
        assert middleware._rule_for(request) == RateLimitRule(path_prefix="", max_requests=0, window_s=0)


class TestSetRules:
    def test_set_rules_overrides_class_defaults(self):
        original = list(RateLimitMiddleware.RULES)
        try:
            RateLimitMiddleware.set_rules([RateLimitRule(path_prefix="/api/v1/custom", max_requests=5, window_s=10)])
            assert [
                RateLimitRule(path_prefix="/api/v1/custom", max_requests=5, window_s=10)
            ] == RateLimitMiddleware.RULES
        finally:
            RateLimitMiddleware.set_rules(original)

    def test_rule_for_uses_updated_rules(self, middleware):
        original = list(RateLimitMiddleware.RULES)
        try:
            RateLimitMiddleware.set_rules([RateLimitRule(path_prefix="/api/v1/custom", max_requests=5, window_s=10)])
            request = make_mock_request(path="/api/v1/custom/42")
            assert middleware._rule_for(request) == RateLimitRule(
                path_prefix="/api/v1/custom", max_requests=5, window_s=10
            )
        finally:
            RateLimitMiddleware.set_rules(original)
