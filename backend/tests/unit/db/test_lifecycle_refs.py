"""Hermetic unit tests for db/lifecycle_refs.py (FAR-142).

Covers the canonical work-item ref rules owned by ``modulo.db.lifecycle_refs``:

  * the reserved input-payload key contract (``_work_item_id``,
    ``_modulo.work_item``, ``_feedback_correction``) — the exact set the
    webhook/``create_run`` chokepoints refuse to forge;
  * ``canonicalise_kind`` — None/blank rejection, lowercase + whitespace
    collapse;
  * github-family ref canonicalisation — URL (pull/issues/commit), ``owner/repo#N``
    and ``#N``/``N`` collapse forms;
  * linear/jira ref canonicalisation — URL-prefix strip, leading-``#`` strip,
    ``FAR 1`` / ``FAR-1`` / ``FAR:1`` project-key normalisation, plain-uppercase
    fallback;
  * ``canonicalise_ref`` per-kind dispatch and None/blank rejection;
  * ``validate_ref_entry`` — non-dict entry, missing/blank ref, invalid source
    and invalid status rejection, plus canonicalised output shape;
  * ``canonical_work_item_id`` — deterministic uuid5 derivation, so
    ``#5`` and ``5`` (or a github URL and ``owner/repo#N``) land on the SAME
    journey row, while differing kind/ref/org land elsewhere.
"""

import uuid

import pytest

from modulo.db.lifecycle_refs import (
    _RESERVED_INPUT_PAYLOAD_KEYS,
    canonical_work_item_id,
    canonicalise_kind,
    canonicalise_ref,
    validate_ref_entry,
)

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-000000000002")


class TestReservedInputPayloadKeys:
    def test_reserved_keys_are_exactly_the_system_injected_set(self) -> None:
        assert frozenset({"_work_item_id", "_modulo.work_item", "_feedback_correction"}) == _RESERVED_INPUT_PAYLOAD_KEYS


class TestCanonicaliseKind:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("github", "github"),
            ("GitHub Issue", "github_issue"),
            ("  GitHub  Issue  ", "github_issue"),
            ("GITHUB_PR", "github_pr"),
            ("linear", "linear"),
            ("JIRA", "jira"),
        ],
    )
    def test_normalises_kind(self, raw: str, expected: str) -> None:
        assert canonicalise_kind(raw) == expected

    def test_none_raises(self) -> None:
        with pytest.raises(ValueError, match="kind must not be None"):
            canonicalise_kind(None)

    def test_blank_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="kind must not be empty"):
            canonicalise_kind("   ")


class TestCanonicaliseRefGithub:
    @pytest.mark.parametrize(
        ("kind", "raw", "expected"),
        [
            ("github", "https://github.com/a/b/pull/5", "a/b#5"),
            ("github_issue", "https://github.com/a/b/issues/7", "a/b#7"),
            ("github", "https://www.github.com/a/b/issues/7", "a/b#7"),
            ("github_issue", "a/b#5", "a/b#5"),
            ("github_pr", "a/b#5", "a/b#5"),
            ("github_issue", "#123", "123"),
            ("github_issue", "123", "123"),
            ("github_issue", "   #123   ", "123"),
        ],
        ids=[
            "github-pull",
            "github-issue",
            "github-www",
            "issue-shorthand",
            "pr-shorthand",
            "issue-hash",
            "issue-digits",
            "issue-hash-spaced",
        ],
    )
    def test_github_ref_forms(self, kind: str, raw: str, expected: str) -> None:
        assert canonicalise_ref(kind, raw) == expected

    def test_commit_url_with_sha_is_preserved(self) -> None:
        raw = "https://github.com/a/b/commit/abc123"
        assert canonicalise_ref("github_pr", raw) == raw


class TestCanonicaliseRefTracker:
    @pytest.mark.parametrize(
        ("kind", "raw", "expected"),
        [
            ("linear", "far 123", "FAR-123"),
            ("linear", "far-123", "FAR-123"),
            ("jira", "FAR:123", "FAR-123"),
            ("linear", "https://linear.app/acme/issue/FAR-123/xyz", "FAR-123"),
            ("jira", "#FAR-123", "FAR-123"),
            ("linear", "  far 123  ", "FAR-123"),
            ("linear", "abc", "ABC"),
            ("jira", "far_123", "FAR_123"),
            ("linear", "far-123abc", "FAR-123ABC"),
        ],
        ids=[
            "linear-spaces",
            "linear-lower",
            "jira-colon",
            "linear-url",
            "jira-hash",
            "linear-padded",
            "linear-upper",
            "jira-underscore",
            "linear-mixed",
        ],
    )
    def test_tracker_ref_forms(self, kind: str, raw: str, expected: str) -> None:
        assert canonicalise_ref(kind, raw) == expected


