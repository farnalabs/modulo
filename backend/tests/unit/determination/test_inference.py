"""Unit tests for the Determination Inference Engine."""

import uuid

from modulo.connectors.base import ConnectorType
from modulo.determination.inference import Finding, infer
from modulo.determination.scanner import ScanSample


def _sample(
    resource: str,
    records: list[dict],
    connector_type: ConnectorType = ConnectorType.GITHUB,
    error: str | None = None,
) -> ScanSample:
    return ScanSample(
        connector_id=uuid.uuid4(),
        connector_type=connector_type,
        resource=resource,
        records=records,
        sample_count=len(records),
        error=error,
    )


def test_empty_samples_returns_findings():
    findings = infer([])
    assert len(findings) >= 1
    # Should indicate no data was available
    categories = {f.category for f in findings}
    assert "automation" in categories


def test_repos_detect_development_stage():
    samples = [
        _sample("repos", [{"name": "backend"}, {"name": "frontend"}])
    ]
    findings = infer(samples)
    stages = [f for f in findings if f.category == "stage" and "Development" in f.finding]
    assert len(stages) >= 1
    assert "2 repositories" in stages[0].evidence


def test_pull_requests_detect_code_review():
    samples = [
        _sample("pulls", [{"number": 1, "created_at": "2026-06-20T00:00:00Z"}])
    ]
    findings = infer(samples)
    review = [f for f in findings if f.category == "stage" and "Code review" in f.finding]
    assert len(review) == 1


def test_stale_pr_bottleneck():
    old_date = "2026-06-01T00:00:00Z"
    recent_date = "2026-06-22T00:00:00Z"
    samples = [
        _sample("pulls", [
            {"number": 1, "created_at": old_date},
            {"number": 2, "created_at": recent_date},
        ])
    ]
    findings = infer(samples)
    bottlenecks = [f for f in findings if f.category == "bottleneck"]
    assert len(bottlenecks) >= 1
    assert "bottleneck" in bottlenecks[0].finding.lower()


def test_planning_stage_from_jira_issues():
    samples = [
        _sample(
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


def test_planning_stage_from_linear_issues():
    samples = [
        _sample(
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


def test_issue_lifecycle_transition():
    samples = [
        _sample(
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
    assert len(transitions) >= 1
    assert "Issue lifecycle" in transitions[0].finding


def test_confidence_levels_present():
    samples = [
        _sample("repos", [{"name": "repo"}]),
        _sample("pulls", [{"number": 1, "created_at": "2026-06-22T00:00:00Z"}]),
    ]
    findings = infer(samples)
    confidences = {f.confidence for f in findings}
    assert confidences.issubset({"high", "medium", "low"})


def test_each_finding_has_evidence():
    samples = [
        _sample("repos", [{"name": "repo"}]),
        _sample("issues", [
            {"fields": {"status": {"name": "Backlog"}}}
        ], connector_type=ConnectorType.JIRA),
    ]
    findings = infer(samples)
    for f in findings:
        assert f.evidence, f"Finding '{f.finding}' has no evidence"
        assert f.category, "Finding has no category"


def test_linear_search_results():
    samples = [
        _sample(
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


def test_finding_model():
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


def test_related_connector_in_finding():
    cid = uuid.uuid4()
    f = Finding(
        category="stage",
        finding="Test",
        evidence="Ev",
        confidence="low",
        related_connector=cid,
    )
    assert f.related_connector == cid
