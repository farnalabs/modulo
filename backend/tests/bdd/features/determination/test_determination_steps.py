"""BDD step definitions: SDLC determination endpoints."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, scenarios, then, when

from modulo.connectors.base import ConnectorType
from modulo.determination.draft import DraftNode, PipelineDraft
from modulo.determination.inference import Finding
from modulo.determination.scanner import ScanSample

scenarios("determination.feature")


def _mock_page(items: list) -> MagicMock:
    page = MagicMock()
    page.items = items
    page.total = len(items)
    page.page = 1
    page.page_size = 100
    return page


def _mock_connector_instance() -> MagicMock:
    ci = MagicMock()
    ci.id = uuid.uuid4()
    ci.name = "Test Connector"
    ci.connector_type_id = "github"
    ci.config_json = {}
    ci.credentials_ciphertext = b"encrypted"
    ci.visibility = "org"
    ci.allowed_operations = None
    return ci


def _mock_hub_context() -> MagicMock:
    hub = AsyncMock()
    hub.initialise = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=hub)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _github_sample() -> ScanSample:
    return ScanSample(
        connector_id=uuid.uuid4(),
        connector_type=ConnectorType.GITHUB,
        resource="repos",
        records=[{"name": "owner/repo1"}],
        sample_count=1,
    )


def _overview_finding() -> Finding:
    return Finding(
        category="overview",
        finding="SDLC stages detected: development",
        evidence="1 repository accessible",
        confidence="medium",
    )


def _no_stages_finding() -> Finding:
    return Finding(
        category="overview",
        finding="No SDLC stages could be detected from connected tools",
        evidence="No connector produced stage-identifying records",
        confidence="low",
    )


def _draft() -> PipelineDraft:
    return PipelineDraft(
        nodes=[
            DraftNode(id="start", node_type="placeholder", label="Start"),
            DraftNode(id="end", node_type="placeholder", label="End"),
        ],
        edges=[],
        findings=[_overview_finding()],
        automation_suggestions=[],
    )


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("I am authenticated as an operator")
def _bdd_auth_operator(request, client) -> None:
    request.node._client = client


@given("I am authenticated as a viewer")
def _bdd_auth_viewer(request, viewer_client) -> None:
    request.node._client = viewer_client


@given("connected tools are configured")
def _bdd_connectors_configured(request) -> None:
    request.node._no_connectors = False


@given("no connected tools are configured")
def _bdd_no_connectors_configured(request) -> None:
    request.node._no_connectors = True


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("I request GET /api/v1/determination")
def _bdd_request_determination(request) -> None:
    request.node._resp = _request(request, "GET", "/api/v1/determination")


@when("I request POST /api/v1/determination/draft")
def _bdd_request_determination_draft(request) -> None:
    request.node._resp = _request(request, "POST", "/api/v1/determination/draft")


def _request(request, method: str, path: str):
    client = request.node._client
    no_connectors = bool(getattr(request.node, "_no_connectors", False))
    page_items = [] if no_connectors else [_mock_connector_instance()]
    samples = [] if no_connectors else [_github_sample()]
    findings = [_no_stages_finding()] if no_connectors else [_overview_finding()]

    with (
        patch("modulo.api.routes.determination.list_connector_instances", return_value=_mock_page(page_items)),
        patch("modulo.api.routes.determination.set_rls_org"),
        patch("modulo.api.routes.determination.create_secrets_backend"),
        patch("modulo.api.routes.determination.ConnectorHub", _mock_hub_context()),
        patch("modulo.api.routes.determination.run_scan", new_callable=AsyncMock, return_value=samples),
        patch("modulo.api.routes.determination.infer", return_value=findings),
        patch("modulo.api.routes.determination.generate_draft", return_value=_draft()),
    ):
        return client.request(method, path)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the response contains a determination summary")
def _bdd_has_summary(request) -> None:
    body = request.node._resp.json()
    assert isinstance(body, dict)
    assert "summary" in body, f"missing summary: {body}"
    assert "findings" in body, f"missing findings: {body}"


@then("the response contains sample results")
def _bdd_has_samples(request) -> None:
    body = request.node._resp.json()
    assert body["samples"], "expected at least one sample"


@then("the response has an empty sample list")
def _bdd_empty_samples(request) -> None:
    assert request.node._resp.json()["samples"] == []


@then("the response contains draft nodes and edges")
def _bdd_has_draft(request) -> None:
    body = request.node._resp.json()
    assert body["nodes"], "expected at least one draft node"
    assert "edges" in body, f"missing edges: {body}"
    assert "automation_suggestions" in body, f"missing automation_suggestions: {body}"
