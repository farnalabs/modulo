"""Unit tests for the canonicalizer + reserved-key set (FAR-142).

``modulo.db.lifecycle_refs`` owns the work-item canonicalisation rules, the
reserved input-payload keys, and the deterministic canonical journey id. These
tests lock the contract:

  * canonicalisation is idempotent (round-trip: canonical(canonical(x)) == canonical(x))
  * distinct raw refs never collapse onto each other (injectivity on the
    canonical space, modulo the intended #123/123 equivalence)
  * per-kind cases (#123, 123, owner/repo#123, https://github.com/.../pull/123,
    FAR-123) canonicalise as documented
  * the reserved-key set contains the three forge keys
  * ``_apply_payload_mapping`` rejects reserved-key mapping targets
  * ``canonical_work_item_id`` is deterministic across calls
"""

import uuid

import pytest

from modulo.core.trigger_engine import _apply_payload_mapping
from modulo.db.lifecycle_refs import (
    _RESERVED_INPUT_PAYLOAD_KEYS,
    canonical_work_item_id,
    canonicalise_kind,
    canonicalise_ref,
    validate_ref_entry,
)

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestCanonicaliseKind:
    def test_strips_whitespace_and_lowercases(self) -> None:
        assert canonicalise_kind(" GitHub Issue ") == "github_issue"

    def test_collapses_inner_whitespace(self) -> None:
        assert canonicalise_kind("github   issue") == "github_issue"

    def test_rejects_none(self) -> None:
        with pytest.raises(ValueError, match="kind must not be None"):
            canonicalise_kind(None)

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            canonicalise_kind("   ")


class TestCanonicaliseRef:
    def test_round_trip_github_issue(self) -> None:
        raw = "https://github.com/owner/repo/issues/123"
        once = canonicalise_ref("github_issue", raw)
        assert once == "owner/repo#123"
        assert canonicalise_ref("github_issue", once) == once

    def test_round_trip_linear(self) -> None:
        once = canonicalise_ref("linear", "far-123")
        assert once == "FAR-123"
        assert canonicalise_ref("linear", once) == once

    def test_round_trip_generic(self) -> None:
        once = canonicalise_ref("zendesk", "#T-42")
        assert once == "T-42"
        assert canonicalise_ref("zendesk", once) == once

    def test_github_hash_prefix_collapses_to_plain_number(self) -> None:
        assert canonicalise_ref("github_issue", "#123") == "123"
        assert canonicalise_ref("github_issue", "123") == "123"

    def test_github_owner_repo_does_not_collapse_with_plain_number(self) -> None:
        assert canonicalise_ref("github_issue", "owner/repo#123") == "owner/repo#123"
        assert canonicalise_ref("github_issue", "owner/repo#123") != canonicalise_ref("github_issue", "123")

    def test_github_url_forms(self) -> None:
        assert canonicalise_ref("github_pr", "https://github.com/a/b/pull/123") == "a/b#123"
        assert canonicalise_ref("github_issue", "https://github.com/a/b/issues/123") == "a/b#123"
        assert canonicalise_ref("github", "https://www.github.com/a/b/commit/7") == "a/b#7"

    def test_distinct_refs_do_not_collapse(self) -> None:
        assert canonicalise_ref("github_issue", "123") != canonicalise_ref("github_issue", "1234")
        assert canonicalise_ref("linear", "FAR-123") != canonicalise_ref("linear", "FAR-124")
        assert canonicalise_ref("linear", "FAR-123") != canonicalise_ref("github_issue", "123")

    def test_linear_variants(self) -> None:
        assert canonicalise_ref("linear", "far 123") == "FAR-123"
        assert canonicalise_ref("linear", "FAR:123") == "FAR-123"
        assert canonicalise_ref("linear", "https://linear.app/acme/issue/FAR-123/xyz") == "FAR-123"

    def test_rejects_none_ref(self) -> None:
        with pytest.raises(ValueError, match="ref must not be None"):
            canonicalise_ref("github_issue", None)

    def test_rejects_empty_ref(self) -> None:
        with pytest.raises(ValueError, match="ref must not be empty"):
            canonicalise_ref("github_issue", "   ")


class TestValidateRefEntry:
    def test_returns_canonicalised_entry(self) -> None:
        assert validate_ref_entry({"kind": "GitHub Issue", "ref": "https://github.com/a/b/pull/5"}) == {
            "kind": "github_issue",
            "ref": "a/b#5",
            "source": "derived",
        }

    def test_preserves_reported_source(self) -> None:
        assert validate_ref_entry({"kind": "linear", "ref": "FAR-1", "source": "reported"}) == {
            "kind": "linear",
            "ref": "FAR-1",
            "source": "reported",
        }

    def test_preserves_optional_status(self) -> None:
        assert validate_ref_entry({"kind": "linear", "ref": "FAR-1", "source": "derived", "status": "done"}) == {
            "kind": "linear",
            "ref": "FAR-1",
            "source": "derived",
            "status": "done",
        }

    def test_rejects_non_dict(self) -> None:
        with pytest.raises(ValueError, match="must be a dict"):
            validate_ref_entry("not-a-dict")

    def test_rejects_missing_ref(self) -> None:
        with pytest.raises(ValueError, match="'ref' is required"):
            validate_ref_entry({"kind": "linear"})

    def test_rejects_invalid_source(self) -> None:
        with pytest.raises(ValueError, match="'source' must be one of"):
            validate_ref_entry({"kind": "linear", "ref": "FAR-1", "source": "hallucinated"})

    def test_rejects_invalid_status(self) -> None:
        with pytest.raises(ValueError, match="'status' must be one of"):
            validate_ref_entry({"kind": "linear", "ref": "FAR-1", "status": "abandoned"})


class TestReservedKeys:
    def test_contains_all_three_forge_keys(self) -> None:
        assert "_work_item_id" in _RESERVED_INPUT_PAYLOAD_KEYS
        assert "_modulo.work_item" in _RESERVED_INPUT_PAYLOAD_KEYS
        assert "_feedback_correction" in _RESERVED_INPUT_PAYLOAD_KEYS

    def test_payload_mapping_rejects_reserved_targets(self) -> None:
        for reserved in ("_work_item_id", "_modulo.work_item", "_feedback_correction"):
            with pytest.raises(ValueError, match="reserved and cannot be mapped"):
                _apply_payload_mapping({"github": {"number": 1}}, {reserved: "github.number"})

    def test_payload_mapping_empty_mapping_passthrough(self) -> None:
        raw = {"a": 1, "_work_item_id": "forged"}
        assert _apply_payload_mapping(raw, {}) == raw


class TestCanonicalWorkItemId:
    def test_deterministic_across_calls(self) -> None:
        a = canonical_work_item_id(_ORG, "github_issue", "https://github.com/a/b/pull/5")
        b = canonical_work_item_id(_ORG, "github_issue", "a/b#5")
        assert a == b

    def test_differs_across_orgs(self) -> None:
        other_org = uuid.UUID("00000000-0000-0000-0000-000000000002")
        assert canonical_work_item_id(_ORG, "linear", "FAR-1") != canonical_work_item_id(other_org, "linear", "FAR-1")

    def test_differs_across_kinds(self) -> None:
        assert canonical_work_item_id(_ORG, "linear", "FAR-1") != canonical_work_item_id(_ORG, "github_issue", "FAR-1")

    def test_is_a_uuid(self) -> None:
        assert isinstance(canonical_work_item_id(_ORG, "linear", "FAR-1"), uuid.UUID)
