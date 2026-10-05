"""Unit tests for the shared operator-alert context renderer (FAR-1495).

Pure rendering, no SMTP, no network: each test builds a ``Settings`` instance
directly (initialisation kwargs beat any ambient env var, so the assertions
are deterministic) and renders the context with the shared helpers that both
alert channels import.

Note on how the fields are passed: ``Settings`` is a ``case_sensitive=False``
``BaseSettings``, so constructor keys are matched against each field's
ENVIRONMENT ALIAS case-insensitively. ``alert_context`` also works under its
field name (it case-folds onto the ``ALERT_CONTEXT`` alias), but
``environment`` does NOT: the ``MODULO_ENV`` spelling is required, because a
``environment``-named key is silently dropped by ``extra="ignore"``. This
suite therefore passes both as their aliases (``MODULO_ENV`` /
``ALERT_CONTEXT``), exactly as the rest of the suite passes ``ALERT_EMAIL_TO``.

"""

from __future__ import annotations

from typing import Any

from modulo.core.alert_context import (
    MAX_CONTEXT_LINE_CHARS,
    MAX_CONTEXT_LINES,
    alert_context_html,
    alert_context_lines,
    alert_context_text,
    alert_environment_line,
)
from modulo.settings import Settings


def _make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": "a" * 32,
        "fernet_key": "a" * 32,
        "modulo_admin_password": "test",
        "redis_url": "redis://localhost:6379/0",
        # Pin both FAR-1495 fields so no ambient env var can flip an assertion.
        "MODULO_ENV": "staging",
        "ALERT_CONTEXT": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_environment_line_is_always_present_and_first() -> None:
    settings = _make_settings(MODULO_ENV="production")
    assert alert_context_lines(settings) == ["Environment: production"]
    assert alert_context_text(settings) == "Environment: production"


def test_environment_line_capped_at_max_line_chars() -> None:
    """A pathological MODULO_ENV must not bloat an alert body or a stdout
    stamp: the single-sourced environment line is truncated, not dropped."""
    settings = _make_settings(MODULO_ENV="x" * 1000)
    line = alert_environment_line(settings)
    assert len(line) == MAX_CONTEXT_LINE_CHARS
    # The SAME capped line is what the email body renders first.
    assert alert_context_lines(settings)[0] == line


def test_empty_environment_falls_back_to_unknown() -> None:
    settings = _make_settings(MODULO_ENV="")
    assert alert_environment_line(settings) == "Environment: unknown"


def test_multi_line_context_rendered_in_order() -> None:
    settings = _make_settings(
        ALERT_CONTEXT="runbook: https://example.com/runbook\npage the on-call\nescalation: FAR-1495",
    )
    assert alert_context_lines(settings) == [
        "Environment: staging",
        "runbook: https://example.com/runbook",
        "page the on-call",
        "escalation: FAR-1495",
    ]
    assert alert_context_text(settings) == (
        "Environment: staging\nrunbook: https://example.com/runbook\npage the on-call\nescalation: FAR-1495"
    )


def test_blank_lines_dropped_and_lines_stripped() -> None:
    settings = _make_settings(ALERT_CONTEXT="  first line  \n   \n\n\tsecond line\t\n")
    assert alert_context_lines(settings) == ["Environment: staging", "first line", "second line"]


def test_context_capped_at_max_lines_including_environment() -> None:
    """A pasted-log ALERT_CONTEXT must not bloat every alert: the list is
    capped at MAX_CONTEXT_LINES entries (the environment line survives)."""
    settings = _make_settings(ALERT_CONTEXT="\n".join(f"context line {n}" for n in range(1, 41)))
    lines = alert_context_lines(settings)
    assert len(lines) == MAX_CONTEXT_LINES
    assert lines[0] == "Environment: staging"
    assert lines[-1] == "context line 19"
    assert "context line 20" not in lines


def test_long_context_line_truncated_not_dropped() -> None:
    settings = _make_settings(ALERT_CONTEXT="x" * 1000)
    lines = alert_context_lines(settings)
    assert len(lines[1]) == MAX_CONTEXT_LINE_CHARS


def test_html_escapes_angle_brackets_and_ampersand() -> None:
    """ALERT_CONTEXT is operator-supplied free text — it must never be
    interpolated into the HTML part unescaped."""
    settings = _make_settings(ALERT_CONTEXT="a <b> & c")
    assert alert_context_html(settings) == "<ul><li>Environment: staging</li><li>a &lt;b&gt; &amp; c</li></ul>"


def test_unset_context_yields_environment_line_only() -> None:
    settings = _make_settings(ALERT_CONTEXT=None)
    assert alert_context_lines(settings) == ["Environment: staging"]
    assert alert_context_text(settings) == "Environment: staging"
    assert alert_context_html(settings) == "<ul><li>Environment: staging</li></ul>"
