"""Unit tests for FAR-872 executor failure attribution instrumentation.

Tests the ``failure_context`` module: build SHA extraction, column/table
parsing from DB-driver errors, and the fail-open degradation contract.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

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
            result = _get_build_sha()
        finally:
            modulo.__build_tag__ = original
        assert result == "build-deadbe"

    def test_env_only_build_tag_used_when_module_cache_is_local_dev(self) -> None:
        """When ``__build_tag__`` is the local-dev sentinel but GIT_SHA is set,
        the shared ``modulo.get_build_tag()`` helper derives it from the env."""
        import modulo

        original = modulo.__build_tag__
        try:
            modulo.__build_tag__ = "build-local-dev"
            with patch.dict(os.environ, {"GIT_SHA": "deadbeefcafe"}):
                result = _get_build_sha()
        finally:
            modulo.__build_tag__ = original
        assert result == "build-deadbee"


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

    def test_dotted_token_without_column_keyword_returns_none(self) -> None:
        """A bare ``<word>.<word>`` token must NOT be attributed as a column.

        Regression guard: the broader ProgrammingError pattern used to accept
        an unanchored ``word.word`` alternative, so an error like
        ``"no module named a.b"`` produced a false attribution.
        """
        exc = _make_db_error("ImportError: no module named modulo.db")
        table, column = _extract_column_info(exc)
        assert table is None
        assert column is None

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


class _UnstringableError(Exception):
    """An exception whose ``str()`` raises, exercising the fail-open guards."""

    def __str__(self) -> str:
        raise ValueError("cannot stringify this exception")


def _raise(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("forced failure for fail-open coverage")


class TestFailOpenBranches:
    """FAR-872 fail-open contract: every internal error degrades, never raises."""

    def test_build_tag_preferred_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo

        monkeypatch.setattr(modulo, "__build_tag__", "build-deadbe")
        assert _get_build_sha() == "build-deadbe"

    def test_build_tag_import_failure_is_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "modulo", None)
        assert _get_build_sha() == "unknown"

    def test_column_extraction_never_raises_on_unstringable(self) -> None:
        table, column = _extract_column_info(_UnstringableError())
        assert table is None
        assert column is None

    def test_table_extraction_never_raises_on_unstringable(self) -> None:
        assert _extract_tables_from_query(_UnstringableError()) is None

    def test_build_context_swallows_build_sha_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.pipeline_engine.failure_context as fc

        monkeypatch.setattr(fc, "_get_build_sha", _raise)
        ctx = build_failure_context(RuntimeError("x"))
        assert ctx["build_sha"] == "unknown"

    def test_build_context_swallows_column_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.pipeline_engine.failure_context as fc

        monkeypatch.setattr(fc, "_extract_column_info", _raise)
        ctx = build_failure_context(RuntimeError("x"))
        assert ctx["failing_column"] is None
        assert ctx["failing_table"] is None

    def test_build_context_swallows_table_query_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.pipeline_engine.failure_context as fc

        monkeypatch.setattr(fc, "_extract_tables_from_query", _raise)
        ctx = build_failure_context(RuntimeError("x"))
        assert ctx["query_tables"] is None

    def test_enrich_includes_build_when_only_sha_known(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.pipeline_engine.failure_context as fc

        monkeypatch.setattr(
            fc,
            "build_failure_context",
            lambda _exc: {"build_sha": "build-abc1234", "failing_table": None, "failing_column": None},
        )
        result = enrich_error_detail("original", RuntimeError("x"))
        assert "build=build-abc1234" in result

    def test_enrich_swallows_context_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.pipeline_engine.failure_context as fc

        monkeypatch.setattr(fc, "build_failure_context", _raise)
        result = enrich_error_detail("original", RuntimeError("x"))
        assert result == "original"
