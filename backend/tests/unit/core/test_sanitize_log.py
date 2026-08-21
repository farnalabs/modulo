"""Unit tests for the shared log sanitiser (S5145 logging-injection defence)."""

from modulo.core.sanitize_log import (
    DEFAULT_LOG_LIMIT,
    sanitise_log_value,
)


def test_sanitise_log_value_escapes_crlf():
    """CR/LF must be escaped so untrusted values cannot forge log lines."""
    assert sanitise_log_value("bad\nauth\rid") == "bad\\nauth\\rid"


def test_sanitise_log_value_caps_length_with_limit():
    """The rendered value is capped at the supplied limit."""
    assert len(sanitise_log_value("x" * 500, limit=200)) == 200


def test_sanitise_log_value_default_limit():
    """Without an explicit limit, the default cap is applied."""
    assert len(sanitise_log_value("x" * 1000)) == DEFAULT_LOG_LIMIT
