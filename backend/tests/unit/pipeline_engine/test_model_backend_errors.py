"""FAR-734: unit tests for the model-backend error signature scanner.

Covers every signature pattern (timeout/disabled/connection/rate-limit) plus
the no-match/fail-open path, and the classification wiring through the
error-code registry (map_legacy_code resolves the new codes, no
harness.unknown fallback).
"""

from modulo.core.pipeline_engine.error_codes import (
    ERROR_CODE_REGISTRY,
    class_for,
    expand_code_variants,
    is_retryable,
    known_error_codes,
    map_legacy_code,
)
from modulo.core.pipeline_engine.model_backend_errors import (
    NON_RETRYABLE_PROVIDER_CODES,
    classify_provider_error,
    non_retryable_provider_code,
)

# ---------------------------------------------------------------------------
# Signature scan tests
# ---------------------------------------------------------------------------


class TestClassifyProviderError:
    """classify_provider_error scans retained stdout for terminal error signatures."""

    def test_timeout_unknown_error(self):
        """UnknownError with 'The operation timed out.' → model.provider_timeout."""
        stdout = (
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"thinking..."}]}}\n'
            '{"type":"error","error":{"name":"UnknownError","data":{"message":"The operation timed out."}}}'
        )
        assert classify_provider_error(stdout) == "model.provider_timeout"

    def test_timeout_embedded_in_jsonl(self):
        """Timeout signature embedded in a larger JSONL stream."""
        stdout = (
            '{"type":"message","message":{"role":"assistant","content":[]}}\n'
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"working"}]}}\n'
            '{"type":"error","error":{"name":"UnknownError","data":{"message":"The operation timed out."}}}'
        )
        assert classify_provider_error(stdout) == "model.provider_timeout"

    def test_model_disabled_api_error(self):
        """APIError with Model is disabled + 401 → model_disabled."""
        stdout = '{"type":"error","error":{"name":"APIError","data":{"message":"Model is disabled","statusCode":401}}}'
        assert classify_provider_error(stdout) == "model_disabled"

    def test_connection_error_api_connection(self):
        """APIConnectionError → model.connection."""
        stdout = '{"type":"error","error":{"name":"APIConnectionError","data":{"message":"Connection refused"}}}'
        assert classify_provider_error(stdout) == "model.connection"

    def test_connection_error_network_error(self):
        """NetworkError → model.connection."""
        stdout = '{"type":"error","error":{"name":"NetworkError","data":{"message":"DNS resolution failed"}}}'
        assert classify_provider_error(stdout) == "model.connection"

    def test_connection_error_etimedout(self):
        """ETIMEDOUT → model.connection."""
        stdout = '{"type":"error","error":{"name":"ETIMEDOUT","data":{"message":"Connection timed out"}}}'
        assert classify_provider_error(stdout) == "model.connection"

    def test_rate_limit_error(self):
        """RateLimitError → model.rate_limited."""
        stdout = '{"type":"error","error":{"name":"RateLimitError","data":{"message":"Rate limit exceeded"}}}'
        assert classify_provider_error(stdout) == "model.rate_limited"

    def test_rate_limit_429(self):
        """429 status code → model.rate_limited."""
        stdout = '{"type":"error","error":{"name":"HTTPError","data":{"message":"Too many requests","statusCode":429}}}'
        assert classify_provider_error(stdout) == "model.rate_limited"

    def test_expired_api_key_plain_line_is_provider_authentication(self):
        """FAR-2006 measured shape: a plain stderr line, not JSONL."""
        stderr = 'HTTP 401 "This API key has expired"'
        assert classify_provider_error(stderr) == "provider.authentication"

    def test_incorrect_api_key_message_is_provider_authentication(self):
        """The OpenAI-style message spelling also maps to provider.authentication."""
        stdout = '{"type":"error","error":{"name":"APIError","data":{"message":"Incorrect API key","statusCode":401}}}'
        assert classify_provider_error(stdout) == "provider.authentication"

    def test_status_401_error_event_is_provider_authentication(self):
        """A JSONL error event with statusCode 401 is an auth refusal."""
        stdout = '{"type":"error","error":{"name":"APIError","data":{"message":"Unauthorized","statusCode":401}}}'
        assert classify_provider_error(stdout) == "provider.authentication"

    def test_insufficient_account_funds_plain_line_is_provider_billing(self):
        """FAR-2006 measured shape: HTTP 402 'Insufficient account funds'."""
        stderr = 'HTTP 402 "Insufficient account funds"'
        assert classify_provider_error(stderr) == "provider.billing"

    def test_status_402_error_event_is_provider_billing(self):
        stdout = '{"type":"error","error":{"name":"APIError","data":{"message":"Payment required","statusCode":402}}}'
        assert classify_provider_error(stdout) == "provider.billing"

    def test_insufficient_quota_is_provider_billing(self):
        """The provider-neutral quota spelling also maps to provider.billing."""
        stdout = '{"type":"error","error":{"name":"APIError","data":{"message":"insufficient_quota"}}}'
        assert classify_provider_error(stdout) == "provider.billing"

    def test_unrelated_401_is_not_a_provider_refusal(self):
        """A non-provider 401 (e.g. GitHub 'Bad credentials') must not match."""
        assert classify_provider_error("HTTP 401 Bad credentials") is None

    def test_agent_prose_is_not_a_provider_refusal(self):
        """A phrase match needs an HTTP/JSON error context, not bare prose."""
        assert classify_provider_error("My guess is the API key has expired, but the call never ran") is None

    def test_no_match_returns_none(self):
        """Unrecognised error signature → None (fail open)."""
        stdout = '{"type":"error","error":{"name":"SomeOtherError","data":{"message":"oops"}}}'
        assert classify_provider_error(stdout) is None

    def test_empty_string_returns_none(self):
        """Empty input → None."""
        assert classify_provider_error("") is None

    def test_none_returns_none(self):
        """None input → None."""
        assert classify_provider_error(None) is None

    def test_no_error_type_returns_none(self):
        """JSONL without 'type:error' lines → None."""
        stdout = '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}'
        assert classify_provider_error(stdout) is None

    def test_partial_signature_no_match(self):
        """Partial match (incomplete JSON) → None."""
        stdout = '{"type":"error","error":{"name":"UnknownError","data":{"message":"The operation'
        assert classify_provider_error(stdout) is None

    def test_timeout_priority_over_connection(self):
        """First matching pattern wins — timeout before connection."""
        stdout = (
            '{"type":"error","error":{"name":"UnknownError","data":{"message":"The operation timed out."}}}\n'
            '{"type":"error","error":{"name":"APIConnectionError","data":{"message":"Connection refused"}}}'
        )
        assert classify_provider_error(stdout) == "model.provider_timeout"


