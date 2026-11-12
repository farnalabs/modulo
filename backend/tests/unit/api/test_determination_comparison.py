"""Tests for the read-only determination proposal comparison endpoint (FAR-2473).

Covers the wire shape, permission gating, request validation and the read-only
guarantee: the handler takes no database session, no audit event is appended for
a comparison (the request marks itself a no-op write) and both sides report
``activation_status`` ``not_activated``.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from farna.api.dependencies import get_db_session, get_plan_context
from farna.api.main import app
from farna.api.routes.determination import router as determination_router
from farna.auth.dependencies import get_current_tenant_user
from farna.auth.jwt import TenantPrincipal
from farna.core.audit_coverage import audit_session
from farna.determination.comparison import ProposalComparison
from farna.determination.models import (
    DeterminationAttempt,
    ProposalConfidence,
    ProposalContract,
    ProposalFact,
    ReviewState,
    SourceInventoryItem,
)
from tests.unit.api.mock_session import configure_mock_session

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROPOSAL_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TARGET_PROPOSAL_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")
_ATTEMPT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_FACT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

_COMPARE_PATH = "/api/v1/determination/compare"


def _make_principal(role: str) -> TenantPrincipal:
    return TenantPrincipal(
        username="operator" if role == "operator" else "viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )


def _make_mock_session() -> AsyncMock:
    return configure_mock_session(AsyncMock(), allow_empty_execute=True)


def _contract(
    *,
    proposal_id: uuid.UUID = _PROPOSAL_ID,
    confidence: ProposalConfidence = "medium",
    review_state: ReviewState = "needs_review",
    observed: list[ProposalFact] | None = None,
) -> ProposalContract:
    return ProposalContract(
        proposal_id=proposal_id,
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
        review_state=review_state,
        observed_facts=observed
        if observed is not None
        else [
            ProposalFact(
                id=_FACT_ID,
                statement="Observed 1 repository",
                kind="observed",
                confidence="high",
            )
        ],
        confidence=confidence,
    )


def _node_body(
    node_id: str = "development",
    *,
    node_type: str = "agent",
    label: str = "Development (Code Generation)",
    connector_type: str | None = "github",
) -> dict[str, Any]:
    return {"id": node_id, "node_type": node_type, "label": label, "connector_type": connector_type}


def _edge_body(
    source: str = "start",
    target: str = "development",
    *,
    edge_type: str = "normal",
    hitl_review: bool = False,
) -> dict[str, Any]:
    return {"source": source, "target": target, "edge_type": edge_type, "hitl_review": hitl_review}


def _side_body(
    *,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    contract: ProposalContract | None = None,
) -> dict[str, Any]:
    return {
        "proposal": (contract if contract is not None else _contract()).model_dump(mode="json"),
        "nodes": list(nodes or []),
        "edges": list(edges or []),
    }


def _body(
    *,
    base: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "base": base if base is not None else _side_body(),
        "target": target if target is not None else _side_body(),
    }


def _override_app(role: str | None, session: AsyncMock) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    async def override_audit_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[audit_session] = override_audit_session
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    if role is not None:
        app.dependency_overrides[get_current_tenant_user] = lambda: _make_principal(role)


@pytest.fixture
def session() -> AsyncMock:
    return _make_mock_session()


@pytest.fixture
def client(session: AsyncMock) -> Generator[TestClient, None, None]:
    _override_app("operator", session)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    _override_app("viewer", _make_mock_session())
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    _override_app(None, _make_mock_session())
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def audit_calls() -> Generator[dict[str, list[dict[str, Any]]], None, None]:
    """Capture the route's rich audit append and the coarse ``audited()`` append."""
    rich_calls: list[dict[str, Any]] = []
    coarse_calls: list[dict[str, Any]] = []

    async def fake_rich_audit(_session: Any, **kwargs: Any) -> None:
        rich_calls.append(kwargs)

    async def fake_coarse_audit(_session: Any, **kwargs: Any) -> None:
        coarse_calls.append(kwargs)

    with (
        patch("farna.api.routes.determination.append_audit_event", new=fake_rich_audit),
        patch("farna.core.audit_logger.append_audit_event", new=fake_coarse_audit),
    ):
        yield {"rich": rich_calls, "coarse": coarse_calls}


