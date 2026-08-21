"""Unit tests for the documentation indexer (modulo.core.documentation_indexer).

The module builds a searchable index from ``docs/prd.md`` headings. Existing
coverage — the unit tests in ``tests/unit/remy/test_documentation_indexer.py``,
the remy MCP context-source tests in ``tests/unit/remy/test_context_tools.py``,
and the BDD steps in ``tests/bdd/steps/test_remy_context_sources.py`` — only
exercises ``DocumentationIndex.search`` and ``format_results``. This test module
adds focused coverage of the parser (``_parse`` / ``_extract_first_paragraph``),
the ``build`` I/O and failure paths, and the search/formatting edge cases.
"""

from pathlib import Path

import pytest

from modulo.core.documentation_indexer import DocEntry, DocumentationIndex, _extract_first_paragraph


class TestDocEntry:
    def test_holds_fields(self) -> None:
        entry = DocEntry(
            heading_path="Pipelines > Overview",
            heading="Pipeline Overview",
            first_paragraph="Pipelines are the core execution unit.",
        )
        assert entry.heading_path == "Pipelines > Overview"
        assert entry.heading == "Pipeline Overview"
        assert entry.first_paragraph == "Pipelines are the core execution unit."


class TestSearch:
    def test_empty_query_returns_empty(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="Pipelines", heading="Pipelines", first_paragraph="Core.")]
        )
        assert not index.search("")
        assert not index.search("   ")

    def test_single_word_matches_heading(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="Pipelines", heading="Pipeline Overview", first_paragraph="")]
        )
        results = index.search("pipeline")
        assert len(results) == 1
        assert results[0].heading == "Pipeline Overview"

    def test_search_is_case_insensitive(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="Schemas", heading="Schema Types", first_paragraph="")]
        )
        results = index.search("sChEmA")
        assert [r.heading for r in results] == ["Schema Types"]

    def test_matches_word_in_first_paragraph(self) -> None:
        index = DocumentationIndex(
            entries=[
                DocEntry(
                    heading_path="Triggers",
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
                    heading_path="Pipelines",
                    heading="Pipeline Config",
                    first_paragraph="Configure nodes and edges.",
                ),
                DocEntry(
                    heading_path="Schemas",
                    heading="Schema Types",
                    first_paragraph="Configure node types.",
                ),
            ]
        )
        results = index.search("configure nodes")
        assert [r.heading_path for r in results] == ["Pipelines"]

    def test_multi_word_without_all_terms_returns_empty(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="Pipelines", heading="Pipeline Config", first_paragraph="Nodes.")]
        )
        assert not index.search("pipeline secrets")

    def test_no_match_returns_empty(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="Pipelines", heading="Pipelines", first_paragraph="")]
        )
        assert not index.search("nonexistent-topic")

    def test_section_filter_limits_to_matching_paths(self) -> None:
        index = DocumentationIndex(
            entries=[
                DocEntry(heading_path="Pipelines > Overview", heading="Pipeline Overview", first_paragraph="Core."),
                DocEntry(heading_path="Pipelines > Config", heading="Pipeline Config", first_paragraph="Nodes."),
                DocEntry(heading_path="Schemas > Types", heading="Schema Types", first_paragraph="Core."),
            ]
        )
        results = index.search("core", section="Pipelines")
        assert [r.heading_path for r in results] == ["Pipelines > Overview"]

    def test_section_filter_is_case_insensitive(self) -> None:
        index = DocumentationIndex(
            entries=[
                DocEntry(heading_path="Pipelines > Overview", heading="Pipeline Overview", first_paragraph="Core."),
                DocEntry(heading_path="Schemas > Types", heading="Schema Types", first_paragraph="Core."),
            ]
        )
        results = index.search("core", section="pipelines")
        assert [r.heading_path for r in results] == ["Pipelines > Overview"]

    def test_section_filter_with_no_matching_paths(self) -> None:
        index = DocumentationIndex(
            entries=[DocEntry(heading_path="Pipelines", heading="Pipelines", first_paragraph="Core.")]
        )
        assert not index.search("core", section="Releases")

    def test_blank_entries_do_not_raise(self) -> None:
        index = DocumentationIndex(entries=[])
        assert not index.search("anything")


