"""BDD step definitions: SDLC determination endpoints."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, scenarios, then, when

from modulo.connectors.base import ConnectorType
from modulo.determination.draft import DraftNode, PipelineDraft
from modulo.determination.inference import Finding
from modulo.determination.models import (
    DeterminationAttempt,
    ProposalContract,
    ProposalFact,
    SourceInventoryItem,
    UnresolvedAssumption,
)
from modulo.determination.scanner import ScanSample

scenarios("determination.feature")

_PROPOSAL_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ATTEMPT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OBSERVED_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_DERIVED_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_ASSUMPTION_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


def _make_contract(*, assumption_confidence: str = "low") -> ProposalContract:
    return ProposalContract(
        proposal_id=_PROPOSAL_ID,
        attempt=DeterminationAttempt(
            id=_ATTEMPT_ID,
            status="completed",
            source_inventory=[
                SourceInventoryItem(
                    source_id="github-1:repos",
                    connector_type="github",
                    resource="repos",
                    sample_count=1,
                    status="available",
                )
            ],
        ),
        observed_facts=[
            ProposalFact(
                id=_OBSERVED_ID,
                statement="Observed 1 repository",
                kind="observed",
                confidence="high",
            )
        ],
        derived_elements=[
            ProposalFact(
                id=_DERIVED_ID,
                statement="Development stage observed",
                kind="derived",
                confidence="medium",
            )
        ],
        unresolved_assumptions=[
            UnresolvedAssumption(
                id=_ASSUMPTION_ID,
                statement="Deployment cadence is unknown",
                reason="No CI/CD evidence was sampled",
                confidence=assumption_confidence,
            )
        ],
        confidence="medium",
    )


def _mock_page(items: list[Any]) -> MagicMock:
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


@when("I submit an approve decision acknowledging unresolved assumptions")
def _bdd_approve_acknowledged(request) -> None:
    contract = _make_contract()
    body: dict[str, Any] = {
        "proposal_id": str(contract.proposal_id),
        "proposal": contract.model_dump(mode="json"),
        "decision": "approve",
        "acknowledged_assumption_ids": [str(_ASSUMPTION_ID)],
        "corrected_derived_elements": [],
        "removed_derived_element_ids": [],
    }
    request.node._resp = _request(request, "POST", "/api/v1/determination/review", json=body)


@when("I submit a reject decision with no reason")
def _bdd_reject_no_reason(request) -> None:
    contract = _make_contract()
    body: dict[str, Any] = {
        "proposal_id": str(contract.proposal_id),
        "proposal": contract.model_dump(mode="json"),
        "decision": "reject",
        "reason": None,
        "acknowledged_assumption_ids": [],
        "corrected_derived_elements": [],
        "removed_derived_element_ids": [],
    }
    request.node._resp = _request(request, "POST", "/api/v1/determination/review", json=body)


@when("I submit an approve decision without acknowledging low confidence assumptions")
def _bdd_approve_unacknowledged(request) -> None:
    contract = _make_contract()
    body: dict[str, Any] = {
        "proposal_id": str(contract.proposal_id),
        "proposal": contract.model_dump(mode="json"),
        "decision": "approve",
        "acknowledged_assumption_ids": [],
        "corrected_derived_elements": [],
        "removed_derived_element_ids": [],
    }
    request.node._resp = _request(request, "POST", "/api/v1/determination/review", json=body)


@when("I request POST /api/v1/determination/compare")
def _bdd_request_compare(request) -> None:
    contract = _make_contract()
    side_base = {
        "proposal": contract.model_dump(mode="json"),
        "nodes": [
            {"id": "node-1", "node_type": "manual", "label": "Code Review"},
        ],
        "edges": [],
    }
    side_target = {
        "proposal": contract.model_dump(mode="json"),
        "nodes": [
            {"id": "node-1", "node_type": "agent", "label": "Automated Review"},
        ],
        "edges": [],
    }
    body: dict[str, Any] = {
        "base": side_base,
        "target": side_target,
    }
    request.node._resp = _request(request, "POST", "/api/v1/determination/compare", json=body)


@when("I request POST /api/v1/determination/rerun")
def _bdd_request_rerun(request) -> None:
    contract = _make_contract()
    body: dict[str, Any] = {
        "prior_proposal": contract.model_dump(mode="json"),
        "corrected_derived_elements": [],
        "removed_derived_element_ids": [],
    }
    request.node._resp = _request(request, "POST", "/api/v1/determination/rerun", json=body)


def _request(request, method: str, path: str, json: dict[str, Any] | None = None):
    client = request.node._client
    no_connectors = bool(getattr(request.node, "_no_connectors", False))
    page_items = [] if no_connectors else [_mock_connector_instance()]
    samples = [] if no_connectors else [_github_sample()]
    findings = [_no_stages_finding()] if no_connectors else [_overview_finding()]

    with (
        patch("modulo.api.routes.determination.list_connector_instances", return_value=_mock_page(page_items)),
        patch("modulo.api.routes.determination.set_rls_org"),
        patch("modulo.api.routes.determination.set_rls_user_context"),
        patch("modulo.api.routes.determination.create_secrets_backend"),
        patch("modulo.api.routes.determination.ConnectorHub", _mock_hub_context()),
        patch("modulo.api.routes.determination.run_scan", new_callable=AsyncMock, return_value=samples),
        patch("modulo.api.routes.determination.infer", return_value=findings),
        patch("modulo.api.routes.determination.generate_draft", return_value=_draft()),
        patch("modulo.api.routes.determination.append_audit_event", new_callable=AsyncMock),
    ):
        return client.request(method, path, json=json)


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
    assert not request.node._resp.json()["samples"]


@then("the response contains draft nodes and edges")
def _bdd_has_draft(request) -> None:
    body = request.node._resp.json()
    assert body["nodes"], "expected at least one draft node"
    assert "edges" in body, f"missing edges: {body}"
    assert "automation_suggestions" in body, f"missing automation_suggestions: {body}"


@then("the proposal decision is approve")
def _bdd_proposal_decision_approve(request) -> None:
    body = request.node._resp.json()
    assert body["decision"] == "approve"


@then("the proposal review state is approved")
def _bdd_proposal_review_state_approved(request) -> None:
    body = request.node._resp.json()
    assert body["review_state"] == "approved"


@then("the proposal activation status is not_activated")
def _bdd_proposal_activation_status_not_activated(request) -> None:
    body = request.node._resp.json()
    assert body["activation_status"] == "not_activated"


@then("the comparison contract version is alpha.determination-comparison.v1")
def _bdd_comparison_contract_version(request) -> None:
    body = request.node._resp.json()
    assert body["contract_version"] == "alpha.determination-comparison.v1"


@then("the comparison activation status is not_activated")
def _bdd_comparison_activation_status(request) -> None:
    body = request.node._resp.json()
    assert body["base"]["activation_status"] == "not_activated"
    assert body["target"]["activation_status"] == "not_activated"


@then("the rerun contract version is alpha.determination-rerun.v1")
def _bdd_rerun_contract_version(request) -> None:
    body = request.node._resp.json()
    assert body["contract_version"] == "alpha.determination-rerun.v1"
