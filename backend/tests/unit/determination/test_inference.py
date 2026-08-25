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


def test_repos_detect_development_stage() -> None:
    samples = [make_sample("repos", [{"name": "backend"}, {"name": "frontend"}])]
    findings = infer(samples)
    stages = [f for f in findings if f.category == "stage" and "Development" in f.finding]
    assert len(stages) == 1
    assert "2 repositories" in stages[0].evidence
    assert stages[0].confidence == "high"


def test_projects_detect_development_stage() -> None:
    samples = [make_sample("projects", [{"name": "proj-1"}, {"name": "proj-2"}], connector_type=ConnectorType.GITLAB)]
    findings = infer(samples)
    stages = [f for f in findings if f.category == "stage" and "Development" in f.finding]
    assert len(stages) == 1
    assert "2 repositories" in stages[0].evidence


def test_full_name_repo_records_detect_development_stage() -> None:
    samples = [make_sample("repos", [{"full_name": "owner/repo1"}])]
    findings = infer(samples)
    stages = [f for f in findings if f.category == "stage" and "Development" in f.finding]
    assert len(stages) == 1
    assert "1 repository" in stages[0].evidence


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


def test_invalid_pr_date_ignored() -> None:
    samples = [make_sample("pulls", [{"number": 1, "created_at": "not-a-date"}])]
    findings = infer(samples)
    assert not any(f.category == "bottleneck" for f in findings)
    assert any(f.category == "stage" and "Code review" in f.finding for f in findings)


def test_pr_camelcase_created_at_detects_stale_bottleneck() -> None:
    """Inference must read the camelCase ``createdAt`` field when ``created_at`` is absent."""
    samples = [make_sample("pulls", [{"number": 1, "createdAt": iso_days_ago(10)}])]
    findings = infer(samples)
    bottleneck = next((f for f in findings if f.category == "bottleneck"), None)
    assert bottleneck is not None
    assert "Potential review bottleneck: 1 PRs/MRs open for >5 days" in bottleneck.finding


def test_naive_created_at_without_timezone_does_not_crash() -> None:
    """A timezone-less ISO timestamp must be interpreted as UTC, never crash inference.

    datetime.fromisoformat('2026-06-20T00:00:00') yields a naive datetime;
    subtracting an aware datetime from it would raise TypeError.
    """
    samples = [make_sample("pulls", [{"number": 1, "created_at": iso_days_ago(10)[:-1]}])]
    findings = infer(samples)
    bottleneck = next((f for f in findings if f.category == "bottleneck"), None)
    assert bottleneck is not None
    assert "Potential review bottleneck" in bottleneck.finding


def test_date_only_created_at_does_not_crash() -> None:
    """A bare date (parsed as naive midnight UTC) must be handled gracefully."""
    date_only = iso_days_ago(10).split("T")[0]
    samples = [make_sample("pulls", [{"number": 1, "created_at": date_only}])]
    findings = infer(samples)
    assert any(f.category == "stage" and "Code review" in f.finding for f in findings)
    bottleneck = next((f for f in findings if f.category == "bottleneck"), None)
    assert bottleneck is not None
    assert "Potential review bottleneck" in bottleneck.finding


def test_non_string_created_at_is_ignored() -> None:
    """Non-string created_at values must not crash or produce a bottleneck finding."""
    samples = [make_sample("pulls", [{"number": 1, "created_at": 12345}])]
    findings = infer(samples)
    assert any(f.category == "stage" and "Code review" in f.finding for f in findings)
    assert not any(f.category == "bottleneck" for f in findings)


def test_pr_camelcase_created_at_recent_no_stale_bottleneck() -> None:
    samples = [make_sample("pulls", [{"number": 1, "createdAt": iso_days_ago(1)}])]
    findings = infer(samples)
    stale = [f for f in findings if f.category == "bottleneck" and "Potential review bottleneck" in f.finding]
    assert stale == []
    no_stale = [f for f in findings if f.category == "bottleneck" and "No stale PRs" in f.finding]
    assert len(no_stale) == 1


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


def test_ci_detected_from_repo_name() -> None:
    samples = [make_sample("repos", [{"name": "azure-pipelines"}])]
    findings = infer(samples)
    ci = [f for f in findings if f.category == "automation" and "CI/CD configuration detected" in f.finding]
    assert len(ci) == 1


def test_ci_detected_from_description_without_name() -> None:
    """CI detection must not require an identifier: a bare description hint suffices."""
    samples = [make_sample("repos", [{"description": "deploys via .github/workflows"}])]
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


def test_finding_model() -> None:
    f = Finding(
        category="stage",
        finding="Test finding",
        evidence="Some evidence",
        confidence="high",
        uncertainty="Some uncertainty",
    )
    assert f.category == "stage"
    assert f.confidence == "high"
    assert f.uncertainty == "Some uncertainty"


def test_finding_invalid_confidence_raises() -> None:
    with pytest.raises(ValueError, match="confidence must be one of"):
        Finding(category="stage", finding="Test", evidence="Ev", confidence="very-sure")


def test_related_connector_in_finding() -> None:
    cid = uuid.uuid4()
    f = Finding(
        category="stage",
        finding="Test",
        evidence="Ev",
        confidence="low",
        related_connector=cid,
    )
    assert f.related_connector == cid
