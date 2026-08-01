"""Unit tests for the Determination Inference Engine."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from modulo.connectors.base import ConnectorType
from modulo.determination.inference import Finding, infer

from .helpers import make_sample


def _iso(delta_days: float) -> str:
    """Return an ISO-8601 timestamp that many days in the past (deterministic across run dates)."""
    return (datetime.now(UTC) - timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def test_pull_requests_detect_code_review() -> None:
    samples = [make_sample("pulls", [{"number": 1, "created_at": _iso(2)}])]
    findings = infer(samples)
    review = [f for f in findings if f.category == "stage" and "Code review" in f.finding]
    assert len(review) == 1
    assert "1 open PRs/MRs" in review[0].evidence


def test_stale_pr_bottleneck() -> None:
    samples = [
        make_sample(
            "pulls",
            [
                {"number": 1, "created_at": _iso(10)},
                {"number": 2, "created_at": _iso(2)},
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
                {"number": 1, "created_at": _iso(2)},
                {"number": 2, "created_at": _iso(0.5)},
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


def test_planning_stage_from_linear_issues() -> None:
    samples = [
        make_sample(
            "issues",
            records=[
                {"state": {"name": "Todo"}, "title": "Task 1"},
                {"state": {"name": "Done"}, "title": "Task 2"},
            ],
            connector_type=ConnectorType.LINEAR,
        )
    ]
    findings = infer(samples)
    planning = [f for f in findings if f.category == "stage" and "Planning" in f.finding]
    assert len(planning) == 1


def test_planning_stage_from_linear_plain_string_state() -> None:
    samples = [
        make_sample(
            "issues",
            records=[
                {"state": "Todo", "title": "Task 1"},
                {"state": "Done", "title": "Task 2"},
            ],
            connector_type=ConnectorType.LINEAR,
        )
    ]
    findings = infer(samples)
    planning = [f for f in findings if f.category == "stage" and "Planning" in f.finding]
    assert len(planning) == 1


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


def test_confidence_levels_present() -> None:
    samples = [
        make_sample("repos", [{"name": "repo"}]),
        make_sample("pulls", [{"number": 1, "created_at": _iso(2)}]),
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


def test_linear_search_results() -> None:
    samples = [
        make_sample(
            "issues",
            records=[
                {"state": {"name": "Todo"}, "title": "Bug fix"},
                {"state": {"name": "In Progress"}, "title": "Feature work"},
            ],
            connector_type=ConnectorType.LINEAR,
        )
    ]
    findings = infer(samples)
    stages = {f.finding for f in findings if f.category == "stage"}
    assert any("Planning" in s for s in stages)


def test_error_samples_do_not_crash_inference() -> None:
    samples = [make_sample("repos", [], error="GitHub API HTTP 500: boom")]
    findings = infer(samples)
    categories = {f.category for f in findings}
    assert categories == {"automation", "overview"}


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
    with pytest.raises(ValueError):
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
