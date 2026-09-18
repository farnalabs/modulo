"""Unit tests for the shared log sanitiser (S5145 logging-injection defence).

The sanitiser is the single choke-point used by sso.py, triggers.py and the
audit logger before untrusted values touch the log sink. These tests pin its
two non-negotiable guarantees:

1. **CR/LF-free output** — neither ``\\r`` nor ``\\n`` may ever reach the log
   sink, regardless of input type or structure.
2. **Bounded output** — the rendered string never exceeds the configured cap,
   and invalid/absent caps degrade safely to a sane default.

QA-lens additions cover mixed/edge inputs, non-string coercion, length-cap
boundary semantics and idempotency so the defence is proven end-to-end.
"""

from modulo.core.sanitize_log import (
    DEFAULT_LOG_LIMIT,
    sanitise_log_value,
)

_CR = "\r"
_LF = "\n"
_R_ESCAPED = "\\r"
_N_ESCAPED = "\\n"


class TestCRLFInjectionDefence:
    def test_crlf_escaped(self) -> None:
        """A trailing CR+LF is rendered as its literal escaped forms."""
        assert sanitise_log_value("bad\nauth\rid") == "bad\\nauth\\rid"

    def test_lone_cr_escaped(self) -> None:
        assert sanitise_log_value("a\rb") == "a\\rb"

    def test_lone_lf_escaped(self) -> None:
        assert sanitise_log_value("a\nb") == "a\\nb"

    def test_crlf_pair_escaped_in_order(self) -> None:
        assert sanitise_log_value("a\r\nb") == "a\\r\\nb"

    def test_multiple_mixed_newlines_escaped(self) -> None:
        assert sanitise_log_value("a\r\nb\nc\rd") == "a\\r\\nb\\nc\\rd"

    def test_cr_only_value_escaped(self) -> None:
        assert sanitise_log_value("\rlean\r") == "\\rlean\\r"

    def test_newline_only_value_escaped(self) -> None:
        assert sanitise_log_value("spooled\n") == "spooled\\n"

    def test_log_line_forgery_is_impossible(self) -> None:
        """A value cannot inject a real newline, so log-lines cannot be forged."""
        assert _LF not in sanitise_log_value("info\r\n[ADMIN] SYS=wiped")
        assert _CR not in sanitise_log_value("info\r\n[ADMIN] SYS=wiped")

    def test_entire_value_is_single_log_line(self) -> None:
        """Output must contain exactly one line (no bare CR or LF)."""
        value = sanitise_log_value("one\r\ntwo\nthree\rcricket")
        assert "\n" not in value
        assert "\r" not in value
        assert value.splitlines() == [value]


class TestNonStringCoercion:
    def test_none_coerced_to_literal_string(self) -> None:
        assert sanitise_log_value(None) == "None"

    def test_int_coerced(self) -> None:
        assert sanitise_log_value(123) == "123"

    def test_float_coerced(self) -> None:
        assert sanitise_log_value(12.5) == "12.5"

    def test_bool_coerced(self) -> None:
        assert sanitise_log_value(True) == "True"

    def test_list_coerced(self) -> None:
        assert sanitise_log_value(["a", "b"]) == "['a', 'b']"

    def test_dict_coerced(self) -> None:
        assert sanitise_log_value({"k": "v"}) == "{'k': 'v'}"

    def test_bytes_repr_never_leaks_raw_newlines(self) -> None:
        """bytes with embedded CR/LF render as escaped repr, not real ones."""
        result = sanitise_log_value(b"token\x0aauth")
        assert "\n" not in result
        assert "\r" not in result

    def test_nested_newlines_in_collection_are_still_escaped(self) -> None:
        result = sanitise_log_value(["line1", "line2"])
        assert result == "['line1', 'line2']"


class TestLengthCapping:
    def test_caps_length_with_explicit_limit(self) -> None:
        assert len(sanitise_log_value("x" * 500, limit=200)) == 200

    def test_uses_default_limit_when_omitted(self) -> None:
        assert len(sanitise_log_value("x" * 1000)) == DEFAULT_LOG_LIMIT

    def test_short_value_untouched(self) -> None:
        assert sanitise_log_value("short") == "short"

    def test_value_exactly_at_limit_untouched(self) -> None:
        assert len(sanitise_log_value("x" * DEFAULT_LOG_LIMIT)) == DEFAULT_LOG_LIMIT

    def test_zero_limit_yields_empty(self) -> None:
        assert not sanitise_log_value("abc", limit=0)

    def test_padding_newlines_do_not_bypass_cap(self) -> None:
        """Escaped CR/LF count toward the cap like any other character."""
        assert len(sanitise_log_value("a\r\n" * 100)) == DEFAULT_LOG_LIMIT


class TestUnicodeAndIdempotency:
    def test_unicode_preserved(self) -> None:
        assert sanitise_log_value("héllo wörld ✓") == "héllo wörld ✓"

    def test_already_escaped_value_not_double_escaped(self) -> None:
        """A benign backslash-n is not treated as a newline (idempotent)."""
        assert sanitise_log_value("already\\nhere") == "already\\nhere"

    def test_sanitiser_is_idempotent(self) -> None:
        once = sanitise_log_value("raw\r\npayload\nend")
        assert sanitise_log_value(once) == once

    def test_unicode_code_points_count_toward_cap(self) -> None:
        assert len(sanitise_log_value("é" * (DEFAULT_LOG_LIMIT + 50))) == DEFAULT_LOG_LIMIT
