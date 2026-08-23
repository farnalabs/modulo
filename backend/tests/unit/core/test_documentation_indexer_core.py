"""Unit tests for the documentation indexer (modulo.core.documentation_indexer).

The module builds a searchable index from the product manifest
``frontend/src/manifest.yaml`` (the structured product manifest of routes/pages
shipped in the build). Existing coverage — the unit tests in
``tests/unit/remy/test_documentation_indexer.py``, the remy MCP context-source
tests in ``tests/unit/remy/test_context_tools.py``, and the BDD steps in
``tests/bdd/steps/test_remy_context_sources.py`` — exercises
``DocumentationIndex.search`` and ``format_results``. This test module adds
focused coverage of the manifest ``build`` and its failure paths, plus the
search/formatting edge cases.
"""

from pathlib import Path

import pytest

from modulo.core.documentation_indexer import DocEntry, DocumentationIndex

_MINIMAL_MANIFEST = """\
schema_version: 1
routes:
    /pipelines:
        name: pipeline-list
        breadcrumb: Pipelines
        sidebar_group: build
        type: list_page
        deprecated: false
    /runs/:id:
        name: run-detail
        breadcrumb: Run Detail
        type: detail_page
        visibility: private_preview
        deprecated: false
    /analytics:
        name: analytics
        breadcrumb: Analytics
        type: page
        required_permissions: [analytics.query]
        deprecated: false
    /oauth/authorize:
        name: oauth-authorize
        breadcrumb:
        type: page
        deprecated: false
"""


class TestDocEntry:
    def test_holds_fields(self) -> None:
        entry = DocEntry(
            heading_path="/pipelines",
            heading="Pipelines",
            first_paragraph="pipeline-list · type=list_page · sidebar_group=build",
        )
        assert entry.heading_path == "/pipelines"
        assert entry.heading == "Pipelines"
        assert entry.first_paragraph == "pipeline-list · type=list_page · sidebar_group=build"


class TestSearch:
    def test_empty_query_returns_empty(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="/pipelines", heading="Pipelines", first_paragraph="Core.")]
        )
        assert not index.search("")
        assert not index.search("   ")

    def test_single_word_matches_heading(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="/pipelines", heading="Pipeline Overview", first_paragraph="")]
        )
        results = index.search("pipeline")
        assert len(results) == 1
        assert results[0].heading == "Pipeline Overview"

    def test_search_is_case_insensitive(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="/schemas", heading="Schema Types", first_paragraph="")]
        )
        results = index.search("sChEmA")
        assert [r.heading for r in results] == ["Schema Types"]

    def test_matches_word_in_first_paragraph(self) -> None:
        index = DocumentationIndex(
            entries=[
                DocEntry(
                    heading_path="/triggers",
                    heading="Trigger Setup",
                    first_paragraph="Set up triggers to fire pipelines automatically.",
                )
            ]
        )
        results = index.search("automatically")
        assert len(results) == 1

    def test_multi_word_requires_all_terms(self) -> None:
        index = DocumentationIndex(
            entries=[
                DocEntry(
                    heading_path="/pipelines",
                    heading="Pipeline Config",
                    first_paragraph="Configure nodes and edges.",
                ),
                DocEntry(
                    heading_path="/schemas",
                    heading="Schema Types",
                    first_paragraph="Configure node types.",
                ),
            ]
        )
        results = index.search("configure nodes")
        assert [r.heading_path for r in results] == ["/pipelines"]

    def test_multi_word_without_all_terms_returns_empty(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="/pipelines", heading="Pipeline Config", first_paragraph="Nodes.")]
        )
        assert not index.search("pipeline secrets")

    def test_no_match_returns_empty(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="/pipelines", heading="Pipelines", first_paragraph="")]
        )
        assert not index.search("nonexistent-topic")

    def test_section_filter_limits_to_matching_paths(self) -> None:
        index = DocumentationIndex(
            entries=[
                DocEntry(heading_path="/pipelines", heading="Pipeline Overview", first_paragraph="Core."),
                DocEntry(heading_path="/pipelines/copy", heading="Pipeline Config", first_paragraph="Nodes."),
                DocEntry(heading_path="/schemas", heading="Schema Types", first_paragraph="Core."),
            ]
        )
        results = index.search("core", section="/pipelines")
        assert [r.heading_path for r in results] == ["/pipelines"]

    def test_section_filter_is_case_insensitive(self) -> None:
        index = DocumentationIndex(
            entries=[
                DocEntry(heading_path="/pipelines", heading="Pipeline Overview", first_paragraph="Core."),
                DocEntry(heading_path="/schemas", heading="Schema Types", first_paragraph="Core."),
            ]
        )
        results = index.search("core", section="/PIPELINES")
        assert [r.heading_path for r in results] == ["/pipelines"]

    def test_section_filter_with_no_matching_paths(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="/pipelines", heading="Pipelines", first_paragraph="Core.")]
        )
        assert not index.search("core", section="/releases")

    def test_blank_entries_do_not_raise(self) -> None:
        index = DocumentationIndex(entries=[])
        assert not index.search("anything")