class TestFormatResults:
    def test_empty_results_returns_empty_string(self) -> None:
        assert not DocumentationIndex.format_results([])

    def test_single_entry_formats_markdown(self) -> None:
        out = DocumentationIndex.format_results(
            [DocEntry(heading_path="Pipelines", heading="Pipelines", first_paragraph="The core unit.")]
        )
        assert out == "### Pipelines\n\nPipelines\n\nThe core unit."

    def test_multiple_entries_joined_with_separator(self) -> None:
        out = DocumentationIndex.format_results(
            [
                DocEntry(heading_path="A", heading="Alpha", first_paragraph="One."),
                DocEntry(heading_path="B", heading="Beta", first_paragraph="Two."),
            ]
        )
        assert out == "### A\n\nAlpha\n\nOne.\n\n---\n\n### B\n\nBeta\n\nTwo."

    def test_truncates_entry_larger_than_budget(self) -> None:
        big_paragraph = "x" * 20_000
        prefix = "### Big\n\nHuge\n\n"
        out = DocumentationIndex.format_results(
            [DocEntry(heading_path="Big", heading="Huge", first_paragraph=big_paragraph)]
        )
        assert out.startswith(prefix + "x" * (DocumentationIndex._TOKEN_BUDGET_CHARS - len(prefix)))
        assert out.endswith("\n\n*(truncated — results exceed token budget)*")
        assert "---" not in out

    def test_truncates_later_entry_when_budget_exhausted(self) -> None:
        small = DocEntry(heading_path="A", heading="Alpha", first_paragraph="One.")
        large = DocEntry(heading_path="B", heading="Beta", first_paragraph="y" * 20_000)
        out = DocumentationIndex.format_results([small, large])
        assert out.startswith("### A\n\nAlpha\n\nOne.\n\n---\n\n### B")
        assert out.endswith("\n\n*(truncated — results exceed token budget)*")

    def test_entries_that_fit_are_all_included(self) -> None:
        entries = [
            DocEntry(heading_path=f"S{i}", heading=f"Section {i}", first_paragraph=f"Para {i}.") for i in range(10)
        ]
        out = DocumentationIndex.format_results(entries)
        assert out.count("---") == len(entries) - 1
        assert "Section 9" in out


