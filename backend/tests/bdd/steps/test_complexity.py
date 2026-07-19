"""Step definitions for the Complexity Reviewer / determination scanner feature."""

import asyncio
import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import given, scenarios, then, when

from modulo.connectors.base import ConnectorType
from modulo.determination.scanner import ScanSample

# ---------------------------------------------------------------------------
# Register feature file
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/complexity/complexity_reviewer.feature")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


# ===========================================================================
# complexity/complexity_reviewer.feature  —  5 scenarios
# ===========================================================================


@given("a GitHub connector with sample data")
def step_github_connector_samples(ctx):
    """Mock a GitHub connector that returns repo and PR data."""
    from modulo.connectors.base import ConnectorQuery, ConnectorResult

    mock_connector = AsyncMock()
    mock_connector.connector_type = ConnectorType.GITHUB
    mock_connector.health_check = AsyncMock()

    async def mock_query(q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "repos":
                return ConnectorResult(
                    records=[
                        {"full_name": "owner/repo1", "name": "repo1"},
                        {"full_name": "owner/repo2", "name": "repo2"},
                    ],
                    total=2,
                )
            case "pulls":
                return ConnectorResult(
                    records=[
                        {"number": 1, "title": "Fix bug", "state": "open"},
                    ],
                    total=1,
                )
            case _:
                return ConnectorResult(records=[], total=0)

    mock_connector.query = mock_query
    ctx["_mock_connector"] = mock_connector
    ctx["_connector_id"] = uuid.uuid4()


@given("a Jira connector with sample data")
def step_jira_connector_samples(ctx):
    from modulo.connectors.base import ConnectorResult

    mock_connector = AsyncMock()
    mock_connector.connector_type = ConnectorType.JIRA
    mock_connector.health_check = AsyncMock()

    async def mock_query(q):
        return ConnectorResult(
            records=[
                {"id": "10001", "key": "PROJ-1", "fields": {"summary": "Ticket"}},
            ],
            total=1,
        )

    mock_connector.query = mock_query
    ctx["_mock_connector"] = mock_connector
    ctx["_connector_id"] = uuid.uuid4()


@given("scanned samples with planning and development stages")
def step_scanned_samples_planning_dev(ctx):
    """Build mock ScanSample objects representing planning + dev findings."""
    from modulo.determination.inference import Finding

    samples = [
        ScanSample(
            connector_id=uuid.uuid4(),
            connector_type=ConnectorType.GITHUB,
            resource="repos",
            records=[{"full_name": "owner/repo1"}],
            sample_count=1,
        ),
        ScanSample(
            connector_id=uuid.uuid4(),
            connector_type=ConnectorType.JIRA,
            resource="issues",
            records=[{"id": "100", "key": "PROJ-1"}],
            sample_count=1,
        ),
    ]
    findings = [
        Finding(
            category="stage",
            finding="SDLC Stage: Planning (Ticket Triage) — consider automating",
            evidence="Issues in planning statuses found",
            confidence="high",
        ),
        Finding(
            category="stage",
            finding="Development stage detected: source repositories found",
            evidence="Repositories accessible",
            confidence="high",
        ),
    ]
    ctx["samples"] = samples
    ctx["findings"] = findings


@given("no connectors have data")
def step_no_connector_data(ctx):
    """Empty samples — no data to generate a draft from."""
    ctx["samples"] = []
    ctx["findings"] = []


@given("scanned samples with planning stage")
def step_scanned_samples_planning(ctx):
    from modulo.determination.inference import Finding

    samples = [
        ScanSample(
            connector_id=uuid.uuid4(),
            connector_type=ConnectorType.JIRA,
            resource="issues",
            records=[{"id": "101", "key": "PROJ-2"}],
            sample_count=1,
        ),
    ]
    findings = [
        Finding(
            category="stage",
            finding="SDLC Stage: Planning (Ticket Triage) — consider automating",
            evidence="Issues in planning statuses found",
            confidence="high",
        ),
    ]
    ctx["samples"] = samples
    ctx["findings"] = findings


@when("the determination scanner samples the connector")
def step_scanner_sample_connector(ctx):
    """Use the scanner module to sample a single connector."""
    from modulo.determination.scanner import _sample_connector

    connector_id = ctx["_connector_id"]
    connector = ctx["_mock_connector"]

    loop = asyncio.new_event_loop()
    try:
        samples = loop.run_until_complete(_sample_connector(connector_id, connector))
        ctx["scan_samples"] = samples
    finally:
        loop.close()


@when("a pipeline draft is generated")
def step_generate_pipeline_draft(ctx):
    from modulo.determination.draft import generate_draft

    samples = ctx.get("samples", [])
    findings = ctx.get("findings", [])
    draft = generate_draft(samples, findings)
    ctx["pipeline_draft"] = draft


@then("the samples include repos")
def step_samples_include_repos(ctx):
    samples = ctx.get("scan_samples", [])
    repo_samples = [s for s in samples if s.resource == "repos"]
    assert len(repo_samples) > 0, "No repo samples found"
    assert repo_samples[0].sample_count > 0, "Repo sample has no records"


@then("the samples include pull requests")
def step_samples_include_pull_requests(ctx):
    samples = ctx.get("scan_samples", [])
    pr_samples = [s for s in samples if s.resource == "pulls"]
    assert len(pr_samples) > 0, "No PR samples found"
    assert pr_samples[0].sample_count > 0, "PR sample has no records"


@then("the samples include issues")
def step_samples_include_issues(ctx):
    samples = ctx.get("scan_samples", [])
    issue_samples = [s for s in samples if s.resource == "issues"]
    assert len(issue_samples) > 0, "No issue samples found"


@then("the draft has a start node and an end node")
def step_draft_has_start_end(ctx):
    draft = ctx.get("pipeline_draft")
    assert draft is not None, "No draft generated"
    node_ids = [n.id for n in draft.nodes]
    assert "start" in node_ids, f"Draft missing start node: {node_ids}"
    assert "end" in node_ids, f"Draft missing end node: {node_ids}"


@then("the draft has at least one agent node")
def step_draft_has_agent_node(ctx):
    draft = ctx.get("pipeline_draft")
    agent_nodes = [n for n in draft.nodes if n.node_type == "agent"]
    assert len(agent_nodes) > 0, "Draft has no agent nodes"


@then("the draft has no nodes")
def step_draft_has_no_nodes(ctx):
    draft = ctx.get("pipeline_draft")
    assert draft is not None and len(draft.nodes) == 0, (
        f"Expected empty draft, got {len(draft.nodes) if draft else 0} nodes"
    )


@then("the draft contains automation suggestions")
def step_draft_has_automation_suggestions(ctx):
    draft = ctx.get("pipeline_draft")
    assert draft is not None, "No draft generated"
    assert len(draft.automation_suggestions) > 0, "Draft has no automation suggestions"
