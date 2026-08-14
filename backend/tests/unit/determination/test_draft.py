"""Unit tests for pipeline draft generation from determination data."""

from modulo.connectors.base import ConnectorType
from modulo.determination.draft import DraftEdge, generate_draft
from modulo.determination.inference import infer

from .helpers import iso_days_ago, make_finding, make_sample


def test_empty_data_returns_empty_draft() -> None:
    draft = generate_draft([], [])
    assert not draft.nodes
    assert not draft.edges
    assert not draft.automation_suggestions
    assert not draft.findings


def test_only_findings_no_samples_returns_empty() -> None:
    findings = [make_finding("overview", "No SDLC stages detected")]
    draft = generate_draft([], findings)
    assert not draft.nodes
    assert not draft.edges
    assert not draft.automation_suggestions
    assert draft.findings == findings


def test_repos_create_start_end_nodes() -> None:
    samples = [make_sample("repos", [{"name": "repo-a"}, {"name": "repo-b"}])]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    node_ids = {n.id for n in draft.nodes}
    assert node_ids == {"start", "development", "end"}
    dev = next(n for n in draft.nodes if n.id == "development")
    assert dev.node_type == "agent"
    assert draft.edges == [DraftEdge(source="development", target="end")]


def test_planning_and_development_creates_hitl_gate() -> None:
    samples = [
        make_sample(
            "issues",
            [{"fields": {"status": {"name": "Backlog"}, "summary": "T1"}}],
            connector_type=ConnectorType.JIRA,
        ),
        make_sample("repos", [{"name": "repo-a"}]),
    ]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    edge = next((e for e in draft.edges if e.hitl_gate), None)
    assert edge is not None
    assert edge.source == "planning"
    assert edge.target == "development"
    assert edge.edge_type == "normal"


def test_full_sdlc_creates_all_stages() -> None:
    samples = [
        make_sample(
            "issues",
            [{"fields": {"status": {"name": "Backlog"}, "summary": "T1"}}],
            connector_type=ConnectorType.JIRA,
        ),
        make_sample("repos", [{"name": "repo-a", "description": "uses .github/workflows"}]),
        make_sample("pulls", [{"number": 1, "created_at": iso_days_ago(2)}]),
    ]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    node_ids = {n.id for n in draft.nodes}
    assert "planning" in node_ids
    assert "development" in node_ids
    assert "review" in node_ids
    assert "ci_cd" in node_ids
    assert {n.node_type for n in draft.nodes} == {"placeholder", "manual", "agent"}


def test_automation_suggestions_included() -> None:
    samples = [
        make_sample(
            "issues",
            [{"fields": {"status": {"name": "Backlog"}, "summary": "T1"}}],
            connector_type=ConnectorType.JIRA,
        ),
        make_sample("repos", [{"name": "repo-a"}]),
        make_sample("pulls", [{"number": 1, "created_at": iso_days_ago(2)}]),
    ]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    planning = next((s for s in draft.automation_suggestions if s["stage"] == "planning"), None)
    assert planning is not None
    assert planning["connector_type"] == "jira"
    assert "Auto-assign issues" in planning["suggestion"]
    review = next((s for s in draft.automation_suggestions if s["stage"] == "review"), None)
    assert review is not None
    assert review["connector_type"] == "github"
    assert "Auto-request reviews" in review["suggestion"]


def test_review_only_draft_connects_from_start() -> None:
    samples = [make_sample("pulls", [{"number": 1, "created_at": iso_days_ago(2)}])]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    node_ids = {n.id for n in draft.nodes}
    assert node_ids == {"start", "review", "end"}
    # No CI/CD finding is present, so a ci_cd node must not be created
    assert "ci_cd" not in node_ids
    review = next((n for n in draft.nodes if n.id == "review"), None)
    assert review is not None
    assert review.node_type == "agent"
    edge = next((e for e in draft.edges if e.target == "review"), None)
    assert edge is not None
    assert edge.source == "start"
    assert edge.hitl_gate is True
    # Review must not be wired to itself when no prior stage exists
    assert not any(e.source == "review" and e.target == "review" for e in draft.edges)


