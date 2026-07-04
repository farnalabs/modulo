"""Direct unit tests for _parse_saml_datetime edge cases."""

from datetime import UTC, datetime

import pytest

from modulo.auth.sso import _parse_saml_datetime


class TestParseSamlDatetime:
    def test_z_suffixed(self) -> None:
        dt = _parse_saml_datetime("2024-01-01T00:00:00Z")
        assert dt == datetime(2024, 1, 1, tzinfo=UTC)

    def test_positive_offset(self) -> None:
        dt = _parse_saml_datetime("2024-01-01T00:00:00+00:00")
        assert dt == datetime(2024, 1, 1, tzinfo=UTC)

    def test_naive_timestamp(self) -> None:
        dt = _parse_saml_datetime("2024-06-15T12:30:00")
        assert dt == datetime(2024, 6, 15, 12, 30, tzinfo=UTC)
        assert dt.tzinfo == UTC

    def test_non_utc_offset(self) -> None:
        dt = _parse_saml_datetime("2024-01-01T03:00:00+03:00")
        assert dt == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)

    def test_milliseconds(self) -> None:
        dt = _parse_saml_datetime("2024-01-01T00:00:00.123Z")
        assert dt == datetime(2024, 1, 1, 0, 0, 0, 123000, tzinfo=UTC)

    def test_raises_on_invalid_format(self) -> None:
        with pytest.raises(ValueError):
            _parse_saml_datetime("not-a-date")
