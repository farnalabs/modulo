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
from modulo.core.pipeline_engine.model_backend_errors import classify_provider_error

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
