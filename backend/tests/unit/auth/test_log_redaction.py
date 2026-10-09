"""Unit tests for the log-redaction helpers (#849)."""

from __future__ import annotations

import uuid

from modulo.auth.log_redaction import truncate_token_family


def test_truncate_token_family_keeps_first_eight_chars() -> None:
    family = uuid.uuid4()
    assert truncate_token_family(family) == str(family)[:8]


def test_truncate_token_family_accepts_strings() -> None:
    assert truncate_token_family("abcdef0123456789") == "abcdef01"


def test_truncate_token_family_short_value_unchanged() -> None:
    assert truncate_token_family("abc") == "abc"
