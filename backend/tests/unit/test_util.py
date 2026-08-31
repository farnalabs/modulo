"""Unit tests for modulo.util leaf helpers.

Locks the behaviour of the dependency-free utilities that the DB, core and API
layers import, with a focus on the S5145 log-injection fix
(``sanitise_log_value``) and the deliberate webhook URL scheme guard
(``is_valid_http_url``).
"""

from __future__ import annotations

import uuid

import pytest

from modulo.util import is_valid_http_url, sanitise_log_value
from modulo.utils.uuid import coerce_uuid


class TestSanitiseLogValue:
    def test_plain_value_passthrough(self) -> None:
        assert sanitise_log_value("hello") == "hello"

    def test_cr_and_lf_are_escaped(self) -> None:
        assert sanitise_log_value("a\nb\rc") == "a\\nb\\rc"

    def test_non_string_is_coerced(self) -> None:
        assert sanitise_log_value(123) == "123"

    def test_length_is_capped_at_default(self) -> None:
        assert len(sanitise_log_value("x" * 500)) == 200

    def test_custom_limit_is_honoured(self) -> None:
        assert len(sanitise_log_value("y" * 500, limit=10)) == 10

    def test_escape_then_truncate_order(self) -> None:
        value = "a\nb" + "z" * 500
        result = sanitise_log_value(value, limit=5)
        assert result == "a\\nbz"

    def test_injection_payload_cannot_forge_log_lines(self) -> None:
        assert "\n" not in sanitise_log_value("info\nsystem=compromised")
        assert "\r" not in sanitise_log_value("info\r\n[ADMIN]")


class TestIsValidHttpUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://x.com",
            "https://x.com",
            "HTTP://x.com",
            " http://x.com",
            "  https://x.com  ",
        ],
    )
    def test_scheme_with_netloc_accepted(self, url: str) -> None:
        assert is_valid_http_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://x.com",
            "file:///etc/passwd",
            "https:example.com",
            "https://",
            "not-a-url",
            "",
        ],
    )
    def test_bogus_or_schemeless_rejected(self, url: str) -> None:
        assert is_valid_http_url(url) is False


class TestCoerceUuid:
    def test_well_formed_string_passthrough(self) -> None:
        value = "12345678-1234-5678-1234-567812345678"
        assert coerce_uuid(value) == uuid.UUID(value)

    def test_already_uuid_passthrough(self) -> None:
        u = uuid.uuid4()
        assert coerce_uuid(u) is u

    def test_malformed_string_returns_none(self) -> None:
        assert coerce_uuid("node-a") is None
        assert coerce_uuid("not-a-uuid") is None

    def test_none_returns_none(self) -> None:
        assert coerce_uuid(None) is None

    def test_int_form_accepted(self) -> None:
        u = uuid.UUID(int=0)
        assert coerce_uuid(0) == u
