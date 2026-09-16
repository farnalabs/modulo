"""Attribution instrumentation for executor failures (FAR-872).

Builds a structured failure-context dict from an exception, enriching the
error detail with the deployed build SHA, the failing column name (for
``UndefinedColumnError`` / ``ProgrammingError``), and the query's table set.

This module is FAIL-OPEN by construction — every public function catches
and degrades to ``None`` / ``"unknown"`` on any internal error, so a bug
here can never mask or replace the original exception.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Build SHA source
# ---------------------------------------------------------------------------


def _get_build_sha() -> str:
    """Return the deployed build identifier, or ``"unknown"``.

    Reuses the single build-tag source in ``modulo`` (``__build_tag__`` is
    wired through the deploy pipeline's build args; ``get_build_tag()``
    re-derives it from ``GIT_SHA`` when the cached value is still
    ``"build-local-dev"``, e.g. when the env var was set after ``modulo``
    was imported).  No env-parsing logic is duplicated here.
    """
    try:
        from modulo import __build_tag__, get_build_tag

        # __build_tag__ is ``"build-<7chars>"`` or ``"build-local-dev"``.
        if __build_tag__ and __build_tag__ != "build-local-dev":
            return __build_tag__
        tag = get_build_tag()
        if tag and tag != "build-local-dev":
            return tag
    except Exception:  # noqa: S110 — intentional fail-open
        pass

    return "unknown"


# ---------------------------------------------------------------------------
# Column / table extraction from DB-driver errors
# ---------------------------------------------------------------------------

# Pattern for asyncpg's UndefinedColumnError message:
#   "column <table>.<column> does not exist"
_UNDEFINED_COLUMN_RE = re.compile(
    r"column\s+(?P<table>\w+)\.(?P<column>\w+)\s+does\s+not\s+exist",
    re.IGNORECASE,
)

# Broader ProgrammingError pattern.  Both alternatives MUST stay anchored
# to the ``column`` keyword: an unanchored ``<word>.<word>`` alternative
# would match any dotted token in an error string and emit a false
# attribution (e.g. ``"no module named a.b"``).  Supported shapes:
#   'column "<table>.<column>" does not exist'
#   "column <table>.<column> does not exist"
_PROGRAMMING_COLUMN_RE = re.compile(
    r"column\s+(?:(?:\"(?P<table_q>\w+)\.(?P<column_q>\w+)\")|(?:(?P<table>\w+)\.(?P<column>\w+)))",
    re.IGNORECASE,
)


def _extract_column_info(exc: BaseException) -> tuple[str | None, str | None]:
    """Extract (table, column) from a DB-driver ProgrammingError.

    Returns ``(None, None)`` when the error message does not match the
    expected pattern — never raises.
    """
    try:
        text = str(exc) or ""
        # Try the specific UndefinedColumnError pattern first (most precise).
        m = _UNDEFINED_COLUMN_RE.search(text)
        if m:
            return m.group("table"), m.group("column")
        # Broader ProgrammingError column pattern.
        m = _PROGRAMMING_COLUMN_RE.search(text)
        if m:
            table = m.group("table_q") or m.group("table")
            column = m.group("column_q") or m.group("column")
            return table, column
    except Exception:  # noqa: S110 — intentional fail-open
        pass
    return None, None


def _extract_tables_from_query(exc: BaseException) -> list[str] | None:
    """Best-effort extraction of table names referenced in the error context.

    For ``UndefinedColumnError`` the table is already captured by
    ``_extract_column_info``.  This helper scans for additional ``FROM`` /
    ``JOIN`` patterns in the error text and returns deduplicated table names.

    Returns ``None`` when no tables can be extracted (fail-open).
    """
    try:
        text = str(exc) or ""
        # Match ``FROM <table>`` and ``JOIN <table>`` patterns (case-insensitive).
        tables = re.findall(r"(?:FROM|JOIN)\s+(?:(?:\")?(\w+)(?:\")?)", text, re.IGNORECASE)
        if tables:
            seen: set[str] = set()
            unique: list[str] = []
            for t in tables:
                if t not in seen:
                    seen.add(t)
                    unique.append(t)
            return unique
    except Exception:  # noqa: S110 — intentional fail-open
        pass
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_failure_context(exc: BaseException) -> dict[str, Any]:
    """Build an attribution context dict from an executor failure exception.

    Returns a dict with keys:
    - ``build_sha``: deployed build identifier (first 7 chars of GIT_SHA)
    - ``failing_column``: the column that triggered the error, or ``None``
    - ``failing_table``: the table containing that column, or ``None``
    - ``query_tables``: list of tables referenced in the error, or ``None``

    This function is FAIL-OPEN: any internal error is silently swallowed and
    the corresponding field is ``None`` / ``"unknown"``.
    """
    context: dict[str, Any] = {}
    try:
        context["build_sha"] = _get_build_sha()
    except Exception:
        context["build_sha"] = "unknown"

    try:
        table, column = _extract_column_info(exc)
        context["failing_column"] = column
        context["failing_table"] = table
    except Exception:
        context["failing_column"] = None
        context["failing_table"] = None

    try:
        context["query_tables"] = _extract_tables_from_query(exc)
    except Exception:
        context["query_tables"] = None

    return context


def enrich_error_detail(error_detail: str | None, exc: BaseException) -> str:
    """Append attribution fields to the error detail string.

    Produces a human-readable suffix like::

        [attribution: build=build-abc1234 table=organisations column=updated_at]

    The original ``error_detail`` is never modified — a new string is returned.
    When no useful attribution can be extracted, the original string is
    returned unchanged.
    """
    try:
        ctx = build_failure_context(exc)
        parts: list[str] = []
        if ctx.get("build_sha") and ctx["build_sha"] != "unknown":
            parts.append(f"build={ctx['build_sha']}")
        if ctx.get("failing_table") and ctx.get("failing_column"):
            parts.append(f"table={ctx['failing_table']}")
            parts.append(f"column={ctx['failing_column']}")
        if not parts:
            return error_detail or ""
        suffix = " [attribution: " + " ".join(parts) + "]"
        return (error_detail or "") + suffix
    except Exception:
        return error_detail or ""