class TestCanonicaliseRefGeneric:
    def test_strips_leading_hash_and_whitespace(self) -> None:
        assert canonicalise_ref("feature", "  #  ref  ") == "ref"

    def test_unknown_kind_keeps_ref(self) -> None:
        assert canonicalise_ref("epic", "EPIC-42") == "EPIC-42"

    def test_none_ref_raises(self) -> None:
        with pytest.raises(ValueError, match="ref must not be None"):
            canonicalise_ref("feature", None)

    def test_blank_ref_raises(self) -> None:
        with pytest.raises(ValueError, match="ref must not be empty"):
            canonicalise_ref("feature", "   ")


class TestValidateRefEntry:
    def test_canonicalises_valid_entry_with_default_source(self) -> None:
        entry = validate_ref_entry({"kind": "GitHub Issue", "ref": "https://github.com/a/b/pull/5"})
        assert entry == {"kind": "github_issue", "ref": "a/b#5", "source": "derived"}

    def test_canonicalises_valid_entry_with_source_and_status(self) -> None:
        entry = validate_ref_entry({"kind": "jira", "ref": "far-1", "source": "reported", "status": "done"})
        assert entry == {"kind": "jira", "ref": "FAR-1", "source": "reported", "status": "done"}

    def test_omits_status_when_not_provided(self) -> None:
        entry = validate_ref_entry({"kind": "linear", "ref": "FAR-1", "source": "derived"})
        assert "status" not in entry

    @pytest.mark.parametrize(
        "entry",
        [
            "not-a-dict",
            ["kind", "ref"],
            None,
        ],
    )
    def test_non_dict_entry_raises(self, entry: object) -> None:
        with pytest.raises(ValueError, match="must be a dict"):
            validate_ref_entry(entry)

    @pytest.mark.parametrize(
        "entry",
        [
            {"kind": "feature"},
            {"kind": "feature", "ref": ""},
            {"kind": "feature", "ref": "   "},
        ],
    )
    def test_missing_or_blank_ref_raises(self, entry: dict[str, object]) -> None:
        with pytest.raises(ValueError, match="'ref' is required"):
            validate_ref_entry(entry)

    def test_invalid_source_raises(self) -> None:
        with pytest.raises(ValueError, match="'source' must be one of"):
            validate_ref_entry({"kind": "feature", "ref": "1", "source": "fake"})

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValueError, match="'status' must be one of"):
            validate_ref_entry({"kind": "feature", "ref": "1", "status": "in_progress"})

    def test_blank_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="kind must not be empty"):
            validate_ref_entry({"kind": "", "ref": "1"})


class TestCanonicalWorkItemId:
    def test_is_deterministic_golden_value(self) -> None:
        result = canonical_work_item_id(_ORG, "github_issue", "a/b#5")
        assert result == uuid.UUID("20ad65f6-0880-50c9-81e4-2b0902e10a28")

    def test_hash_collapses_to_bare_ref_for_same_row(self) -> None:
        assert canonical_work_item_id(_ORG, "github_issue", "#5") == canonical_work_item_id(_ORG, "github_issue", "5")

    def test_github_url_and_qualified_ref_collapse_to_same_row(self) -> None:
        assert canonical_work_item_id(_ORG, "github_issue", "https://github.com/a/b/pull/5") == canonical_work_item_id(
            _ORG, "github_issue", "a/b#5"
        )

    def test_tracker_spacing_collapses_to_same_row(self) -> None:
        assert canonical_work_item_id(_ORG, "linear", "far 123") == canonical_work_item_id(_ORG, "linear", "far-123")

    def test_kind_participates_in_the_id(self) -> None:
        assert canonical_work_item_id(_ORG, "github_issue", "#5") != canonical_work_item_id(_ORG, "jira", "FAR-5")

    def test_org_participates_in_the_id(self) -> None:
        assert canonical_work_item_id(_ORG, "github_issue", "#5") != canonical_work_item_id(
            _OTHER_ORG, "github_issue", "#5"
        )

    def test_returns_typed_uuid(self) -> None:
        result = canonical_work_item_id(_ORG, "github_issue", "#5")
        assert isinstance(result, uuid.UUID)
        assert result.version == 5

    def test_invalid_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="kind must not be None"):
            canonical_work_item_id(_ORG, None, "#5")

    def test_invalid_ref_raises(self) -> None:
        with pytest.raises(ValueError, match="ref must not be None"):
            canonical_work_item_id(_ORG, "github_issue", None)