class TestFormatResults:
    def test_empty_results_returns_empty_string(self) -> None:
        assert not DocumentationIndex.format_results([])

    def test_single_entry_formats_markdown(self) -> None:
        out = DocumentationIndex.format_results(
            [DocEntry(heading_path="/pipelines", heading="Pipelines", first_paragraph="The core unit.")]
        )
        assert out == "### /pipelines\n\nPipelines\n\nThe core unit."

    def test_multiple_entries_joined_with_separator(self) -> None:
        out = DocumentationIndex.format_results(
            [
                DocEntry(heading_path="/a", heading="Alpha", first_paragraph="One."),
                DocEntry(heading_path="/b", heading="Beta", first_paragraph="Two."),
            ]
        )
        assert out == "### /a\n\nAlpha\n\nOne.\n\n---\n\n### /b\n\nBeta\n\nTwo."

    def test_truncates_entry_larger_than_budget(self) -> None:
        big_paragraph = "x" * 20_000
        prefix = "### /big\n\nHuge\n\n"
        out = DocumentationIndex.format_results(
            [DocEntry(heading_path="/big", heading="Huge", first_paragraph=big_paragraph)]
        )
        assert out.startswith(prefix + "x" * (DocumentationIndex._TOKEN_BUDGET_CHARS - len(prefix)))
        assert out.endswith("\n\n*(truncated — results exceed token budget)*")
        assert "---" not in out

    def test_truncates_later_entry_when_budget_exhausted(self) -> None:
        small = DocEntry(heading_path="/a", heading="Alpha", first_paragraph="One.")
        large = DocEntry(heading_path="/b", heading="Beta", first_paragraph="y" * 20_000)
        out = DocumentationIndex.format_results([small, large])
        assert out.startswith("### /a\n\nAlpha\n\nOne.\n\n---\n\n### /b")
        assert out.endswith("\n\n*(truncated — results exceed token budget)*")

    def test_entries_that_fit_are_all_included(self) -> None:
        entries = [
            DocEntry(heading_path=f"/s{i}", heading=f"Section {i}", first_paragraph=f"Para {i}.") for i in range(10)
        ]
        out = DocumentationIndex.format_results(entries)
        assert out.count("---") == len(entries) - 1
        assert "Section 9" in out


class TestBuild:
    def test_build_parses_manifest(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(_MINIMAL_MANIFEST, encoding="utf-8")
        index = DocumentationIndex.build(manifest)
        paths = [e.heading_path for e in index.entries]
        assert paths == ["/analytics", "/oauth/authorize", "/pipelines", "/runs/:id"]

        by_path = {e.heading_path: e for e in index.entries}
        assert by_path["/pipelines"].heading == "Pipelines"
        assert by_path["/pipelines"].first_paragraph == "pipeline-list · type=list_page · sidebar_group=build"
        assert by_path["/runs/:id"].heading == "Run Detail"
        assert (
            by_path["/runs/:id"].first_paragraph
            == "run-detail · type=detail_page · visibility=private_preview (dev-mode preview)"
        )
        assert by_path["/analytics"].heading == "Analytics"
        assert by_path["/analytics"].first_paragraph == "analytics · type=page · required_permissions=analytics.query"
        assert by_path["/oauth/authorize"].heading == "oauth-authorize"
        assert by_path["/oauth/authorize"].first_paragraph == "oauth-authorize · type=page"

    def test_build_accepts_str_path(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(
            "routes:\n    /schemas:\n        name: schemas\n        breadcrumb: Schemas\n",
            encoding="utf-8",
        )
        index = DocumentationIndex.build(str(manifest))
        assert [e.heading for e in index.entries] == ["Schemas"]

    def test_build_skips_non_dict_routes(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(
            "routes:\n"
            "    /pipelines:\n"
            "        name: pipeline-list\n"
            "        breadcrumb: Pipelines\n"
            "    /legacy: just-a-string\n",
            encoding="utf-8",
        )
        index = DocumentationIndex.build(manifest)
        assert [e.heading_path for e in index.entries] == ["/pipelines"]

    def test_build_sorts_routes_lexicographically(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(
            "routes:\n"
            "    /runs:\n"
            "        name: runs-list\n"
            "        breadcrumb: Runs\n"
            "    /admin:\n"
            "        name: admin\n"
            "        breadcrumb: Admin\n",
            encoding="utf-8",
        )
        index = DocumentationIndex.build(manifest)
        assert [e.heading_path for e in index.entries] == ["/admin", "/runs"]

    def test_build_missing_file_returns_empty_index(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        missing = tmp_path / "does-not-exist.yaml"
        with caplog.at_level("WARNING"):
            index = DocumentationIndex.build(missing)
        assert not index.entries
        assert "Manifest not found" in caplog.text

    def test_build_invalid_yaml_returns_empty_index(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        bad = tmp_path / "manifest.yaml"
        bad.write_text("routes:\n    /pipelines: {{", encoding="utf-8")
        with caplog.at_level("ERROR"):
            index = DocumentationIndex.build(bad)
        assert not index.entries
        assert "Failed to load manifest" in caplog.text

    def test_build_undecodable_file_returns_empty_index(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        bad = tmp_path / "manifest.yaml"
        bad.write_bytes(b"\xff\xfe invalid utf8 \x00\x01")
        with caplog.at_level("ERROR"):
            index = DocumentationIndex.build(bad)
        assert not index.entries
        assert "Failed to load manifest" in caplog.text

    def test_build_unreadable_file_returns_empty_index(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unreadable = tmp_path / "manifest.yaml"
        unreadable.write_text("routes: {}", encoding="utf-8")

        original_open = Path.open

        def _raise(*_args: object, **_kwargs: object) -> object:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "open", _raise)
        try:
            with caplog.at_level("ERROR"):
                index = DocumentationIndex.build(unreadable)
            assert not index.entries
            assert "Failed to load manifest" in caplog.text
        finally:
            monkeypatch.setattr(Path, "open", original_open)

    def test_build_missing_routes_key_returns_empty_index(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text("schema_version: 1\n", encoding="utf-8")
        index = DocumentationIndex.build(manifest)
        assert not index.entries

    def test_build_default_path_reads_repo_manifest(self) -> None:
        index = DocumentationIndex.build()
        assert index.entries
        assert any(e.heading_path == "/pipelines" and e.heading == "Pipelines" for e in index.entries)