# ---------------------------------------------------------------------------
# Registry pin tests (FAR-734 checklist)
# ---------------------------------------------------------------------------


class TestModelBackendErrorRegistry:
    """New error codes are properly registered and resolvable."""

    def test_model_provider_timeout_is_known_retryable(self):
        """model.provider_timeout is a known, retryable, model-class code."""
        known = known_error_codes()
        assert "model.provider_timeout" in known
        spec = ERROR_CODE_REGISTRY["model.provider_timeout"]
        assert spec.error_class == "model"
        assert spec.retryable is True
        assert spec.alert_severity == "warning"
        assert is_retryable("model.provider_timeout") is True
        assert map_legacy_code("model.provider_timeout") == "model.provider_timeout"
        assert class_for("model.provider_timeout") == "model"

    def test_model_disabled_is_known_non_retryable(self):
        """model_disabled is a known, non-retryable, critical code."""
        known = known_error_codes()
        assert "model_disabled" in known
        spec = ERROR_CODE_REGISTRY["model_disabled"]
        assert spec.error_class == "model"
        assert spec.retryable is False
        assert spec.alert_severity == "critical"
        assert is_retryable("model_disabled") is False
        assert map_legacy_code("model_disabled") == "model_disabled"
        assert class_for("model_disabled") == "model"

    def test_model_connection_is_known_retryable(self):
        """model.connection is a known, retryable, model-class code."""
        known = known_error_codes()
        assert "model.connection" in known
        spec = ERROR_CODE_REGISTRY["model.connection"]
        assert spec.error_class == "model"
        assert spec.retryable is True
        assert is_retryable("model.connection") is True
        assert map_legacy_code("model.connection") == "model.connection"

    def test_model_rate_limited_is_known_retryable(self):
        """model.rate_limited is a known, retryable, model-class code."""
        known = known_error_codes()
        assert "model.rate_limited" in known
        spec = ERROR_CODE_REGISTRY["model.rate_limited"]
        assert spec.error_class == "model"
        assert spec.retryable is True
        assert is_retryable("model.rate_limited") is True
        assert map_legacy_code("model.rate_limited") == "model.rate_limited"

    def test_provider_authentication_and_billing_are_known_non_retryable(self):
        """FAR-2006: both provider refusals are registered and never retried."""
        known = known_error_codes()
        for code in ("provider.authentication", "provider.billing"):
            assert code in known
            spec = ERROR_CODE_REGISTRY[code]
            assert spec.error_class == "provider"
            assert spec.retryable is False
            assert spec.alert_severity == "critical"
            assert is_retryable(code) is False
            assert map_legacy_code(code) == code
            assert class_for(code) == "provider"
            # The canonical dotted spelling is always among the variants.
            assert code in expand_code_variants(code)
        # The pre-existing auth alias still resolves to the same canonical code.
        assert expand_code_variants("AuthenticationError") == {"provider.authentication", "AuthenticationError"}

    def test_non_retryable_provider_code_helper(self):
        """The shared helper returns only the permanent provider refusals."""
        assert non_retryable_provider_code('HTTP 401 "This API key has expired"') == "provider.authentication"
        assert non_retryable_provider_code('HTTP 402 "Insufficient account funds"') == "provider.billing"
        # Transient provider codes keep their retry behaviour.
        timeout = '{"type":"error","error":{"name":"UnknownError","data":{"message":"The operation timed out."}}}'
        assert non_retryable_provider_code(timeout) is None
        assert non_retryable_provider_code("some unrelated failure") is None
        assert non_retryable_provider_code("") is None
        assert non_retryable_provider_code(None) is None

    def test_non_retryable_provider_codes_constant_matches_registry(self):
        """The shipped constant and the registry cannot drift apart."""
        assert {"provider.authentication", "provider.billing"} == NON_RETRYABLE_PROVIDER_CODES
        for code in NON_RETRYABLE_PROVIDER_CODES:
            assert ERROR_CODE_REGISTRY[code].retryable is False

    def test_legacy_aliases_resolve(self):
        """Legacy snake_case spellings map to the canonical dotted codes."""
        assert map_legacy_code("model_provider_timeout") == "model.provider_timeout"
        assert map_legacy_code("model_connection") == "model.connection"
        assert map_legacy_code("model_rate_limited") == "model.rate_limited"

    def test_expand_code_variants_covers_all_spellings(self):
        """expand_code_variants returns both dotted and legacy spellings."""
        variants = expand_code_variants("model.provider_timeout")
        assert "model.provider_timeout" in variants
        assert "model_provider_timeout" in variants

    def test_classify_then_resolve_roundtrip(self):
        """classify_provider_error output resolves through map_legacy_code."""
        stdout = '{"type":"error","error":{"name":"UnknownError","data":{"message":"The operation timed out."}}}'
        code = classify_provider_error(stdout)
        assert code == "model.provider_timeout"
        assert map_legacy_code(code) == "model.provider_timeout"
        assert class_for(code) == "model"
        assert is_retryable(code) is True
