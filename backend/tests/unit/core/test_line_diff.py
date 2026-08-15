"""Unit tests for modulo.core.line_diff.

The shared diff walk backs the prompt-version and node-output diff endpoints,
so these tests lock content, ordering, and 1-based line-number accounting per
opcode kind. They double as the regression guard for multi-line unchanged hunks,
which previously repeated the first hunk line across the whole run.
"""

from modulo.core.line_diff import iter_line_diffs


def materialize(a: str, b: str) -> list[tuple[str, str, int | None, int | None]]:
    return list(iter_line_diffs(a.splitlines(keepends=True), b.splitlines(keepends=True)))


class TestLineDiff:
    def test_identical_multi_line_keeps_each_line(self) -> None:
        assert materialize("one\ntwo\nthree\n", "one\ntwo\nthree\n") == [
            ("unchanged", "one", 1, 1),
            ("unchanged", "two", 2, 2),
            ("unchanged", "three", 3, 3),
        ]

    def test_replace_preserves_context_and_numbers(self) -> None:
        assert materialize("one\ntwo\nthree\nfour\n", "one\ntwo\nCHANGED\nfour\n") == [
            ("unchanged", "one", 1, 1),
            ("unchanged", "two", 2, 2),
            ("removed", "three", 3, None),
            ("added", "CHANGED", None, 3),
            ("unchanged", "four", 4, 4),
        ]

    def test_insert_only_appends_unmatched_b_lines(self) -> None:
        assert materialize("one\ntwo\n", "one\ntwo\nthree\nfour\n") == [
            ("unchanged", "one", 1, 1),
            ("unchanged", "two", 2, 2),
            ("added", "three", None, 3),
            ("added", "four", None, 4),
        ]

    def test_delete_only_reports_unmatched_a_lines(self) -> None:
        assert materialize("one\ntwo\nthree\n", "one\n") == [
            ("unchanged", "one", 1, 1),
            ("removed", "two", 2, None),
            ("removed", "three", 3, None),
        ]

    def test_diff_trailing_newline_stripped_from_content(self) -> None:
        assert materialize("a\nb\n", "a\nb") == [
            ("unchanged", "a", 1, 1),
            ("removed", "b", 2, None),
            ("added", "b", None, 2),
        ]

    def test_empty_listings_produce_no_rows(self) -> None:
        assert not materialize("", "")

    def test_single_line_insert(self) -> None:
        assert materialize("", "hi\n") == [("added", "hi", None, 1)]
