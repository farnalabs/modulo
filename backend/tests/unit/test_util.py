"""Unit tests for modulo.util leaf helpers.

Locks the behaviour of the dependency-free utilities that the DB, core and API
layers import, with a focus on the S5145 log-injection fix
(``sanitise_log_value``) and the deliberate webhook URL scheme guard
(``is_valid_http_url``).
"""

from __future__ import annotations

import uuid
from functools import partial

import pytest

from modulo.util import callable_label, is_valid_http_url, sanitise_log_value
from modulo.util.uuid import coerce_uuid


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

    def test_bool_returns_none(self) -> None:
        # ``bool`` subclasses ``int``; without the explicit guard ``True``
        # would coerce to ``UUID(int=1)`` and ``False`` to the nil UUID, neither
        # of which is a valid UUID input.
        assert coerce_uuid(True) is None
        assert coerce_uuid(False) is None

    def test_bytes_form_returns_none(self) -> None:
        # Positional ``uuid.UUID`` treats bytes as the ``hex`` arg, so raw bytes
        # are intentionally downgraded to "unset" rather than decoded.
        assert coerce_uuid(b"\x00" * 16) is None

    def test_non_uuid_object_returns_none(self) -> None:
        class Bad:
            pass

        assert coerce_uuid(Bad()) is None


class TestBackwardCompatShim:
    """The deprecated ``modulo.utils`` package must keep re-exporting ``coerce_uuid``."""

    def test_utils_package_re_exports_coerce_uuid(self) -> None:
        from modulo.utils import coerce_uuid as shim_pkg

        assert shim_pkg is coerce_uuid
        assert shim_pkg("12345678-1234-5678-1234-567812345678") == uuid.UUID("12345678-1234-5678-1234-567812345678")

    def test_utils_uuid_module_re_exports_coerce_uuid(self) -> None:
        from modulo.utils.uuid import coerce_uuid as shim_mod

        assert shim_mod is coerce_uuid
        assert shim_mod(None) is None


class TestCallableLabel:
    """``callable_label`` must never raise — it is used ON error paths.

    A ``Callable`` may be a plain function, a callable object, a
    ``functools.partial``, or a class instance. Only plain functions define
    ``__name__``, so reading it directly raises ``AttributeError`` for the rest.
    On an error path that turns the handler itself into the failure.
    """

    def test_plain_function(self) -> None:
        def my_builder() -> None: ...

        assert callable_label(my_builder) == "my_builder"

    def test_class_itself(self) -> None:
        assert callable_label(TestCallableLabel) == "TestCallableLabel"

    def test_callable_object_instance(self) -> None:
        class Builder:
            def __call__(self) -> None: ...

        assert callable_label(Builder()).endswith(".Builder")

    def test_functools_partial(self) -> None:
        def base(a: int, b: int) -> int:
            return a + b

        assert callable_label(partial(base, 1)) == "partial"

    def test_lambda(self) -> None:
        # Lambdas DO define __name__; the value is kept as-is.
        assert callable_label(lambda: None) == "<lambda>"

    def test_bound_method(self) -> None:
        class Thing:
            def method(self) -> None: ...

        assert callable_label(Thing().method) == "method"

    def test_non_callable_object_does_not_raise(self) -> None:
        assert callable_label(object()) == "object"

    def test_empty_dunder_name_falls_back_to_type(self) -> None:
        class Weird:
            __name__ = ""

        assert callable_label(Weird()).endswith(".Weird")

    def test_non_string_dunder_name_falls_back_to_type(self) -> None:
        class Weird:
            __name__ = 42  # type: ignore[assignment]

        assert callable_label(Weird()).endswith(".Weird")
