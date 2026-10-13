"""Unit tests for the platform-resolved changed sets + ``diff_policy`` (FAR-1691).

Covers the provenance model (``observed`` vs ``reported``), the resolution
precedence, the executable-risk classification, the pure ``diff_policy``
evaluation, and the Evidence-layer surfacing.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from modulo.core.eval_engine.changed_sets import (
    AGENT_CONFIG_RISK_PATTERNS,
    CHANGED_SET_EVIDENCE_PREFIX,
    ChangedSet,
    ChangedSetResolutionError,
    DiffPolicyConfig,
    DiffPolicyConfigError,
    changed_set_evidence_values,
    evaluate_diff_policy,
    is_executable_risk_path,
    normalize_path,
    record_changed_set_evidence,
    resolve_changed_set,
)
from modulo.core.eval_engine.evidence_layer import (
    EvidenceWriteAuthorisationError,
    assert_write_authorisation,
)
from modulo.core.provenance import PROVENANCE_OBSERVED, PROVENANCE_REPORTED


class _RecordingSession:
    """Minimal ``db.add`` recorder — the store write path only needs ``add``."""

    def __init__(self) -> None:
        self.rows: list[Any] = []

    def add(self, row: Any) -> None:
        self.rows.append(row)


# ---------------------------------------------------------------------------
# Path normalisation + executable-risk classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/app.py", "src/app.py"),
        ("./src/app.py", "src/app.py"),
        ("/src/app.py", "src/app.py"),
        ("src\\nested\\app.py", "src/nested/app.py"),
        ("  .claude/settings.json  ", ".claude/settings.json"),
    ],
)
def test_normalize_path(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        ".cursor/rules.md",
        ".vscode/tasks.json",
        ".github/workflows/ci.yml",
        "CLAUDE.md",
        "docs/AGENTS.md",
        ".mcp.json",
        "sub/.cursorrules",
    ],
    ids=[
        "claude-config",
        "cursor-config",
        "vscode-config",
        "github-workflow",
        "claude-md-root",
        "agents-md-nested",
        "mcp-json-root",
        "cursorrules-nested",
    ],
)
def test_executable_risk_paths_are_flagged(path: str) -> None:
    assert is_executable_risk_path(path)


@pytest.mark.parametrize("path", ["src/app.py", "README.md", "docs/guide.md", "package.json"])
def test_ordinary_paths_are_not_flagged(path: str) -> None:
    assert not is_executable_risk_path(path)


def test_risk_patterns_do_not_collapse_to_empty() -> None:
    assert AGENT_CONFIG_RISK_PATTERNS


# ---------------------------------------------------------------------------
# ChangedSet construction
# ---------------------------------------------------------------------------


def test_observed_factory_sets_provenance_and_sha() -> None:
    changed = ChangedSet.observed(["src/a.py"], commit_sha="abc123")
    assert changed.provenance == PROVENANCE_OBSERVED
    assert changed.source == "vcs_connector"
    assert changed.commit_sha == "abc123"


def test_reported_factory_sets_provenance() -> None:
    changed = ChangedSet.reported(["src/a.py"])
    assert changed.provenance == PROVENANCE_REPORTED
    assert changed.source == "agent_output"
    assert changed.commit_sha is None


def test_invalid_provenance_is_rejected() -> None:
    with pytest.raises(ValueError, match="provenance"):
        ChangedSet(files=("a",), provenance="wat", source="x")


def test_empty_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="source"):
        ChangedSet(files=("a",), provenance=PROVENANCE_REPORTED, source="")


def test_normalized_files_dedupes_sorts_and_normalises() -> None:
    changed = ChangedSet.reported(["b.py", "./a.py", "b.py", "src\\c.py", "  "])
    assert changed.normalized_files() == ("a.py", "b.py", "src/c.py")


# ---------------------------------------------------------------------------
# Resolution: observed wins, reported is the fallback, provenance is retained
# ---------------------------------------------------------------------------


def test_observed_set_is_evaluated_when_present() -> None:
    observed = ChangedSet.observed(["a.py", "b.py"])
    reported = ChangedSet.reported(["a.py"])
    resolved = resolve_changed_set(observed=observed, reported=reported)
    assert resolved.evaluated is observed
    assert resolved.evaluated_provenance == PROVENANCE_OBSERVED


def test_reported_set_is_evaluated_when_platform_cannot_observe() -> None:
    reported = ChangedSet.reported(["a.py"])
    resolved = resolve_changed_set(reported=reported)
    assert resolved.evaluated is reported
    assert resolved.evaluated_provenance == PROVENANCE_REPORTED


def test_resolution_requires_at_least_one_set() -> None:
    with pytest.raises(ChangedSetResolutionError):
        resolve_changed_set()


def test_observed_with_wrong_provenance_is_rejected() -> None:
    mislabelled = ChangedSet(files=("a.py",), provenance=PROVENANCE_REPORTED, source="vcs")
    with pytest.raises(ChangedSetResolutionError, match="observed"):
        resolve_changed_set(observed=mislabelled)


def test_reported_with_wrong_provenance_is_rejected() -> None:
    mislabelled = ChangedSet(files=("a.py",), provenance=PROVENANCE_OBSERVED, source="agent")
    with pytest.raises(ChangedSetResolutionError, match="reported"):
        resolve_changed_set(reported=mislabelled)


def test_drift_lists_both_sides_of_the_divergence() -> None:
    observed = ChangedSet.observed(["a.py", "b.py"])
    reported = ChangedSet.reported(["a.py", "c.py"])
    resolved = resolve_changed_set(observed=observed, reported=reported)
    assert resolved.observed_only == ("b.py",)
    assert resolved.reported_only == ("c.py",)
    assert resolved.drift == ("b.py", "c.py")


def test_drift_is_empty_when_sets_agree() -> None:
    observed = ChangedSet.observed(["a.py", "./a.py"])
    reported = ChangedSet.reported(["a.py"])
    resolved = resolve_changed_set(observed=observed, reported=reported)
    assert not resolved.drift


# ---------------------------------------------------------------------------
# DiffPolicyConfig parsing
# ---------------------------------------------------------------------------


def test_config_from_mapping_parses_globs_and_flags() -> None:
    config = DiffPolicyConfig.from_mapping(
        {
            "allow": ["src/**"],
            "deny": ["**/*.secret"],
            "forbidden": ["infra/**"],
            "flag_agent_config": False,
            "agent_config_is_violation": True,
        }
    )
    assert config.allow == ("src/**",)
    assert config.deny == ("**/*.secret",)
    assert config.forbidden == ("infra/**",)
    assert config.flag_agent_config is False
    assert config.agent_config_is_violation is True


def test_config_from_mapping_defaults() -> None:
    config = DiffPolicyConfig.from_mapping({})
    assert not config.allow
    assert config.flag_agent_config is True
    assert config.agent_config_is_violation is False


def test_config_rejects_bare_string_glob() -> None:
    with pytest.raises(DiffPolicyConfigError, match="list of glob strings"):
        DiffPolicyConfig.from_mapping({"deny": "**/*.secret"})


def test_config_rejects_non_boolean_flag() -> None:
    with pytest.raises(DiffPolicyConfigError, match="boolean"):
        DiffPolicyConfig.from_mapping({"flag_agent_config": "yes"})


def test_config_treats_explicit_none_glob_list_as_empty() -> None:
    config = DiffPolicyConfig.from_mapping({"deny": None})
    assert not config.deny


def test_config_rejects_non_list_glob_container() -> None:
    with pytest.raises(DiffPolicyConfigError, match="list of glob strings"):
        DiffPolicyConfig.from_mapping({"allow": 5})


def test_config_rejects_non_string_glob_entry() -> None:
    with pytest.raises(DiffPolicyConfigError, match="entries must be strings"):
        DiffPolicyConfig.from_mapping({"forbidden": ["ok/**", 7]})


# ---------------------------------------------------------------------------
# DiffPolicy evaluation
# ---------------------------------------------------------------------------


def _resolved(*files: str, observed: bool = True):
    changed = ChangedSet.observed(list(files)) if observed else ChangedSet.reported(list(files))
    return resolve_changed_set(observed=changed) if observed else resolve_changed_set(reported=changed)


def test_forbidden_path_fails_the_check() -> None:
    outcome = evaluate_diff_policy(DiffPolicyConfig(forbidden=("infra/**",)), _resolved("src/a.py", "infra/db.py"))
    assert not outcome.passed
    assert outcome.forbidden_matches == ("infra/db.py",)


def test_deny_glob_fails_the_check() -> None:
    outcome = evaluate_diff_policy(DiffPolicyConfig(deny=("**/*.secret",)), _resolved("src/a.py", "creds.secret"))
    assert not outcome.passed


def test_allow_list_rejects_paths_outside_it() -> None:
    outcome = evaluate_diff_policy(DiffPolicyConfig(allow=("src/**",)), _resolved("src/a.py", "docs/x.md"))
    assert not outcome.passed
    assert outcome.disallowed_matches == ("docs/x.md",)


def test_clean_set_passes() -> None:
    outcome = evaluate_diff_policy(DiffPolicyConfig(allow=("src/**",)), _resolved("src/a.py", "src/b.py"))
    assert outcome.passed
    assert outcome.evaluated_count == 2


def test_evaluate_diff_policy_accepts_a_mapping_config() -> None:
    outcome = evaluate_diff_policy({"forbidden": ["infra/**"]}, _resolved("src/a.py", "infra/db.py"))
    assert not outcome.passed
    assert outcome.forbidden_matches == ("infra/db.py",)


def test_executable_risk_is_surfaced_but_passes_by_default() -> None:
    outcome = evaluate_diff_policy(DiffPolicyConfig(), _resolved("src/a.py", ".github/workflows/ci.yml"))
    assert outcome.passed
    assert outcome.risk_inputs == (".github/workflows/ci.yml",)


def test_executable_risk_fails_when_policy_says_so() -> None:
    outcome = evaluate_diff_policy(
        DiffPolicyConfig(agent_config_is_violation=True),
        _resolved("CLAUDE.md"),
    )
    assert not outcome.passed
    assert outcome.risk_inputs == ("CLAUDE.md",)


def test_risk_classification_can_be_disabled() -> None:
    outcome = evaluate_diff_policy(DiffPolicyConfig(flag_agent_config=False), _resolved("CLAUDE.md"))
    assert not outcome.risk_inputs
    assert outcome.passed


def test_outcome_records_reported_provenance_on_fallback() -> None:
    outcome = evaluate_diff_policy(DiffPolicyConfig(), _resolved("a.py", observed=False))
    assert outcome.evaluated_provenance == PROVENANCE_REPORTED
    assert "provenance=reported" in outcome.detail


def test_outcome_evaluates_the_observed_set_not_the_reported() -> None:
    observed = ChangedSet.observed(["infra/secret.py"])
    reported = ChangedSet.reported(["src/a.py"])
    resolved = resolve_changed_set(observed=observed, reported=reported)
    outcome = evaluate_diff_policy(DiffPolicyConfig(forbidden=("infra/**",)), resolved)
    assert not outcome.passed
    assert outcome.evaluated_provenance == PROVENANCE_OBSERVED


# ---------------------------------------------------------------------------
# Evidence surfacing
# ---------------------------------------------------------------------------


def test_evidence_values_record_both_sets_and_provenance() -> None:
    observed = ChangedSet.observed(["a.py", "b.py"])
    reported = ChangedSet.reported(["a.py", "c.py"])
    resolved = resolve_changed_set(observed=observed, reported=reported)
    values = changed_set_evidence_values(resolved)
    assert values["changed_set_observed"] == ["a.py", "b.py"]
    assert values["changed_set_reported"] == ["a.py", "c.py"]
    assert values["changed_set_evaluated"] == ["a.py", "b.py"]
    assert values["changed_set_evaluated_provenance"] == PROVENANCE_OBSERVED
    assert values["changed_set_drift"] == ["b.py", "c.py"]


def test_evidence_values_mark_absent_observed_as_none() -> None:
    resolved = resolve_changed_set(reported=ChangedSet.reported(["a.py"]))
    values = changed_set_evidence_values(resolved)
    assert values["changed_set_observed"] is None
    assert values["changed_set_evaluated_provenance"] == PROVENANCE_REPORTED
    assert values["changed_set_policy_passed"] is None


def test_evidence_values_include_policy_outcome_when_present() -> None:
    resolved = resolve_changed_set(observed=ChangedSet.observed(["infra/x.py"]))
    outcome = evaluate_diff_policy(DiffPolicyConfig(forbidden=("infra/**",)), resolved)
    values = changed_set_evidence_values(resolved, outcome)
    assert values["changed_set_policy_passed"] is False


def test_record_changed_set_evidence_appends_authorised_rows() -> None:
    session = _RecordingSession()
    resolved = resolve_changed_set(observed=ChangedSet.observed(["a.py"]))
    rows = record_changed_set_evidence(session, organisation_id=uuid4(), run_id=uuid4(), resolved=resolved)
    assert len(rows) == len(session.rows)
    assert rows
    for row in rows:
        assert row.key.startswith(CHANGED_SET_EVIDENCE_PREFIX)
        assert row.producer_type == "derived"
        assert row.subject_type == "run"


def test_derived_producer_is_authorised_for_changed_set_keys() -> None:
    assert_write_authorisation("derived", "changed_set_evaluated")


def test_derived_producer_is_not_authorised_for_foreign_keys() -> None:
    with pytest.raises(EvidenceWriteAuthorisationError):
        assert_write_authorisation("derived", "eval_something")