class TestComparisonResponse:
    def test_compare_returns_the_structured_comparison(self, client: TestClient) -> None:
        target = _side_body(
            contract=_contract(proposal_id=_TARGET_PROPOSAL_ID, confidence="high"),
            nodes=[
                _node_body("start", node_type="placeholder", label="Start", connector_type=None),
                _node_body("review", label="Code Review"),
            ],
            edges=[_edge_body("start", "review", hitl_review=True)],
        )

        resp = client.post(_COMPARE_PATH, json=_body(target=target))

        assert resp.status_code == 200
        data = resp.json()
        assert data["contract_version"] == "alpha.determination-comparison.v1"
        # Added items are ordered by their stable id.
        assert [node["id"] for node in data["nodes_added"]] == ["review", "start"]
        assert data["nodes_added"][0]["side"] == "target"
        assert [edge["id"] for edge in data["edges_added"]] == ["start->review"]
        assert [change["kind"] for change in data["behaviour_changes"]] == ["approval_gate_added"]
        assert data["base"]["proposal_id"] == str(_PROPOSAL_ID)
        assert data["target"]["proposal_id"] == str(_TARGET_PROPOSAL_ID)
        ProposalComparison.model_validate(data)

    def test_identical_sides_return_no_changes(self, client: TestClient) -> None:
        side = _side_body(
            nodes=[
                _node_body("start", node_type="placeholder", label="Start", connector_type=None),
                _node_body(),
            ],
            edges=[_edge_body("start", "development", hitl_review=True)],
        )

        resp = client.post(_COMPARE_PATH, json=_body(base=side, target=side))

        assert resp.status_code == 200
        data = resp.json()
        assert not data["nodes_added"]
        assert not data["nodes_removed"]
        assert not data["nodes_changed"]
        assert not data["edges_changed"]
        assert not data["behaviour_changes"]

    def test_empty_proposals_are_accepted(self, client: TestClient) -> None:
        empty = _side_body(contract=_contract(observed=[]))

        resp = client.post(_COMPARE_PATH, json=_body(base=empty, target=empty))

        assert resp.status_code == 200
        assert not resp.json()["nodes_added"]


class TestReadOnlyGuarantee:
    def test_no_audit_event_is_appended_for_a_comparison(
        self,
        client: TestClient,
        audit_calls: dict[str, list[dict[str, Any]]],
    ) -> None:
        resp = client.post(_COMPARE_PATH, json=_body())

        assert resp.status_code == 200
        assert not audit_calls["rich"]
        assert not audit_calls["coarse"]

    def test_no_orm_state_is_written(self, client: TestClient, session: AsyncMock) -> None:
        resp = client.post(_COMPARE_PATH, json=_body())

        assert resp.status_code == 200
        session.add.assert_not_called()
        session.add_all.assert_not_called()
        session.delete.assert_not_called()
        session.flush.assert_not_called()

    def test_both_sides_report_not_activated(self, client: TestClient) -> None:
        resp = client.post(_COMPARE_PATH, json=_body())

        assert resp.status_code == 200
        data = resp.json()
        assert data["base"]["activation_status"] == "not_activated"
        assert data["target"]["activation_status"] == "not_activated"

    def test_route_handler_takes_no_database_session(self) -> None:
        route = next(route for route in determination_router.routes if isinstance(route, APIRoute) and route.path == _COMPARE_PATH)

        assert "POST" in (route.methods or set())
        assert "session" not in inspect.signature(route.dependant.call).parameters


class TestValidationAndPermissions:
    def test_viewer_is_forbidden(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(_COMPARE_PATH, json=_body())

        assert resp.status_code == 403

    def test_unauthenticated_is_rejected(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(_COMPARE_PATH, json=_body())

        assert resp.status_code in (401, 403)

    def test_duplicate_node_id_is_rejected(self, client: TestClient) -> None:
        base = _side_body(nodes=[_node_body(), _node_body(label="Other")])

        resp = client.post(_COMPARE_PATH, json=_body(base=base))

        assert resp.status_code == 422

    def test_dangling_edge_is_rejected(self, client: TestClient) -> None:
        base = _side_body(nodes=[_node_body()], edges=[_edge_body("start", "development")])

        resp = client.post(_COMPARE_PATH, json=_body(base=base))

        assert resp.status_code == 422

    def test_invalid_proposal_contract_is_rejected(self, client: TestClient) -> None:
        body = _body()
        body["base"]["proposal"]["derived_elements"] = [
            {
                "id": str(_FACT_ID),
                "statement": "Not actually derived",
                "kind": "observed",
                "confidence": "high",
                "evidence_refs": [],
            }
        ]

        resp = client.post(_COMPARE_PATH, json=body)

        assert resp.status_code == 422

    def test_unknown_side_field_is_rejected(self, client: TestClient) -> None:
        body = _body()
        body["target"]["nodes"] = [_node_body()]
        body["target"]["automation_suggestions"] = []

        resp = client.post(_COMPARE_PATH, json=body)

        assert resp.status_code == 422