class TestBuild:
    def test_build_parses_file(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.md"
        prd.write_text("## Pipelines\n\nIntro paragraph.\n\n### Config\n\nConfig paragraph.\n", encoding="utf-8")
        index = DocumentationIndex.build(prd)
        assert [e.heading_path for e in index.entries] == ["Pipelines", "Pipelines > Config"]
        assert index.entries[0].first_paragraph == "Intro paragraph."

    def test_build_accepts_str_path(self, tmp_path: Path) -> None:
        prd = tmp_path / "prd.md"
        prd.write_text("## Schemas\n\nSchema intro.\n", encoding="utf-8")
        index = DocumentationIndex.build(str(prd))
        assert [e.heading for e in index.entries] == ["Schemas"]

    def test_build_missing_file_returns_empty_index(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        missing = tmp_path / "does-not-exist.md"
        with caplog.at_level("WARNING"):
            index = DocumentationIndex.build(missing)
        assert not index.entries
        assert "PRD not found" in caplog.text

    def test_build_undecodable_file_returns_empty_index(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        bad = tmp_path / "prd.md"
        bad.write_bytes(b"\xff\xfe invalid utf8 \x00\x01")
        with caplog.at_level("ERROR"):
            index = DocumentationIndex.build(bad)
        assert not index.entries
        assert "Failed to read PRD" in caplog.text

    def test_build_unreadable_file_returns_empty_index(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unreadable = tmp_path / "prd.md"
        unreadable.write_text("## Pipelines\n", encoding="utf-8")

        def _raise(*_args: object, **_kwargs: object) -> str:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", _raise)
        with caplog.at_level("ERROR"):
            index = DocumentationIndex.build(unreadable)
        assert not index.entries
        assert "Failed to read PRD" in caplog.text


class TestParse:
    def test_h2_heading_is_indexed(self) -> None:
        index = DocumentationIndex._parse("## Pipelines\n\nPipelines are core.\n")
        assert len(index.entries) == 1
        assert index.entries[0].heading_path == "Pipelines"
        assert index.entries[0].heading == "Pipelines"
        assert index.entries[0].first_paragraph == "Pipelines are core."

    def test_h3_heading_nested_under_h2(self) -> None:
        index = DocumentationIndex._parse("## Pipelines\n\n### Config\n\nConfig text.\n")
        assert len(index.entries) == 2
        assert index.entries[0].heading_path == "Pipelines"
        assert index.entries[1].heading_path == "Pipelines > Config"
        assert index.entries[1].heading == "Config"
        assert index.entries[1].first_paragraph == "Config text."

    def test_h3_without_parent_h2(self) -> None:
        index = DocumentationIndex._parse("### Orphan\n\nStandalone section.\n")
        assert index.entries[0].heading_path == "Orphan"

    def test_new_h2_resets_h3_context(self) -> None:
        index = DocumentationIndex._parse(
            "## Pipelines\n\n### Config\n\nConfig text.\n\n## Schemas\n\n### Types\n\nType text.\n"
        )
        paths = [e.heading_path for e in index.entries]
        assert paths == ["Pipelines", "Pipelines > Config", "Schemas", "Schemas > Types"]

    def test_h4_heading_is_ignored(self) -> None:
        index = DocumentationIndex._parse("## Pipelines\n\n#### Not indexed\n")
        assert len(index.entries) == 1

    def test_paragraph_stops_at_next_heading(self) -> None:
        index = DocumentationIndex._parse("## Pipelines\n\nIntro.\n\n## Schemas\n\nTypes.\n")
        assert index.entries[0].first_paragraph == "Intro."
        assert index.entries[1].first_paragraph == "Types."

    def test_paragraph_strips_bold_and_italic(self) -> None:
        index = DocumentationIndex._parse("## Pipelines\n\n**Bold text** and _(see above)_ notes.\n")
        para = index.entries[0].first_paragraph
        assert "**" not in para
        assert "(_" not in para
        assert "Bold text" in para
        assert "notes." in para

    def test_heading_without_paragraph_has_empty_first_paragraph(self) -> None:
        index = DocumentationIndex._parse("## Pipelines\n\n## Schemas\n")
        assert not index.entries[0].first_paragraph
        assert not index.entries[1].first_paragraph

    def test_multi_line_paragraph_is_joined(self) -> None:
        index = DocumentationIndex._parse("## Pipelines\n\nFirst line.\nSecond line.\n")
        assert index.entries[0].first_paragraph == "First line. Second line."

    def test_blank_input_produces_no_entries(self) -> None:
        assert not DocumentationIndex._parse("").entries
        assert not DocumentationIndex._parse("\n\n\n").entries

    def test_no_heading_input_produces_no_entries(self) -> None:
        assert not DocumentationIndex._parse("plain prose without headings\n").entries


class TestExtractFirstParagraph:
    def test_returns_joined_lines_up_to_blank_line(self) -> None:
        lines = ["intro one", "intro two", "", "after blank"]
        assert _extract_first_paragraph(lines, 0) == "intro one intro two"

    def test_skips_leading_blank_lines(self) -> None:
        lines = ["", "  ", "intro"]
        assert _extract_first_paragraph(lines, 0) == "intro"

    def test_stops_at_heading_line(self) -> None:
        lines = ["intro", "## Next Section", "not part of paragraph"]
        assert _extract_first_paragraph(lines, 0) == "intro"

    def test_strips_bold_markers(self) -> None:
        lines = ["**bold** lead", "plain"]
        assert _extract_first_paragraph(lines, 0) == "bold lead plain"

    def test_removes_italic_marker_notes(self) -> None:
        lines = ["text _(footnote)_ end"]
        assert _extract_first_paragraph(lines, 0) == "text  end"

    def test_strips_trailing_asterisks(self) -> None:
        lines = ["*emphasized*"]
        assert _extract_first_paragraph(lines, 0) == "emphasized"

    def test_no_content_returns_empty(self) -> None:
        assert not _extract_first_paragraph(["## Next"], 0)
        assert not _extract_first_paragraph(["", ""], 0)
