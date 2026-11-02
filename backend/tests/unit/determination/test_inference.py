"""Unit tests for the Determination Inference Engine."""

import uuid

import pytest

from modulo.connectors.base import ConnectorType
from modulo.determination.inference import Finding, infer
from modulo.determination.scanner import ScanSample

from .helpers import iso_days_ago, issues_sample, make_sample


def test_empty_samples_returns_findings() -> None:
    findings = infer([])
    categories = {f.category for f in findings}
    assert categories == {"automation", "overview"}
    assert any(f.category == "automation" and "No CI/CD" in f.finding for f in findings)
    assert any(f.category == "overview" and "No SDLC stages" in f.finding for f in findings)


@pytest.mark.parametrize(
    ("resource", "records", "connector_type", "expected_evidence"),
    [
        pytest.param(
            "repos",
            [{"name": "backend"}, {"name": "frontend"}],
            ConnectorType.GITHUB,
            "2 repositories",
            id="github-repos-plain-name",
        ),
        pytest.param(
            "projects",
            [{"name": "proj-1"}, {"name": "proj-2"}],
            ConnectorType.GITLAB,
            "2 repositories",
            id="gitlab-projects-plain-name",
        ),
        pytest.param(
            "repos",
            [{"full_name": "owner/repo1"}],
            ConnectorType.GITHUB,
            "1 repository",
            id="github-repos-full-name",
        ),
    ],
)
def test_repo_family_records_detect_development_stage(
    resource: str,
    records: list[dict],
    connector_type: ConnectorType,
    expected_evidence: str,
) -> None:
    """Any repo/project record shape yields exactly one high-confidence development finding."""
    samples = [make_sample(resource, records, connector_type=connector_type)]
    findings = infer(samples)
    stages = [f for f in findings if f.category == "stage" and "Development" in f.finding]
    assert len(stages) == 1
    assert expected_evidence in stages[0].evidence
    assert stages[0].confidence == "high"


@pytest.mark.parametrize(
    ("resource", "records", "connector_type"),
    [
        pytest.param(
            "pulls",
            [{"number": 1, "created_at": iso_days_ago(2)}],
            ConnectorType.GITHUB,
            id="github-pulls",
        ),
        pytest.param(
            "mrs",
            [{"title": "MR 1"}],
            ConnectorType.GITLAB,
            id="gitlab-mrs",
        ),
    ],
)
def test_open_pull_requests_detect_code_review_stage(
    resource: str,
    records: list[dict],
    connector_type: ConnectorType,
) -> None:
    """Both GitHub PRs and GitLab MRs count as one open code-review item."""
    findings = infer([make_sample(resource, records, connector_type=connector_type)])
    review = [f for f in findings if f.category == "stage" and "Code review" in f.finding]
    assert len(review) == 1
    assert "1 open PRs/MRs" in review[0].evidence


@pytest.mark.parametrize(
    ("ages_days", "expected_finding", "expected_stale_fraction", "expected_confidence"),
    [
        pytest.param([10, 2], "Potential review bottleneck", "1/2 open for >5 days", "medium", id="one-stale-pr"),
        pytest.param([2, 0.5], "No stale PRs", "0/2 open for >5 days", "low", id="all-recent-prs"),
    ],
)
def test_stale_pr_summary_reports_evidence_and_confidence(
    ages_days: list[float],
    expected_finding: str,
    expected_stale_fraction: str,
    expected_confidence: str,
) -> None:
    """The bottleneck finding carries the stale fraction and matches its confidence to the outcome."""
    records = [{"number": index, "created_at": iso_days_ago(age)} for index, age in enumerate(ages_days, start=1)]
    findings = infer([make_sample("pulls", records)])
    bottlenecks = [f for f in findings if f.category == "bottleneck"]
    assert len(bottlenecks) == 1
    assert expected_finding in bottlenecks[0].finding
    assert expected_stale_fraction in bottlenecks[0].evidence
    assert bottlenecks[0].confidence == expected_confidence


_STALE_BOTTLENECK = "Potential review bottleneck"
_NO_STALE = "No stale PRs"


