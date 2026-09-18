"""Executor-level wiring tests for FAR-872 failure attribution.

``failure_context`` has its own unit tests; these assert the executor boundary
actually enriches the failure detail string and exposes the build-SHA /
failing-column log extras, so the enrichment cannot silently regress to a
no-op.
"""

from __future__ import annotations

from modulo.core.pipeline_engine import executor as ex


class TestAttributionWiring:
    def test_attribution_module_available(self) -> None:
        assert ex._ATTRIBUTION_AVAILABLE is True

    def test_enrich_appends_suffix_for_column_error(self) -> None:
        exc = RuntimeError("column organisations.updated_at does not exist")
        detail = ex._enrich_failure_detail("original traceback", exc)
        assert detail.startswith("original traceback")
        assert "[attribution:" in detail
        assert "column=updated_at" in detail
        assert "table=organisations" in detail

    def test_enrich_preserves_detail_when_no_attribution(self) -> None:
        detail = ex._enrich_failure_detail("original", RuntimeError("boom"))
        assert detail == "original"

    def test_enrich_fails_open_when_attribution_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(ex, "_ATTRIBUTION_AVAILABLE", False)
        assert ex._enrich_failure_detail("original", RuntimeError("boom")) == "original"

    def test_log_extras_extract_column_and_build_sha(self) -> None:
        exc = RuntimeError("column organisations.updated_at does not exist")
        assert ex._failure_column(exc) == "updated_at"
        assert isinstance(ex._failure_build_sha(), str)

    def test_log_extras_degrade_for_opaque_error(self) -> None:
        assert ex._failure_column(RuntimeError("boom")) is None
        assert isinstance(ex._failure_build_sha(), str)
