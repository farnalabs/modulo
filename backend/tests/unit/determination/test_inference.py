"""Unit tests for the Determination Inference Engine."""

import uuid

import pytest

from modulo.connectors.base import ConnectorType
from modulo.determination.inference import Finding, infer

from .helpers import iso_days_ago, make_sample


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


def test_pull_requests_detect_code_review() -> None:
    samples = [make_sample("pulls", [{"number": 1, "created_at": iso_days_ago(2)}])]
    findings = infer(samples)
    review = [f for f in findings if f.category == "stage" and "Code review" in f.finding]
    assert len(review) == 1
    assert "1 open PRs/MRs" in review[0].evidence


def test_mrs_detect_code_review_stage() -> None:
    samples = [make_sample("mrs", [{"title": "MR 1"}], connector_type=ConnectorType.GITLAB)]
    findings = infer(samples)
    review = [f for f in findings if f.category == "stage" and "Code review" in f.finding]
    assert len(review) == 1
    assert "1 open PRs/MRs" in review[0].evidence


def test_stale_pr_bottleneck() -> None:
    samples = [
        make_sample(
            "pulls",
            [
                {"number": 1, "created_at": iso_days_ago(10)},
                {"number": 2, "created_at": iso_days_ago(2)},
            ],
        )
    ]
    findings = infer(samples)
    bottlenecks = [f for f in findings if f.category == "bottleneck"]
    assert len(bottlenecks) == 1
    assert "Potential review bottleneck: 1 PRs/MRs open for >5 days without merge" in bottlenecks[0].finding
    assert "1/2 open for >5 days" in bottlenecks[0].evidence


def test_no_stale_prs_when_all_recent() -> None:
    samples = [
        make_sample(
            "pulls",
            [
                {"number": 1, "created_at": iso_days_ago(2)},
                {"number": 2, "created_at": iso_days_ago(0.5)},
            ],
        )
    ]
    findings = infer(samples)
    no_stale = [f for f in findings if f.category == "bottleneck" and "No stale PRs" in f.finding]
    assert len(no_stale) == 1
    assert "0/2 open for >5 days" in no_stale[0].evidence
    assert no_stale[0].confidence == "low"


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
    samples = [
        make_sample(
            "issues",
            records=[
                {
                    "fields": {
                        "status": {"name": "Backlog"},
                        "summary": "Task 1",
                    }
                },
                {
                    "fields": {
                        "status": {"name": "In Progress"},
                        "summary": "Task 2",
                    }
                },
            ],
            connector_type=ConnectorType.JIRA,
        )
    ]
    findings = infer(samples)
    planning = [f for f in findings if f.category == "stage" and "Planning" in f.finding]
    assert len(planning) == 1
    assert "1 issues in planning statuses" in planning[0].evidence


def test_issue_lifecycle_transition() -> None:
    samples = [
        make_sample(
            "issues",
            records=[
                {"fields": {"status": {"name": "Backlog"}, "summary": "T1"}},
                {"fields": {"status": {"name": "In Progress"}, "summary": "T2"}},
                {"fields": {"status": {"name": "Done"}, "summary": "T3"}},
            ],
            connector_type=ConnectorType.JIRA,
        )
    ]
    findings = infer(samples)
    transitions = [f for f in findings if f.category == "transition"]
    assert len(transitions) == 1
    assert "Issue lifecycle" in transitions[0].finding


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


def test_confidence_levels_present() -> None:
    samples = [
        make_sample("repos", [{"name": "repo"}]),
        make_sample("pulls", [{"number": 1, "created_at": iso_days_ago(2)}]),
    ]
    findings = infer(samples)
    confidences = {f.confidence for f in findings}
    assert confidences == {"high", "medium", "low"}


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


def test_unknown_resource_samples_are_ignored() -> None:
    """Samples for resources outside the recognised set must be skipped without crashing."""
    samples = [
        make_sample("unknown_resource", [{"x": 1}]),
        make_sample("repos", [{"name": "repo-a"}]),
    ]
    findings = infer(samples)
    assert any(f.category == "stage" and "Development" in f.finding for f in findings)
    assert "1 repository" in next(f for f in findings if f.category == "stage").evidence


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
