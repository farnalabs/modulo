"""Unit tests for scripts/generate_changelog.py (FAR-1155).

Covers the pure classification / rendering / CHANGELOG section-insert logic
with fixture data only — no network, no ``git``, no ``gh``. The data-collection
layer is exercised indirectly through the pure functions it feeds.
"""

from __future__ import annotations

import sys
from datetime import date
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "generate_changelog.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/generate_changelog.py)")

_loader = SourceFileLoader("generate_changelog", str(script_path))
mod = module_from_spec(spec_from_loader("generate_changelog", _loader))
# Register before exec: @dataclass resolves string annotations via
# sys.modules[cls.__module__].__dict__, and crashes with AttributeError if
# the module is not yet in sys.modules.
sys.modules["generate_changelog"] = mod
_loader.exec_module(mod)

_WHEN = date(2026, 9, 22)


def _pr(number: int, *labels: str, title: str | None = None) -> object:
    return mod.PrInfo(
        number=number,
        title=title if title is not None else f"Change {number}",
        url=f"https://example.test/pull/{number}",
        labels=tuple(labels),
    )


def _fixture_prs() -> list[object]:
    """4 listed PRs (1 breaking, 2 features, 1 fix) + 5 other (1 none, 4 unlabelled)."""
    return [
        _pr(101, "changelog: breaking", title="Drop the legacy API"),
        _pr(102, "changelog: feature", title="Add pipeline templates"),
        _pr(103, "changelog: feature", title="Add output diff view"),
        _pr(104, "changelog: fix", title="Fix trigger retry loop"),
        _pr(105, "changelog: none", title="Internal rename"),
        _pr(106, title="Unlabelled refactor"),
        _pr(107, title="Unlabelled docs"),
        _pr(108, title="Unlabelled test"),
        _pr(109, title="Unlabelled infra"),
    ]


# ---------------------------------------------------------------------------
# extract_pr_numbers
# ---------------------------------------------------------------------------


def test_extract_pr_numbers_from_squash_suffix_and_merge_subjects() -> None:
    subjects = [
        "feat: shiny thing (#123)",
        "fix: quiet thing (#456)",
        "no PR reference here",
        "Merge pull request #789 from owner/branch",
    ]
    assert mod.extract_pr_numbers(subjects) == [123, 456, 789]


def test_extract_pr_numbers_dedupes_preserving_order() -> None:
    subjects = ["first (#2)", "again (#2)", "third (#1)"]
    assert mod.extract_pr_numbers(subjects) == [2, 1]


def test_extract_pr_numbers_requires_suffix_at_end_of_subject() -> None:
    # "(#12) mention mid-subject is not a squash-merge PR suffix."
    subjects = ["chore: mentions (#12) but continues", "clean subject (#34)"]
    assert mod.extract_pr_numbers(subjects) == [34]


# ---------------------------------------------------------------------------
# classify_prs
# ---------------------------------------------------------------------------


def test_classify_groups_by_label_in_section_order() -> None:
    groups, other_count = mod.classify_prs(_fixture_prs())
    assert [pr.number for pr in groups["Breaking Changes"]] == [101]
    assert [pr.number for pr in groups["Features"]] == [102, 103]
    assert [pr.number for pr in groups["Fixes"]] == [104]
    assert other_count == 5


def test_classify_none_label_is_excluded_from_every_section() -> None:
    groups, other_count = mod.classify_prs([_pr(1, "changelog: none"), _pr(2, "changelog: fix")])
    listed = [pr.number for entries in groups.values() for pr in entries]
    assert listed == [2]
    assert other_count == 1


def test_classify_section_label_wins_over_none_and_breaking_wins_over_feature() -> None:
    groups, _ = mod.classify_prs(
        [
            _pr(10, "changelog: none", "changelog: feature"),
            _pr(11, "changelog: feature", "changelog: breaking"),
        ],
    )
    assert [pr.number for pr in groups["Breaking Changes"]] == [11]
    assert [pr.number for pr in groups["Features"]] == [10]


def test_other_count_excludes_prs_already_listed_in_a_section() -> None:
    """The 'Other' line counts ONLY unlisted PRs; listed ones are never double-counted."""
    prs = _fixture_prs()
    groups, other_count = mod.classify_prs(prs)
    listed_total = sum(len(entries) for entries in groups.values())
    assert listed_total == 4
    assert other_count == 5
    assert listed_total + other_count == len(prs)

    markdown = mod.render_release_section("1.0.0", groups, other_count, "", _WHEN)
    assert "_Plus 5 internal changes and fixes._" in markdown
    assert "_Plus 9" not in markdown


# ---------------------------------------------------------------------------
# render_release_section
# ---------------------------------------------------------------------------