def test_ci_cd_node_connected_after_development() -> None:
    samples = [make_sample("repos", [{"name": "repo-a", "description": "uses .github/workflows"}])]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    node_ids = {n.id for n in draft.nodes}
    assert "development" in node_ids
    assert "ci_cd" in node_ids
    edge = next((e for e in draft.edges if e.target == "ci_cd"), None)
    assert edge is not None
    assert edge.source == "development"
    assert edge.hitl_gate is False


def test_development_node_has_required_capabilities() -> None:
    samples = [make_sample("repos", [{"name": "repo-a"}])]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    dev = next((n for n in draft.nodes if n.id == "development"), None)
    assert dev is not None
    assert dev.connector_type == "github"
    assert dev.required_capabilities == ["read", "write"]


def test_development_node_uses_gitlab_connector() -> None:
    samples = [make_sample("projects", [{"name": "proj-1"}], connector_type=ConnectorType.GITLAB)]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    dev = next((n for n in draft.nodes if n.id == "development"), None)
    assert dev is not None
    assert dev.connector_type == "gitlab"


def test_planning_suggestion_uses_linear_when_no_jira() -> None:
    samples = [
        make_sample(
            "issues",
            [{"state": {"name": "Todo"}, "title": "T1"}],
            connector_type=ConnectorType.LINEAR,
        )
    ]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    planning = next((s for s in draft.automation_suggestions if s["stage"] == "planning"), None)
    assert planning is not None
    assert planning["connector_type"] == "linear"


def test_data_without_detectable_stages_falls_back_to_start_end_edge() -> None:
    samples = [
        make_sample(
            "issues",
            [{"fields": {"status": {"name": "Done"}, "summary": "T1"}}],
            connector_type=ConnectorType.JIRA,
        )
    ]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    assert {n.id for n in draft.nodes} == {"start", "end"}
    assert draft.edges == [DraftEdge(source="start", target="end")]


def test_draft_preserves_findings() -> None:
    samples = [make_sample("repos", [{"name": "repo-a"}])]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    assert draft.findings
    assert any(f.category == "overview" for f in draft.findings)


def test_placeholder_nodes_have_correct_types() -> None:
    samples = [make_sample("repos", [{"name": "repo"}]), make_sample("pulls", [{"number": 1}])]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    for n in draft.nodes:
        if n.id in ("start", "end"):
            assert n.node_type == "placeholder", f"{n.id} should be placeholder"
    # Review is an agent node
    review = next((n for n in draft.nodes if n.id == "review"), None)
    assert review is not None
    assert review.node_type == "agent"


def test_planning_to_ci_without_development() -> None:
    """CI/CD must wire to the preceding planning stage when no development stage exists."""
    samples = [
        make_sample(
            "issues",
            [{"fields": {"status": {"name": "Backlog"}, "summary": "T1"}}],
            connector_type=ConnectorType.JIRA,
        ),
        # A description-only repo record triggers CI detection without a development stage
        make_sample("repos", [{"description": "uses .github/workflows"}]),
    ]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    node_ids = {n.id for n in draft.nodes}
    assert node_ids == {"start", "planning", "ci_cd", "end"}
    assert "development" not in node_ids
    edge = next((e for e in draft.edges if e.target == "ci_cd"), None)
    assert edge is not None
    assert edge.source == "planning"
    assert edge.hitl_gate is False


def test_review_sourced_from_planning_when_no_development() -> None:
    """A review stage must fall back to the last prior stage when development is absent."""
    samples = [
        make_sample(
            "issues",
            [{"fields": {"status": {"name": "Backlog"}, "summary": "T1"}}],
            connector_type=ConnectorType.JIRA,
        ),
        make_sample("pulls", [{"number": 1, "created_at": iso_days_ago(2)}]),
    ]
    findings = infer(samples)
    draft = generate_draft(samples, findings)
    node_ids = {n.id for n in draft.nodes}
    assert node_ids == {"start", "planning", "review", "end"}
    assert "development" not in node_ids
    edge = next((e for e in draft.edges if e.target == "review"), None)
    assert edge is not None
    assert edge.source == "planning"
    assert edge.hitl_gate is True
    assert not any(e.source == "review" and e.target == "review" for e in draft.edges)
