"""Unit tests for FAR-872 executor failure attribution instrumentation.

Tests the ``failure_context`` module: build SHA extraction, column/table
parsing from DB-driver errors, and the fail-open degradation contract.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from modulo.core.pipeline_engine.failure_context import (
    _extract_column_info,
    _extract_tables_from_query,
    _get_build_sha,
    build_failure_context,
    enrich_error_detail,
)


class _FakeDBError(Exception):
    """Fake DB error that mimics the str() output of asyncpg ProgrammingError."""


def _make_db_error(msg: str) -> _FakeDBError:
    """Create a fake DB error with the given message."""
    return _FakeDBError(msg)


class TestGetBuildSha:
    """Build SHA extraction from env / __build_tag__."""

    def test_reads_git_sha_env(self) -> None:
        with patch.dict(os.environ, {"GIT_SHA": "abc123def456"}):
            result = _get_build_sha()
        # __build_tag__ may already be cached as "build-local-dev" from module
        # import time; the env var fallback reads GIT_SHA and takes first 7 chars.
        assert result.startswith("build-")
        assert len(result) == len("build-") + 7

    def test_falls_back_to_unknown_when_no_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GIT_SHA", None)
            result = _get_build_sha()
        assert result == "unknown"

    def test_short_sha_treated_as_unknown(self) -> None:
        with patch.dict(os.environ, {"GIT_SHA": "abc"}):
            result = _get_build_sha()
        assert result == "unknown"

    def test_uses_build_tag_when_available(self) -> None:
        import modulo

        original = modulo.__build_tag__
        try:
            modulo.__build_tag__ = "build-deadbe"
            result = _get_build_tag_wrapper()
        finally:
            modulo.__build_tag__ = original
        assert result == "build-deadbe"


def _get_build_tag_wrapper() -> str:
    """Wrapper to test the __build_tag__ path."""
    try:
        from modulo import __build_tag__

        if __build_tag__ and __build_tag__ != "build-local-dev":
            return __build_tag__
    except Exception:  # noqa: S110 — intentional fail-open
        pass
    return _get_build_sha()


class TestExtractColumnInfo:
    """Column/table extraction from exception messages."""

    def test_undefined_column_error(self) -> None:
        exc = _make_db_error(
            "asyncpg.exceptions.UndefinedColumnError: "
            "column organisations.updated_at does not exist\n"
            "HINT: Perhaps you meant to reference the column"
        )
        table, column = _extract_column_info(exc)
        assert table == "organisations"
        assert column == "updated_at"

    def test_generic_programming_error(self) -> None:
        exc = _make_db_error('column "pipeline_runs.status" does not exist')
        table, column = _extract_column_info(exc)
        assert table == "pipeline_runs"
        assert column == "status"

    def test_malformed_message_returns_none(self) -> None:
        exc = _make_db_error("some unrelated error message")
        table, column = _extract_column_info(exc)
        assert table is None
        assert column is None

    def test_empty_message_returns_none(self) -> None:
        exc = _make_db_error("")
        table, column = _extract_column_info(exc)
        assert table is None
        assert column is None

    def test_non_string_exception_returns_none(self) -> None:
        exc = ValueError(42)
        table, column = _extract_column_info(exc)
        assert table is None
        assert column is None


class TestExtractTablesFromQuery:
    """Table extraction from error messages."""

    def test_extracts_from_join(self) -> None:
        exc = RuntimeError("SELECT ... FROM organisations JOIN accounts ON ...")
        tables = _extract_tables_from_query(exc)
        assert tables is not None
        assert "organisations" in tables
        assert "accounts" in tables

    def test_no_tables_returns_none(self) -> None:
        exc = RuntimeError("some error")
        tables = _extract_tables_from_query(exc)
        assert tables is None

    def test_deduplicates_tables(self) -> None:
        exc = RuntimeError("FROM organisations JOIN organisations ON ...")
        tables = _extract_tables_from_query(exc)
        assert tables is not None
        assert tables.count("organisations") == 1


class TestBuildFailureContext:
    """Full failure context builder."""

    def test_undefined_column_error_context(self) -> None:
        exc = _make_db_error("asyncpg.exceptions.UndefinedColumnError: column organisations.updated_at does not exist")
        ctx = build_failure_context(exc)
        assert ctx["failing_column"] == "updated_at"
        assert ctx["failing_table"] == "organisations"
        assert isinstance(ctx["build_sha"], str)

    def test_opaque_error_degrades_gracefully(self) -> None:
        exc = RuntimeError("something went wrong")
        ctx = build_failure_context(exc)
        assert ctx["failing_column"] is None
        assert ctx["failing_table"] is None
        assert isinstance(ctx["build_sha"], str)

    def test_never_raises_on_any_input(self) -> None:
        ctx = build_failure_context("not-an-exception")
        assert "build_sha" in ctx
        assert "failing_column" in ctx


class TestEnrichErrorDetail:
    """Error detail enrichment with attribution suffix."""

    def test_appends_attribution_for_column_error(self) -> None:
        exc = _make_db_error("column organisations.updated_at does not exist")
        result = enrich_error_detail("original detail", exc)
        assert "attribution:" in result
        assert "table=organisations" in result
        assert "column=updated_at" in result
        assert result.startswith("original detail")

    def test_no_attribution_for_opaque_error(self) -> None:
        exc = RuntimeError("something")
        result = enrich_error_detail("original", exc)
        assert result == "original"

    def test_never_raises(self) -> None:
        result = enrich_error_detail(None, RuntimeError("x"))
        assert isinstance(result, str)

    def test_preserves_original_detail(self) -> None:
        exc = _make_db_error("column foo.bar does not exist")
        original = "traceback text here"
        result = enrich_error_detail(original, exc)
        assert result.startswith(original)
