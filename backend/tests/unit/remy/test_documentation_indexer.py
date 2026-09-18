"""Unit tests for DocumentationIndex — manifest build, keyword search, and result formatting."""

from pathlib import Path

import pytest

from modulo.core.documentation_indexer import DocEntry, DocumentationIndex


class TestDocumentationIndexBuild:
    """Tests for DocumentationIndex.build from a YAML product manifest."""

    def test_build_creates_entries_from_routes(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(
            "routes:\n"
            "    /pipelines:\n"
            "        name: pipeline-list\n"
            "        breadcrumb: Pipelines\n"
            "        type: list_page\n"
            "        sidebar_group: build\n"
            "        deprecated: false\n",
            encoding="utf-8",
        )
        index = DocumentationIndex.build(manifest)
        assert len(index.entries) == 1
        assert index.entries[0].heading_path == "/pipelines"
        assert index.entries[0].heading == "Pipelines"
        assert "pipeline-list" in index.entries[0].first_paragraph
        assert "type=list_page" in index.entries[0].first_paragraph

    def test_build_heading_falls_back_to_name(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text("routes:\n    /settings/license:\n        name: settings-license\n", encoding="utf-8")
        index = DocumentationIndex.build(manifest)
        assert index.entries[0].heading == "settings-license"

    def test_build_heading_falls_back_to_path(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text("routes:\n    /orphan:\n        name:\n        breadcrumb:\n", encoding="utf-8")
        index = DocumentationIndex.build(manifest)
        assert index.entries[0].heading == "/orphan"

    def test_build_empty_text_returns_empty_index(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text("", encoding="utf-8")
        index = DocumentationIndex.build(manifest)
        assert not index.entries

    def test_build_manifest_with_no_routes(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text("schema_version: 1\n", encoding="utf-8")
        index = DocumentationIndex.build(manifest)
        assert not index.entries

    def test_build_returns_empty_index_when_file_not_found(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.yaml"
        index = DocumentationIndex.build(missing)
        assert isinstance(index, DocumentationIndex)
        assert not index.entries

    def test_build_from_existing_file(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(
            "routes:\n    /features:\n        name: features\n        breadcrumb: Features\n        type: page\n",
            encoding="utf-8",
        )
        index = DocumentationIndex.build(manifest)
        assert len(index.entries) == 1
        assert index.entries[0].heading == "Features"


class TestDocumentationIndexSearch:
    """Tests for DocumentationIndex.search."""

    @pytest.fixture
    def index(self) -> DocumentationIndex:
        entries = [
            DocEntry(
                heading_path="Pipelines > Overview",
                heading="Pipeline Overview",
                first_paragraph="Pipelines are the core execution unit.",
            ),
            DocEntry(
                heading_path="Pipelines > Config",
                heading="Pipeline Config",
                first_paragraph="Configure pipeline nodes and edges.",
            ),
            DocEntry(
                heading_path="Schemas > Types",
                heading="Schema Types",
                first_paragraph="Define schemas with types and validation rules.",
            ),
        ]
        return DocumentationIndex(entries=entries)

    def test_search_by_keyword(self, index: DocumentationIndex) -> None:
        results = index.search("pipeline")
        assert len(results) == 2

    def test_search_case_insensitive(self, index: DocumentationIndex) -> None:
        results = index.search("PIPELINE")
        assert len(results) == 2

    def test_search_multiple_words(self, index: DocumentationIndex) -> None:
        results = index.search("pipeline core")
        assert len(results) == 1

    def test_search_no_match(self, index: DocumentationIndex) -> None:
        results = index.search("nonexistent")
        assert not results

    def test_search_empty_query(self, index: DocumentationIndex) -> None:
        results = index.search("")
        assert not results

    def test_search_empty_index(self) -> None:
        index = DocumentationIndex()
        results = index.search("pipeline")
        assert not results

    def test_search_with_section_filter(self, index: DocumentationIndex) -> None:
        results = index.search("pipeline", section="Pipelines")
        assert len(results) == 2

    def test_search_with_section_filter_excludes_other(self, index: DocumentationIndex) -> None:
        results = index.search("schema", section="Pipelines")
        assert not results


class TestDocumentationIndexFormatResults:
    """Tests for DocumentationIndex.format_results."""

    @pytest.fixture
    def index(self) -> DocumentationIndex:
        entries = [
            DocEntry(
                heading_path="Pipelines > Overview", heading="Pipeline Overview", first_paragraph="Core execution unit."
            ),
            DocEntry(
                heading_path="Schemas > Types",
                heading="Schema Types",
                first_paragraph="Define schemas with validation.",
            ),
        ]
        return DocumentationIndex(entries=entries)

    def test_format_basic(self, index: DocumentationIndex) -> None:
        formatted = index.format_results(index.entries)
        assert "Pipeline Overview" in formatted
        assert "Schema Types" in formatted
        assert "---" in formatted

    def test_format_truncates_token_budget(self) -> None:
        long_para = "A" * 20_000
        entries = [
            DocEntry(heading_path="Section > Long", heading="Long Entry", first_paragraph=long_para),
            DocEntry(heading_path="Section > Short", heading="Short Entry", first_paragraph="Short."),
        ]
        index = DocumentationIndex(entries=entries)
        formatted = index.format_results(entries)
        assert "*(truncated" in formatted
        assert "Short Entry" not in formatted  # truncated before reaching it

    def test_format_empty_results(self, index: DocumentationIndex) -> None:
        formatted = index.format_results([])
        assert formatted == ""
