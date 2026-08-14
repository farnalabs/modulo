"""Unit tests for the self-report work-item ref parser (FAR-143).

Locks the FAR-143 contract:

  * ``parse_self_report_refs`` extracts raw entries from BOTH the top-level key
    placement and the FAR-125 node-keyed split placement, plus arbitrary
    nesting, and flattens every matching location.
  * ``validate_and_normalise_reported_refs`` canonicalises ``kind`` + ``ref``
    via ``modulo.db.lifecycle_refs``, forces provenance to ``"reported"``,
    drops unknown statuses, dedups by ``(kind, ref, source)``, and caps at
    ``max_refs``.
  * both functions are PURE: no DB, deterministic, and the input is never
    mutated.
"""

from __future__ import annotations

import json
from typing import Any

from modulo.core.lifecycle_map.self_report import (
    parse_self_report_refs,
    validate_and_normalise_reported_refs,
)

_ENTRY: dict[str, Any] = {"kind": "github_pr", "ref": "#123"}


# ---------------------------------------------------------------------------
# parse_self_report_refs -- extraction
# ---------------------------------------------------------------------------


def test_parse_top_level_key() -> None:
    raw = parse_self_report_refs({"work_item_refs": [_ENTRY]})
    assert raw == [_ENTRY]


def test_parse_top_level_dotted_key() -> None:
    raw = parse_self_report_refs({"modulo.work_item_refs": [_ENTRY]})
    assert raw == [_ENTRY]


def test_parse_node_keyed_output() -> None:
    merged = {"node_1": {"status": "completed", "output": {"work_item_refs": [_ENTRY]}}}
    assert parse_self_report_refs(merged) == [_ENTRY]


def test_parse_node_keyed_equals_top_level() -> None:
    top = parse_self_report_refs({"work_item_refs": [_ENTRY]})
    node = parse_self_report_refs({"node_1": {"output": {"work_item_refs": [_ENTRY]}}})
    assert top == node == [_ENTRY]


def test_parse_flattens_top_level_and_node_locations() -> None:
    merged = {
        "work_item_refs": [{"kind": "linear", "ref": "FAR-1"}],
        "node_1": {"output": {"modulo.work_item_refs": [{"kind": "github_pr", "ref": "2"}]}},
        "node_2": {"status": "completed", "output": {"touched_work_items": [{"kind": "jira", "ref": "JIRA-3"}]}},
    }
    assert parse_self_report_refs(merged) == [
        {"kind": "linear", "ref": "FAR-1"},
        {"kind": "github_pr", "ref": "2"},
        {"kind": "jira", "ref": "JIRA-3"},
    ]


def test_parse_nested_node_output_extraction() -> None:
    merged = {"node_1": {"output": {"result": {"details": {"touched_work_items": [_ENTRY]}}}}}
    assert parse_self_report_refs(merged) == [_ENTRY]


def test_parse_single_dict_value_counts_as_one_entry() -> None:
    assert parse_self_report_refs({"work_item_refs": {"kind": "linear", "ref": "FAR-1"}}) == [
        {"kind": "linear", "ref": "FAR-1"}
    ]


def test_parse_scalar_value_surfaces_as_malformed_entry() -> None:
    raw = parse_self_report_refs({"work_item_refs": "FAR-1"})
    assert raw == ["FAR-1"]
    valid, counters = validate_and_normalise_reported_refs(raw)
    assert valid == []
    assert counters["malformed"] == 1


def test_parse_empty_when_no_matching_keys() -> None:
    assert not parse_self_report_refs({"node_1": {"output": {"summary": "nope"}}})
    assert not parse_self_report_refs({})
    assert not parse_self_report_refs("not a dict")
    assert not parse_self_report_refs(None)  # type: ignore[arg-type]


def test_parse_handles_self_referential_dict_cycle() -> None:
    merged: dict[str, Any] = {"work_item_refs": [_ENTRY]}
    merged["node_1"] = {"output": merged}
    # The cyclic node is walked once; the top-level refs are still collected and
    # the walk terminates without recursing forever.
    assert parse_self_report_refs(merged) == [_ENTRY]


def test_parse_handles_self_referential_list_cycle() -> None:
    inner: list[Any] = [_ENTRY]
    inner.append(inner)
    raw = parse_self_report_refs({"work_item_refs": inner})
    assert raw == [_ENTRY, inner]


def test_parse_is_pure_deterministic_and_non_mutating() -> None:
    merged = {
        "work_item_refs": [_ENTRY],
        "node_1": {"output": {"touched_work_items": [{"kind": "linear", "ref": "FAR-1"}]}},
        "other": {"data": [1, 2]},
    }
    snapshot = json.dumps(merged, sort_keys=True)
    first = parse_self_report_refs(merged)
    second = parse_self_report_refs(merged)
    assert first == second
    assert json.dumps(merged, sort_keys=True) == snapshot


# ---------------------------------------------------------------------------
# validate_and_normalise_reported_refs -- acceptance
# ---------------------------------------------------------------------------


def test_validate_accepts_valid_entry() -> None:
    valid, counters = validate_and_normalise_reported_refs([{"kind": "github_pr", "ref": "#123"}])
    assert valid == [{"kind": "github_pr", "ref": "123", "source": "reported"}]
    assert counters == {"malformed": 0, "capped": 0, "valid": 1}


def test_validate_empty_entries() -> None:
    valid, counters = validate_and_normalise_reported_refs([])
    assert valid == []
    assert counters == {"malformed": 0, "capped": 0, "valid": 0}


def test_validate_forces_reported_source() -> None:
    valid, _ = validate_and_normalise_reported_refs([{"kind": "github_pr", "ref": "#1", "source": "derived"}])
    assert valid[0]["source"] == "reported"