@pytest.mark.parametrize(
    ("record", "expected_bottleneck"),
    [
        pytest.param({"number": 1, "created_at": iso_days_ago(10)}, _STALE_BOTTLENECK, id="stale-snake-case-10-days"),
        pytest.param({"number": 1, "createdAt": iso_days_ago(10)}, _STALE_BOTTLENECK, id="stale-camelcase-10-days"),
        pytest.param(
            {"number": 1, "created_at": iso_days_ago(10)[:-1]},
            _STALE_BOTTLENECK,
            id="stale-naive-timestamp-no-timezone",
        ),
        pytest.param(
            {"number": 1, "created_at": iso_days_ago(10).split("T")[0]},
            _STALE_BOTTLENECK,
            id="stale-date-only",
        ),
        pytest.param({"number": 1, "created_at": iso_days_ago(2)}, _NO_STALE, id="recent-two-days"),
        pytest.param({"number": 1, "createdAt": iso_days_ago(1)}, _NO_STALE, id="recent-camelcase-one-day"),
        pytest.param({"number": 1, "created_at": "not-a-date"}, None, id="unparseable-timestamp-dropped"),
        pytest.param({"number": 1, "created_at": 12345}, None, id="non-string-timestamp-dropped"),
    ],
)
def test_pr_timestamp_spelling_decides_bottleneck_finding(
    record: dict,
    expected_bottleneck: str | None,
) -> None:
    """Every timestamp spelling inference accepts must be classified consistently.

    A PR older than five days yields a bottleneck finding, a recent PR yields the
    "no stale PRs" finding, and an unusable timestamp is dropped so that no
    bottleneck finding is produced at all. The review stage is detected either way.
    """
    findings = infer([make_sample("pulls", [record])])

    assert any(f.category == "stage" and "Code review" in f.finding for f in findings)

    bottlenecks = [f for f in findings if f.category == "bottleneck"]
    if expected_bottleneck is None:
        assert bottlenecks == []
    else:
        assert len(bottlenecks) == 1
        assert expected_bottleneck in bottlenecks[0].finding


def test_planning_stage_from_jira_issues() -> None:
    findings = infer([issues_sample(["Backlog", "In Progress"])])
    planning = [f for f in findings if f.category == "stage" and "Planning" in f.finding]
    assert len(planning) == 1
    # Only the backlog status counts as planning; "In Progress" is a transition.
    assert "1 issues in planning statuses" in planning[0].evidence


def test_issue_lifecycle_transition() -> None:
    findings = infer([issues_sample(["Backlog", "In Progress", "Done"])])
    transitions = [f for f in findings if f.category == "transition"]
    assert len(transitions) == 1
    lifecycle = transitions[0]
    assert lifecycle.confidence == "medium"
    assert "Issue lifecycle" in lifecycle.finding
    # Planning statuses collapse into the synthetic "planning" label; the rest are
    # reported verbatim so the transition line names every observed state.
    assert "planning" in lifecycle.finding
    assert "in progress" in lifecycle.finding
    assert "done" in lifecycle.finding
    assert "backlog" not in lifecycle.finding
    assert "'backlog': 1" in lifecycle.evidence
    assert "'in progress': 1" in lifecycle.evidence
    assert "'done': 1" in lifecycle.evidence


def _repo_record_with_ci_hint(carrier: str, marker: str) -> dict:
    if carrier == "name":
        return {"name": marker}
    return {"name": "repo-a", "description": f"pipeline configured via {marker}"}


@pytest.mark.parametrize("carrier", ["name", "description"])
@pytest.mark.parametrize("marker", [".github/workflows", ".gitlab-ci.yml", "circleci", "azure-pipelines"])
def test_ci_detected_from_repo_metadata(carrier: str, marker: str) -> None:
    """A known CI marker must be recognised in either the repo name or its description."""
    samples = [make_sample("repos", [_repo_record_with_ci_hint(carrier, marker)])]
    findings = infer(samples)
    ci = [f for f in findings if f.category == "automation" and "CI/CD configuration detected" in f.finding]
    assert len(ci) == 1


def test_non_string_repo_identifier_does_not_crash() -> None:
    """Malformed records whose identifiers are not strings must not crash or skew inference."""
    samples = [
        make_sample(
            "repos",
            [
                {"full_name": 12345},
                {"name": ["nested"]},
                {"full_name": "owner/repo"},
            ],
        )
    ]
    findings = infer(samples)
    development = [f for f in findings if f.category == "stage" and "Development" in f.finding]
    assert len(development) == 1
    assert "1 repository" in development[0].evidence


def test_confidence_reflects_evidence_strength() -> None:
    """Observed stages are high confidence; a gap inferred from absent metadata is low."""
    samples = [
        make_sample("repos", [{"name": "repo"}]),
        make_sample("pulls", [{"number": 1, "created_at": iso_days_ago(2)}]),
    ]
    findings = infer(samples)
    development = next(f for f in findings if f.category == "stage" and "Development" in f.finding)
    assert development.confidence == "high"
    # No CI marker in the sampled repo metadata, so the automation finding is a guess.
    automation = next(f for f in findings if f.category == "automation")
    assert automation.confidence == "low"
    assert {f.confidence for f in findings} <= {"high", "medium", "low"}


def test_each_finding_has_evidence() -> None:
    samples = [
        make_sample("repos", [{"name": "repo"}]),
        make_sample("issues", [{"fields": {"status": {"name": "Backlog"}}}], connector_type=ConnectorType.JIRA),
    ]
    findings = infer(samples)
    assert findings, "infer() produced no findings — the loop below would pass vacuously"
    for f in findings:
        assert f.evidence, f"Finding '{f.finding}' has no evidence"
        assert f.category, "Finding has no category"