def test_render_orders_sections_breaking_then_features_then_fixes() -> None:
    groups, other_count = mod.classify_prs(_fixture_prs())
    markdown = mod.render_release_section("1.0.0", groups, other_count, "", _WHEN)
    idx_breaking = markdown.index("### Breaking Changes")
    idx_features = markdown.index("### Features")
    idx_fixes = markdown.index("### Fixes")
    assert idx_breaking < idx_features < idx_fixes


def test_render_places_summary_between_heading_and_first_section() -> None:
    groups, other_count = mod.classify_prs(_fixture_prs())
    markdown = mod.render_release_section(
        "1.2.3",
        groups,
        other_count,
        "A high-level narrative for release readers.",
        _WHEN,
    )
    assert markdown.startswith(
        "## [1.2.3] — 2026-09-22\n\nA high-level narrative for release readers.\n\n### Breaking Changes",
    )


def test_render_lists_only_labelled_prs_with_links() -> None:
    groups, other_count = mod.classify_prs(_fixture_prs())
    markdown = mod.render_release_section("1.0.0", groups, other_count, "", _WHEN)
    assert "- [#102](https://example.test/pull/102) — Add pipeline templates" in markdown
    assert "- [#101](https://example.test/pull/101) — Drop the legacy API" in markdown
    assert "Internal rename" not in markdown
    assert "Unlabelled refactor" not in markdown


def test_render_omits_empty_sections_and_other_line_when_nothing_to_report() -> None:
    groups = {name: [] for name in mod.SECTION_ORDER}
    markdown = mod.render_release_section("9.9.9", groups, 0, "", _WHEN)
    assert markdown == "## [9.9.9] — 2026-09-22\n"
    assert "### " not in markdown
    assert "Plus" not in markdown


# ---------------------------------------------------------------------------
# upsert_section (--output insert / prepend / replace idempotency)
# ---------------------------------------------------------------------------

_HEADER = "# Changelog\n\nIntro prose stays put.\n"
_UNRELEASED = "## [Unreleased]\n\n### Added\n\n- unreleased entry\n"
_OLD_RELEASE = "## [0.1.0] — 2026-01-01\n\n- old entry\n"


def test_upsert_prepends_below_header_and_unreleased_keeping_existing_content() -> None:
    doc = _HEADER + "\n" + _UNRELEASED + "\n" + _OLD_RELEASE
    section = "## [0.2.0] — 2026-09-22\n\n- new entry\n"
    out = mod.upsert_section(doc, section, "0.2.0")
    assert out.index("## [Unreleased]") < out.index("## [0.2.0]") < out.index("## [0.1.0]")
    assert "Intro prose stays put." in out
    assert "- unreleased entry" in out
    assert "- old entry" in out
    assert out.count("## [0.2.0]") == 1


def test_upsert_appends_to_header_only_doc_when_no_sections_exist() -> None:
    section = "## [0.1.0] — 2026-01-01\n\n- first ever entry\n"
    out = mod.upsert_section(_HEADER, section, "0.1.0")
    assert out.startswith("# Changelog")
    assert "## [0.1.0] — 2026-01-01" in out
    assert "- first ever entry" in out


def test_upsert_replaces_existing_version_section_just_that_section() -> None:
    doc = _HEADER + "\n" + _UNRELEASED + "\n" + "## [0.2.0] — 2026-09-22\n\n- first render\n\n" + _OLD_RELEASE
    replacement = "## [0.2.0] — 2026-09-23\n\n- second render\n"
    out = mod.upsert_section(doc, replacement, "0.2.0")
    assert "- second render" in out
    assert "- first render" not in out
    assert out.count("## [0.2.0]") == 1
    assert "- old entry" in out
    assert "- unreleased entry" in out


def test_upsert_is_idempotent_running_twice_does_not_duplicate() -> None:
    doc = _HEADER + "\n" + _UNRELEASED + "\n" + _OLD_RELEASE
    section = "## [0.2.0] — 2026-09-22\n\n- new entry\n"
    once = mod.upsert_section(doc, section, "0.2.0")
    twice = mod.upsert_section(once, section, "0.2.0")
    assert twice == once
    assert once.count("## [0.2.0]") == 1
    assert once.count("- new entry") == 1


def test_upsert_treats_version_prefix_as_distinct_sections() -> None:
    """0.1 must not match an existing 0.1.0 heading (or vice versa)."""
    doc = _HEADER + "\n" + "## [0.1.0] — 2026-01-01\n\n- old entry\n"
    section = "## [0.1] — 2026-02-01\n\n- different version\n"
    out = mod.upsert_section(doc, section, "0.1")
    assert out.count("## [0.1] —") == 1
    assert "## [0.1.0]" in out
    assert "- old entry" in out
