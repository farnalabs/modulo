"""Shared operator-alert context rendering (FAR-1495).

Every operator alert email — the readiness-degradation alert
(``core.health_alerts``) and the worker-liveness watchdog
(``core.watchdog.worker_liveness``) — must identify the deployment
environment and may carry operator-supplied free text (``ALERT_CONTEXT``:
runbook links, escalation notes, ticket pointers). This module is the SINGLE
source of that format so the two alert channels can never drift apart: the
helpers here render the text part, the HTML part, and the shared line list.

Deliberately a leaf: it imports only ``modulo.settings`` (plus stdlib
``html``), so neither import-linter contract is broken by either alert module
depending on it.
"""

from __future__ import annotations

import html
import logging

from modulo.settings import Settings

#: Total lines the context may occupy, the environment line included — a
#: pathological ``ALERT_CONTEXT`` (a pasted log, say) must not bloat every
#: alert email. The environment line always survives the cap.
MAX_CONTEXT_LINES = 20
#: Per-line cap: one absurdly long pasted line is truncated, not dropped.
MAX_CONTEXT_LINE_CHARS = 300


def alert_environment_line(settings: Settings) -> str:
    """The ``Environment: <env>`` line — the ONE definition of its format.

    Used as the first entry of :func:`alert_context_lines` (the email body)
    and by both alert modules' stdout stamps, so the line can never drift
    between the three renderings. Capped at ``MAX_CONTEXT_LINE_CHARS``: a
    pathological ``MODULO_ENV`` cannot bloat an alert or a log line.
    """
    return f"Environment: {settings.environment or 'unknown'}"[:MAX_CONTEXT_LINE_CHARS]


def alert_context_lines(settings: Settings) -> list[str]:
    """The alert context as lines: ``Environment: <env>`` first, then each
    non-blank ``ALERT_CONTEXT`` line.

    Each context line is stripped of surrounding whitespace, truncated to
    ``MAX_CONTEXT_LINE_CHARS``, and the whole list is capped at
    ``MAX_CONTEXT_LINES`` entries (the environment line counts toward the
    cap). With no ``ALERT_CONTEXT`` configured the result is the environment
    line alone.
    """
    lines = [alert_environment_line(settings)]
    if not settings.alert_context:
        return lines
    for raw_line in settings.alert_context.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lines.append(line[:MAX_CONTEXT_LINE_CHARS])
        if len(lines) >= MAX_CONTEXT_LINES:
            break
    return lines


def alert_context_text(settings: Settings) -> str:
    """The context lines joined with newlines — the plain-text email part."""
    return "\n".join(alert_context_lines(settings))


def alert_context_html(settings: Settings) -> str:
    """The context lines as an escaped HTML list — the HTML email part.

    Every line is escaped (the operator controls ``ALERT_CONTEXT``, so it is
    untrusted input from the rendering's point of view).
    """
    items = "".join(f"<li>{html.escape(line)}</li>" for line in alert_context_lines(settings))
    return f"<ul>{items}</ul>"


def stamp_stdout(message: str, *, logger: logging.Logger, log_event: str) -> None:
    """Best-effort stdout stamp for an operator alert event.

    ``fly logs`` renders JSON logs unreliably, so an alert event needs plain
    stdout visibility. This is STRICTLY best-effort: a ``print`` failure
    (``UnicodeEncodeError`` on a non-UTF-8 stdout, ``BrokenPipeError``, ...)
    must never propagate. Callers stamp at a point where a raise would lose
    work — the readiness alert between delivering its email and committing its
    dedup state, the watchdog between claiming/clearing the incident key and
    the email fan-out — so this fails open, logging at ``log_event`` on the
    caller's ``logger``. ``message`` must never carry ``ALERT_CONTEXT`` (it is
    ``repr=False`` specifically to keep the free text out of logs).

    Single-sourced here (rather than copied into each alert module) so the two
    best-effort stamps can never drift apart.
    """
    try:
        print(message, flush=True)  # noqa: T201
    except Exception:
        logger.warning(log_event, exc_info=True)
