"""Unit tests for DocumentationIndex — parsing PRD markdown, keyword search, and result formatting."""

import textwrap
from pathlib import Path

import pytest

from modulo.core.documentation_indexer import DocEntry, DocumentationIndex


class TestDocumentationIndexBuild:
    """Tests for DocumentationIndex.build and _parse."""

    def test_parse_h2_and_h3_headings(self) -> None:
        md = textwrap.dedent("""\
            # Title

            ## Pipelines

            Pipelines are the core execution unit. They chain nodes together.

            ### Pipeline Configuration

            Configure pipeline nodes and edges in the visual editor.

            ## Triggers

            Triggers fire pipelines automatically.

            ### Trigger Types

            There are webhook, schedule, and event triggers.
        """)
        index = DocumentationIndex._parse(md)
        assert len(index.entries) == 4

        assert index.entries[0].heading == "Pipelines"
        assert index.entries[0].heading_path == "Pipelines"
        assert "core execution unit" in index.entries[0].first_paragraph

        assert index.entries[1].heading == "Pipeline Configuration"
        assert index.entries[1].heading_path == "Pipelines > Pipeline Configuration"
        assert "visual editor" in index.entries[1].first_paragraph

        assert index.entries[2].heading == "Triggers"
        assert index.entries[2].heading_path == "Triggers"

        assert index.entries[3].heading == "Trigger Types"
        assert index.entries[3].heading_path == "Triggers > Trigger Types"

    def test_parse_empty_text_returns_empty_index(self) -> None:
        index = DocumentationIndex._parse("")
        assert len(index.entries) == 0

    def test_parse_text_with_no_headings(self) -> None:
        index = DocumentationIndex._parse("Just some plain text without headings.")
        assert len(index.entries) == 0

    def test_parse_first_paragraph_extraction(self) -> None:
        md = textwrap.dedent("""\
            ## Setup

            First, install the package. Then configure your API key.

            This is a second paragraph. It should not appear in first_paragraph.
        """)
        index = DocumentationIndex._parse(md)
        assert len(index.entries) == 1
        assert "First, install the package" in index.entries[0].first_paragraph
        assert "second paragraph" not in index.entries[0].first_paragraph

    def test_parse_bold_text_is_stripped(self) -> None:
        md = textwrap.dedent("""\
            ## Overview

            This is **very important** documentation.
        """)
        index = DocumentationIndex._parse(md)
        assert "very important" in index.entries[0].first_paragraph
        assert "**" not in index.entries[0].first_paragraph

    def test_build_returns_empty_index_when_file_not_found(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.md"
        index = DocumentationIndex.build(missing)
        assert isinstance(index, DocumentationIndex)
        assert len(index.entries) == 0

    def test_build_from_existing_file(self, tmp_path: Path) -> None:
        md_file = tmp_path / "prd.md"
        md_file.write_text("## Features\n\nFeature overview text.")
        index = DocumentationIndex.build(md_file)
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
        assert len(results) == 0

    def test_search_empty_query(self, index: DocumentationIndex) -> None:
        results = index.search("")
        assert len(results) == 0

    def test_search_empty_index(self) -> None:
        index = DocumentationIndex()
        results = index.search("pipeline")
        assert len(results) == 0

    def test_search_with_section_filter(self, index: DocumentationIndex) -> None:
        results = index.search("pipeline", section="Pipelines")
        assert len(results) == 2

    def test_search_with_section_filter_excludes_other(self, index: DocumentationIndex) -> None:
        results = index.search("schema", section="Pipelines")
        assert len(results) == 0


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
