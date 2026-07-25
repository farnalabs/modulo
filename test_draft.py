"""Unit tests for pipeline draft generation from determination data."""

import uuid

from modulo.connectors.base import ConnectorType
from modulo.determination.draft import generate_draft
from modulo.determination.inference import Finding, infer
from modulo.determination.scanner import ScanSample


def _sample(
    resource: str,
    records: list[dict],
    connector_type: ConnectorType = ConnectorType.GITHUB,
) -> ScanSample:
    return ScanSample(
        connector_id=uuid.uuid4(),
        connector_type=connector_type,
        resource=resource,
        records=records,
        sample_count=len(records),
    )


def _finding(
    category: str,
    finding: str,
    confidence: str = "high",
) -> Finding:
    return Finding(category=category, finding=finding, evidence="test", confidence=confidence)


def test_empty_data_returns_empty_draft():
    draft = generate_draft([], [])
    assert len(draft.nodes) == 0
    assert len(draft.edges) == 0


def test_only_findings_no_samples_returns_empty():
    findings = [_finding("overview", "No SDLC stages detected")]
    draft = generate_draft([], findings)
    assert len(draft.nodes) == 0


def test_repos_create_start_end_nodes():
    samples = [_sample("repos", [{"name": "repo-a"}, {"name": "repo-b"}])]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    node_ids = {n.id for n in draft.nodes}
    assert "start" in node_ids
    assert "end" in node_ids
    assert "development" in node_ids


def test_planning_and_development_creates_hitl_gate():
    samples = [
        _sample(
            "issues",
            [{"fields": {"status": {"name": "Backlog"}, "summary": "T1"}}],
            connector_type=ConnectorType.JIRA,
        ),
        _sample("repos", [{"name": "repo-a"}]),
    ]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    edge = next((e for e in draft.edges if e.hitl_gate), None)
    assert edge is not None
    assert edge.source == "planning"
    assert edge.target == "development"


def test_full_sdlc_creates_all_stages():
    samples = [
        _sample(
            "issues",
            [{"fields": {"status": {"name": "Backlog"}, "summary": "T1"}}],
            connector_type=ConnectorType.JIRA,
        ),
        _sample("repos", [{"name": "repo-a", "description": "uses .github/workflows"}]),
        _sample("pulls", [{"number": 1, "created_at": "2026-06-20T00:00:00Z"}]),
    ]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    node_ids = {n.id for n in draft.nodes}
    assert "planning" in node_ids
    assert "development" in node_ids
    assert "review" in node_ids
    assert "ci_cd" in node_ids


def test_automation_suggestions_included():
    samples = [
        _sample(
            "issues",
            [{"fields": {"status": {"name": "Backlog"}, "summary": "T1"}}],
            connector_type=ConnectorType.JIRA,
        ),
        _sample("repos", [{"name": "repo-a"}]),
        _sample("pulls", [{"number": 1, "created_at": "2026-06-20T00:00:00Z"}]),
    ]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    assert len(draft.automation_suggestions) >= 1
    for s in draft.automation_suggestions:
        assert "stage" in s
        assert "suggestion" in s


def test_development_node_has_required_capabilities():
    samples = [_sample("repos", [{"name": "repo-a"}])]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    dev = next((n for n in draft.nodes if n.id == "development"), None)
    assert dev is not None
    assert dev.connector_type is not None
    assert len(dev.required_capabilities) > 0


def test_draft_preserves_findings():
    samples = [_sample("repos", [{"name": "repo-a"}])]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    assert len(draft.findings) > 0
    assert any(f.category == "overview" for f in draft.findings)


def test_placeholder_nodes_have_correct_types():
    samples = [_sample("repos", [{"name": "repo"}]), _sample("pulls", [{"number": 1}])]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    for n in draft.nodes:
        if n.id in ("start", "end"):
            assert n.node_type == "placeholder", f"{n.id} should be placeholder"
    # Review is an agent node
    review = next((n for n in draft.nodes if n.id == "review"), None)
    assert review is not None
    assert review.node_type == "agent"