def test_error_samples_do_not_crash_inference() -> None:
    samples = [make_sample("repos", [], error="GitHub API HTTP 500: boom")]
    findings = infer(samples)
    categories = {f.category for f in findings}
    assert categories == {"automation", "overview"}


def test_issues_with_empty_records_are_tolerated() -> None:
    """An issues sample that carries no records must not crash inference."""
    samples = [
        make_sample("issues", [], connector_type=ConnectorType.JIRA),
        make_sample("issues", [{"summary": "T1"}], connector_type=ConnectorType.JIRA),
    ]
    findings = infer(samples)
    assert not any(f.category == "stage" and "Planning" in f.finding for f in findings)
    assert any(f.category == "overview" for f in findings)


@pytest.mark.parametrize(
    ("record", "expected_planning"),
    [
        pytest.param({"fields": {"status": {"name": "Backlog"}}}, True, id="fields-status-object"),
        pytest.param({"fields": {"status": "Backlog"}}, True, id="fields-status-string"),
        pytest.param({"state": "Backlog"}, True, id="top-level-state-string"),
        pytest.param({"state": {"name": "Ready"}}, True, id="top-level-state-object"),
        pytest.param({"fields": {}, "state": "To Do"}, True, id="empty-fields-falls-back-to-state"),
        pytest.param({"state": "in_progress"}, False, id="top-level-state-non-planning"),
        pytest.param(
            {"fields": {"status": None}, "state": "Done"}, False, id="null-status-falls-back-to-non-planning-state"
        ),
    ],
)
def test_issue_status_is_read_from_fields_or_top_level_state(
    record: dict,
    expected_planning: bool,
) -> None:
    """Planning detection must work for Jira-style ``fields.status`` and plain ``state`` records."""
    findings = infer([make_sample("issues", [record], connector_type=ConnectorType.JIRA)])
    planning = [f for f in findings if f.category == "stage" and "Planning" in f.finding]
    assert bool(planning) is expected_planning
    if expected_planning:
        assert "1 issues in planning statuses" in planning[0].evidence


def test_issue_records_without_any_status_are_skipped() -> None:
    """Issue records that expose neither a status nor a state must be ignored, not crash."""
    samples = [
        make_sample(
            "issues",
            [
                {"summary": "T1"},
                {"id": "T2"},
            ],
            connector_type=ConnectorType.JIRA,
        )
    ]
    findings = infer(samples)
    assert not any(f.category == "stage" and "Planning" in f.finding for f in findings)
    assert not any(f.category == "transition" for f in findings)


@pytest.mark.parametrize(
    ("samples", "expected_stages", "expected_stage_count", "expected_verb"),
    [
        pytest.param(
            [make_sample("repos", [{"name": "repo-a"}])],
            "development",
            1,
            "was",
            id="single-stage",
        ),
        pytest.param(
            [
                make_sample("repos", [{"name": "repo-a"}]),
                make_sample("pulls", [{"number": 1, "created_at": iso_days_ago(2)}]),
            ],
            "development, code review",
            2,
            "were",
            id="two-stages",
        ),
    ],
)
def test_overview_lists_detected_stages(
    samples: list[ScanSample],
    expected_stages: str,
    expected_stage_count: int,
    expected_verb: str,
) -> None:
    """The overview names every detected stage and inflects its verb by stage count."""
    overviews = [f for f in infer(samples) if f.category == "overview"]
    assert len(overviews) == 1
    overview = overviews[0]
    assert overview.confidence == "medium"
    assert overview.finding == f"SDLC stages detected: {expected_stages}"
    assert f"{expected_stage_count} {expected_verb} detected" in overview.evidence


def test_unknown_resource_samples_are_ignored() -> None:
    """Samples for resources outside the recognised set must be skipped without crashing."""
    samples = [
        make_sample("unknown_resource", [{"x": 1}]),
        make_sample("repos", [{"name": "repo-a"}]),
    ]
    findings = infer(samples)
    development = [f for f in findings if f.category == "stage" and "Development" in f.finding]
    assert len(development) == 1
    assert "1 repository" in development[0].evidence


def test_finding_rejects_unknown_confidence() -> None:
    """A confidence outside high/medium/low must fail loudly rather than being stored."""
    with pytest.raises(ValueError, match="confidence must be one of"):
        Finding(category="stage", finding="Test finding", evidence="Some evidence", confidence="certain")


def test_finding_defaults_and_optional_fields() -> None:
    """A minimal Finding carries empty uncertainty and no related connector unless supplied."""
    minimal = Finding(category="stage", finding="Test finding", evidence="Some evidence", confidence="high")
    assert not minimal.uncertainty
    assert minimal.related_connector is None

    cid = uuid.uuid4()
    linked = Finding(
        category="stage",
        finding="Test finding",
        evidence="Some evidence",
        confidence="low",
        uncertainty="Some uncertainty",
        related_connector=cid,
    )
    assert linked.uncertainty == "Some uncertainty"
    assert linked.related_connector == cid
