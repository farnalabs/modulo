"""White-box unit tests for RateLimitMiddleware internals.

Covers branches of _client_key (scope auth_principal, Authorization header
parsing, IP fallbacks) that are not reachable through a TestClient, the
no-match fallback of _rule_for, and the set_rules classmethod. All _client_key
coverage lives here so the identity-keying contract has one home.
"""

from unittest.mock import MagicMock

import jwt
import pytest
from fastapi import FastAPI
from tests.unit.rate_limiter.helpers import ANONYMOUS_GET_RULES, make_mock_request, make_settings

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
    def test_auth_principal_api_key(self, middleware):
        scope = {"auth_principal": {"type": "api_key", "org_id": "org-1", "prefix": "abcd1234"}}
        request = make_mock_request(scope=scope)
        assert middleware._client_key(request) == "ak:org-1:abcd1234:/api/v1/runs"

    def test_auth_principal_user(self, middleware):
        request = make_mock_request(scope={"auth_principal": {"type": "user", "org_id": "org-1", "user_id": "user-7"}})
        assert middleware._client_key(request) == "user:org-1:user-7:/api/v1/runs"

    def test_auth_principal_takes_precedence_over_authorization_header(self, middleware):
        request = make_mock_request(
            scope={"auth_principal": {"type": "api_key", "org_id": "org-1", "prefix": "abcd1234"}},
            headers={"Authorization": "Bearer mk_abcdefgh_test1234567890123456789012"},
        )
        assert middleware._client_key(request) == "ak:org-1:abcd1234:/api/v1/runs"

    def test_no_client_and_no_xff_falls_back_to_unknown(self, middleware):
        request = make_mock_request(headers={}, client=None)
        assert middleware._client_key(request) == "ip:unknown:/api/v1/runs"

    def test_hitl_path_normalizes_hex_uuid_segments(self, middleware):
        """Variable run/gate UUIDs in HITL paths must be normalised to fixed
        placeholders so pre-bucket rotation by variable segments is prevented
        (FAR-1304). Since FAR-611 the trailing action is normalized away too —
        one aggregate bucket per identity (PRD §7.18)."""
        run_id = "3f2a1b2c-9d4e-4b5c-8a1f-123456789abc"
        review_id = "7cba9876-543f-4edc-8ba1-fedcba987654"
        request = make_mock_request(
            path=f"/api/v1/runs/{run_id}/hitl/{review_id}/claim",
            headers={"X-Forwarded-For": "198.51.100.7"},
        )
        assert middleware._client_key(request) == "ip:198.51.100.7:/api/v1/runs/<run_id>/hitl/<review_id>"

    def test_hitl_path_normalizes_non_hex_review_ids(self, middleware):
        """Real gate ids are node ids like ``hitl_review_<source>_<target>`` —
        the pre-FAR-611 normalizer only stripped hex-UUID segments, so these
        paths kept the raw gate id (and action) in the bucket key and every
        gate got its own 20/min bucket. Regression test for the 2026-09-05
        bulk-approve sweep (FAR-611)."""
        request = make_mock_request(
            path="/api/v1/runs/3f2a1b2c-9d4e-4b5c-8a1f-123456789abc/hitl/hitl_review_fetch-pr-title_verify-branch/approve",
            headers={"X-Forwarded-For": "198.51.100.7"},
        )
        assert middleware._client_key(request) == "ip:198.51.100.7:/api/v1/runs/<run_id>/hitl/<review_id>"

    def test_hitl_client_key_does_not_leak_raw_uuids(self, middleware):
        """Raw run/gate UUIDs must never appear in a rate-limit bucket key."""
        run_id = "3f2a1b2c-9d4e-4b5c-8a1f-123456789abc"
        review_id = "7cba9876-543f-4edc-8ba1-fedcba987654"
        request = make_mock_request(path=f"/api/v1/runs/{run_id}/hitl/{review_id}/reject")
        key = middleware._client_key(request)
        assert run_id not in key
        assert review_id not in key

    def test_unknown_principal_type_falls_back_to_ip(self, middleware):
        """A truthy auth_principal with an unrecognised type must fall through to IP keying."""
        scope = {"auth_principal": {"type": "service", "org_id": "org1"}}
        req = make_mock_request(scope=scope, headers={"X-Forwarded-For": "203.0.113.9"})
        assert middleware._client_key(req) == "ip:203.0.113.9:/api/v1/runs"

    def test_bearer_management_api_key(self, middleware):
        token = "mk_abcdefgh1234567890"
        req = make_mock_request(headers={"Authorization": f"Bearer {token}"})
        assert middleware._client_key(req) == "ak:none:abcdefgh:/api/v1/runs"

    def test_bearer_jwt_with_org_and_user(self, middleware):
        token = jwt.encode({"org_id": "o1", "user_id": "u1"}, "a" * 32, algorithm="HS256")
        req = make_mock_request(headers={"Authorization": f"Bearer {token}"})
        assert middleware._client_key(req) == "user:o1:u1:/api/v1/runs"

    def test_bearer_jwt_falls_back_to_account_id(self, middleware):
        token = jwt.encode({"org_id": "o1", "account_id": "a1"}, "a" * 32, algorithm="HS256")
        req = make_mock_request(headers={"Authorization": f"Bearer {token}"})
        assert middleware._client_key(req) == "user:o1:a1:/api/v1/runs"

    @pytest.mark.parametrize(
        "claims",
        [
            {"scope": "public"},
            {"user_id": "u1"},
            {"org_id": "o1"},
        ],
        ids=["no-identity", "missing-org-id", "missing-user-id"],
    )
    def test_bearer_jwt_missing_identity_falls_back_to_ip(self, middleware, claims):
        """A JWT without both org_id and user_id must not key on a partial identity."""
        token = jwt.encode(claims, "a" * 32, algorithm="HS256")
        req = make_mock_request(headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "203.0.113.9"})
        assert middleware._client_key(req) == "ip:203.0.113.9:/api/v1/runs"

    def test_invalid_bearer_token_falls_back_to_ip(self, middleware):
        headers = {"Authorization": "Bearer garbage.not.a.jwt.x", "X-Forwarded-For": "198.51.100.7"}
        req = make_mock_request(headers=headers)
        assert middleware._client_key(req) == "ip:198.51.100.7:/api/v1/runs"

    @pytest.mark.parametrize(
        "authorization",
        ["Basic dXNlcjpwYXNz", "Bearer ", "bearer mk_abcdefgh1234567890"],
        ids=["non-bearer-scheme", "empty-bearer-token", "lowercase-scheme"],
    )
    def test_non_bearer_authorization_falls_back_to_ip(self, middleware, authorization):
        """Only an exact ``Bearer `` prefix is parsed; any other value keys by IP."""
        req = make_mock_request(headers={"Authorization": authorization, "X-Forwarded-For": "198.51.100.7"})
        assert middleware._client_key(req) == "ip:198.51.100.7:/api/v1/runs"

    def test_x_forwarded_for_first_hop(self, middleware):
        req = make_mock_request(headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"})
        assert middleware._client_key(req) == "ip:198.51.100.7:/api/v1/runs"

    def test_client_host_fallback(self, middleware):
        req = make_mock_request(client=MagicMock(host="203.0.113.42"))
        assert middleware._client_key(req) == "ip:203.0.113.42:/api/v1/runs"

    def test_client_host_none_falls_back_to_unknown(self, middleware):
        client = MagicMock()
        client.host = None
        req = make_mock_request(client=client)
        assert middleware._client_key(req) == "ip:unknown:/api/v1/runs"


class TestRuleFor:
    def test_matching_rule(self, middleware):
        request = make_mock_request(path="/api/v1/runs/123")
        assert middleware._rule_for(request) == RateLimitRule(path_prefix="/api/v1/runs", max_requests=60, window_s=60)

    def test_no_matching_rule_returns_empty_rule(self, middleware):
        request = make_mock_request(path="/api/v1/other")
        assert middleware._rule_for(request) == RateLimitRule(path_prefix="", max_requests=0, window_s=0)

    @pytest.mark.parametrize(
        ("path", "prefix"),
        ANONYMOUS_GET_RULES.values(),
        ids=list(ANONYMOUS_GET_RULES),
    )
    def test_anonymous_get_paths_resolve_to_get_rules(self, middleware, path, prefix):
        """FAR-856: _rule_for must resolve pre-auth anonymous GET endpoints to
        their GET_RULES enumeration-defence rule — _should_rate_limit gating
        alone does not pin which budget applies."""
        request = make_mock_request(method="GET", path=path)
        assert middleware._rule_for(request) == RateLimitRule(path_prefix=prefix, max_requests=60, window_s=60)

    def test_hitl_rule_takes_precedence_over_prefix_match(self, middleware):
        """HITL paths must resolve to the dedicated 20/min rule, not the
        /api/v1/runs 60/min rule that also prefix-matches them."""
        request = make_mock_request(path="/api/v1/runs/some-run/hitl/some-gate/approve")
        rule = middleware._rule_for(request)
        assert rule.path_prefix == "/hitl/"
        assert rule.max_requests == 20


class TestManualSubmitSurface:
    """The /manual/{review_id}/submit route is an approve-capability surface
    budgeted by the SAME aggregate 20/min HITL rule as the /hitl/ review
    actions (FAR-611 review fix) — a sweep alternating the two surfaces must
    exhaust ONE bucket."""

    _RUN_ID = "3f2a1b2c-9d4e-4b5c-8a1f-123456789abc"
    _GATE_ID = "hitl_review_fetch-pr-title_verify-branch"

    def test_manual_submit_path_is_rate_limited(self, middleware):
        request = make_mock_request(path=f"/api/v1/runs/{self._RUN_ID}/manual/{self._GATE_ID}/submit")
        assert middleware._should_rate_limit(request) is True

    def test_manual_submit_resolves_to_hitl_rule(self, middleware):
        request = make_mock_request(path=f"/api/v1/runs/{self._RUN_ID}/manual/{self._GATE_ID}/submit")
        rule = middleware._rule_for(request)
        assert rule.path_prefix == "/hitl/"
        assert rule.max_requests == 20

    def test_manual_submit_shares_hitl_bucket(self, middleware):
        """Both surfaces must normalize to the SAME placeholder tail."""
        headers = {"X-Forwarded-For": "198.51.100.7"}
        hitl = make_mock_request(
            path=f"/api/v1/runs/{self._RUN_ID}/hitl/{self._GATE_ID}/approve",
            headers=headers,
        )
        manual = make_mock_request(
            path=f"/api/v1/runs/{self._RUN_ID}/manual/{self._GATE_ID}/submit",
            headers=headers,
        )
        assert middleware._client_key(manual) == middleware._client_key(hitl)
        assert middleware._client_key(manual) == "ip:198.51.100.7:/api/v1/runs/<run_id>/hitl/<review_id>"


class TestSetRules:
    """The autouse ``_isolate_module_state`` fixture restores ``RULES`` after
    every test, so these tests mutate the class attribute directly instead of
    hand-rolling a try/finally restore (which silently duplicated the fixture)."""

    def test_set_rules_overrides_class_defaults(self):
        RateLimitMiddleware.set_rules([RateLimitRule(path_prefix="/api/v1/custom", max_requests=5, window_s=10)])
        assert [RateLimitRule(path_prefix="/api/v1/custom", max_requests=5, window_s=10)] == RateLimitMiddleware.RULES

    def test_rule_for_uses_updated_rules(self, middleware):
        RateLimitMiddleware.set_rules([RateLimitRule(path_prefix="/api/v1/custom", max_requests=5, window_s=10)])
        request = make_mock_request(path="/api/v1/custom/42")
        assert middleware._rule_for(request) == RateLimitRule(path_prefix="/api/v1/custom", max_requests=5, window_s=10)
