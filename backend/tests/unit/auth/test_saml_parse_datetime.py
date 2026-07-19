"""Direct unit tests for _parse_saml_datetime edge cases."""

from datetime import UTC, datetime

import pytest

from modulo.auth.sso import _parse_saml_datetime


class TestParseSamlDatetime:
    @pytest.mark.parametrize(
        ("input_str", "expected"),
        [
            ("2024-01-01T00:00:00Z", datetime(2024, 1, 1, tzinfo=UTC)),
            ("2024-01-01T00:00:00+00:00", datetime(2024, 1, 1, tzinfo=UTC)),
            ("2024-06-15T12:30:00", datetime(2024, 6, 15, 12, 30, tzinfo=UTC)),
            ("2024-01-01T03:00:00+03:00", datetime(2024, 1, 1, 0, 0, tzinfo=UTC)),
            ("2024-01-01T00:00:00.123Z", datetime(2024, 1, 1, 0, 0, 0, 123000, tzinfo=UTC)),
        ],
    )
    def test_valid_formats(self, input_str: str, expected: datetime) -> None:
        dt = _parse_saml_datetime(input_str)
        assert dt == expected

    def test_raises_on_invalid_format(self) -> None:
        with pytest.raises(ValueError):
            _parse_saml_datetime("not-a-date")