def test_validate_keeps_optional_valid_status() -> None:
    valid, _ = validate_and_normalise_reported_refs([{"kind": "github_pr", "ref": "#1", "status": "attempted"}])
    assert valid[0]["status"] == "attempted"


# ---------------------------------------------------------------------------
# validate_and_normalise_reported_refs -- malformed
# ---------------------------------------------------------------------------


def test_validate_missing_kind_is_malformed() -> None:
    valid, counters = validate_and_normalise_reported_refs([{"ref": "#123"}])
    assert valid == []
    assert counters["malformed"] == 1


def test_validate_missing_ref_is_malformed() -> None:
    valid, counters = validate_and_normalise_reported_refs([{"kind": "github_pr"}])
    assert valid == []
    assert counters["malformed"] == 1


def test_validate_blank_kind_and_ref_are_malformed() -> None:
    valid, counters = validate_and_normalise_reported_refs([{"kind": "  ", "ref": "#1"}, {"kind": "pr", "ref": ""}])
    assert valid == []
    assert counters["malformed"] == 2


def test_validate_non_dict_entry_is_malformed() -> None:
    valid, counters = validate_and_normalise_reported_refs(["not-a-dict", 42, {"kind": "pr", "ref": "1"}])
    assert valid == [{"kind": "pr", "ref": "1", "source": "reported"}]
    assert counters["malformed"] == 2
    assert counters["valid"] == 1


def test_parse_wrong_type_list_of_strings_counts_malformed() -> None:
    raw = parse_self_report_refs({"work_item_refs": ["abc", "def"]})
    valid, counters = validate_and_normalise_reported_refs(raw)
    assert valid == []
    assert counters == {"malformed": 2, "capped": 0, "valid": 0}


# ---------------------------------------------------------------------------
# validate_and_normalise_reported_refs -- status
# ---------------------------------------------------------------------------


def test_validate_drops_unknown_status() -> None:
    valid, counters = validate_and_normalise_reported_refs(
        [
            {"kind": "github_pr", "ref": "#1", "status": "completed"},
            {"kind": "github_pr", "ref": "#2", "status": "done"},
        ]
    )
    assert valid == [
        {"kind": "github_pr", "ref": "1", "source": "reported"},
        {"kind": "github_pr", "ref": "2", "source": "reported", "status": "done"},
    ]
    assert counters["malformed"] == 0


def test_validate_drops_non_string_status() -> None:
    valid, _ = validate_and_normalise_reported_refs([{"kind": "github_pr", "ref": "#1", "status": {"nested": True}}])
    assert valid[0] == {"kind": "github_pr", "ref": "1", "source": "reported"}


# ---------------------------------------------------------------------------
# validate_and_normalise_reported_refs -- canonicalisation
# ---------------------------------------------------------------------------


def test_validate_applies_canonicalisation() -> None:
    valid, _ = validate_and_normalise_reported_refs(
        [
            {"kind": "pr", "ref": "#123"},
            {"kind": "github_pr", "ref": "https://github.com/owner/repo/pull/456"},
            {"kind": "linear", "ref": "far-789"},
        ]
    )
    assert valid == [
        {"kind": "pr", "ref": "123", "source": "reported"},
        {"kind": "github_pr", "ref": "owner/repo#456", "source": "reported"},
        {"kind": "linear", "ref": "FAR-789", "source": "reported"},
    ]


def test_validate_preserves_qualified_owner_repo_ref() -> None:
    valid, _ = validate_and_normalise_reported_refs([{"kind": "github_pr", "ref": "owner/repo#123"}])
    assert valid[0]["ref"] == "owner/repo#123"


# ---------------------------------------------------------------------------
# validate_and_normalise_reported_refs -- dedup + cap
# ---------------------------------------------------------------------------


def test_validate_dedups_by_kind_ref_source() -> None:
    entries = [
        {"kind": "github_pr", "ref": "#123", "status": "attempted"},
        {"kind": "GitHub PR", "ref": "123", "status": "done"},
        {"kind": "github_pr", "ref": "#124"},
    ]
    valid, counters = validate_and_normalise_reported_refs(entries)
    assert valid == [
        {"kind": "github_pr", "ref": "123", "source": "reported", "status": "attempted"},
        {"kind": "github_pr", "ref": "124", "source": "reported"},
    ]
    assert counters == {"malformed": 0, "capped": 0, "valid": 2}


def test_validate_caps_at_max_refs() -> None:
    entries = [{"kind": "linear", "ref": f"FAR-{i}"} for i in range(1, 4)]
    valid, counters = validate_and_normalise_reported_refs(entries, max_refs=2)
    assert [e["ref"] for e in valid] == ["FAR-1", "FAR-2"]
    assert counters == {"malformed": 0, "capped": 1, "valid": 2}


def test_validate_cap_not_consumed_by_duplicates() -> None:
    entries = [
        {"kind": "linear", "ref": "FAR-1"},
        {"kind": "linear", "ref": "FAR-2"},
        {"kind": "linear", "ref": "FAR-1"},
        {"kind": "linear", "ref": "FAR-3"},
        {"kind": "linear", "ref": "FAR-3"},
    ]
    valid, counters = validate_and_normalise_reported_refs(entries, max_refs=2)
    assert [e["ref"] for e in valid] == ["FAR-1", "FAR-2"]
    assert counters == {"malformed": 0, "capped": 1, "valid": 2}


def test_validate_is_pure_and_deterministic() -> None:
    entries = [{"kind": "github_pr", "ref": "#1"}, {"kind": "github_pr", "ref": "#1"}]
    first = validate_and_normalise_reported_refs(entries)
    second = validate_and_normalise_reported_refs(entries)
    assert first == second
