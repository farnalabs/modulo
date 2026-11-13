"""Tests for the determination proposal review-decision endpoint.

Covers the approve / reject / needs_review decisions, the approval gate on
unacknowledged low/unknown-confidence assumptions, derived-element
corrections, the audit record (coarse + rich), and the guarantee that a review
decision never activates anything.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from farna.api.dependencies import get_db_session, get_plan_context
from farna.api.main import app
from farna.auth.dependencies import get_current_tenant_user
from farna.auth.jwt import TenantPrincipal
from farna.core.audit_coverage import audit_session
from farna.determination.models import (
    DeterminationAttempt,
    ProposalContract,
    ProposalFact,
    SourceInventoryItem,
    UnresolvedAssumption,
)
from farna.determination.review import ProposalReviewRequest
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROPOSAL_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ATTEMPT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OBSERVED_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_DERIVED_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_ASSUMPTION_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_principal(role: str) -> TenantPrincipal:
    return TenantPrincipal(
        username="operator" if role == "operator" else "viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )


def _make_mock_session() -> AsyncMock:
    return configure_mock_session(AsyncMock(), allow_empty_execute=True)


def _contract(*, assumption_confidence: str = "low", statement: str = "Development stage observed") -> ProposalContract:
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
                statement=statement,
                kind="derived",
                confidence="medium",
            )
        ],
        unresolved_assumptions=[
            UnresolvedAssumption(
                id=_ASSUMPTION_ID,
                statement="Deployment cadence is unknown",
                reason="No CI/CD evidence was sampled",
                confidence=assumption_confidence,  # type: ignore[arg-type]
            )
        ],
        confidence="medium",
    )


def _review_body(
    *,
    decision: str = "approve",
    reason: str | None = None,
    proposal: ProposalContract | None = None,
    acknowledged_assumption_ids: list[uuid.UUID] | None = None,
    corrected_derived_elements: list[dict[str, Any]] | None = None,
    removed_derived_element_ids: list[uuid.UUID] | None = None,
    proposal_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    contract = proposal if proposal is not None else _contract()
    return {
        "proposal_id": str(proposal_id if proposal_id is not None else contract.proposal_id),
        "proposal": contract.model_dump(mode="json"),
        "decision": decision,
        "reason": reason,
        "acknowledged_assumption_ids": [str(i) for i in (acknowledged_assumption_ids or [])],
        "corrected_derived_elements": corrected_derived_elements or [],
        "removed_derived_element_ids": [str(i) for i in (removed_derived_element_ids or [])],
    }


def _override_app(role: str | None, session: AsyncMock) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    async def override_audit_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[audit_session] = override_audit_session
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    if role is not None:
        app.dependency_overrides[get_current_tenant_user] = lambda: _make_principal(role)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    _override_app("operator", _make_mock_session())
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
def audits() -> Generator[dict[str, list[dict[str, Any]]], None, None]:
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
        patch("farna.api.routes.determination.set_rls_org", new=AsyncMock()),
        patch("farna.api.routes.determination.set_rls_user_context", new=AsyncMock()),
    ):
        yield {"rich": rich_calls, "coarse": coarse_calls}


class TestApproval:
    def test_approve_records_review_and_audit(self, client: TestClient, audits: dict[str, list[dict[str, Any]]]) -> None:
        resp = client.post(
            "/api/v1/determination/review",
            json=_review_body(acknowledged_assumption_ids=[_ASSUMPTION_ID]),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "approve"
        assert data["review_state"] == "approved"
        assert data["activation_status"] == "not_activated"
        assert data["proposal"]["review_state"] == "approved"
        assert data["proposal"]["activation_status"] == "not_activated"
        assert data["actor_id"] == str(_USER_ID)
        assert data["actor_username"] == "operator"
        assert datetime.fromisoformat(data["decided_at"])
        assert data["acknowledged_assumption_ids"] == [str(_ASSUMPTION_ID)]

        assert len(audits["rich"]) == 1
        audit = audits["rich"][0]
        assert audit["event_type"] == "determination.proposal_review_decided"
        assert audit["resource_type"] == "determination_proposal"
        assert audit["resource_id"] == _PROPOSAL_ID
        assert audit["actor_user_id"] == _USER_ID
        assert audit["org_id"] == _ORG_ID
        assert audit["payload_json"]["decision"] == "approve"
        assert audit["payload_json"]["review_state"] == "approved"
        assert audit["payload_json"]["activation_status"] == "not_activated"
        assert audit["payload_json"]["acknowledged_assumption_ids"] == [str(_ASSUMPTION_ID)]
        assert audit["payload_json"]["actor"] == "operator"

        assert len(audits["coarse"]) == 1
        coarse = audits["coarse"][0]
        assert coarse["event_type"] == "api_access_post"
        assert coarse["resource_type"] == "determination_proposal"
        assert coarse["payload_json"]["outcome"] == "success"

    def test_approve_blocked_by_unacknowledged_assumption(self, client: TestClient, audits: dict[str, list[dict[str, Any]]]) -> None:
        resp = client.post("/api/v1/determination/review", json=_review_body())

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "unacknowledged" in detail
        assert str(_ASSUMPTION_ID) in detail
        assert not audits["rich"]
        assert audits["coarse"][0]["payload_json"]["outcome"] == "error"

    def test_acknowledgement_permits_approval(self, client: TestClient, audits: dict[str, list[dict[str, Any]]]) -> None:
        resp = client.post(
            "/api/v1/determination/review",
            json=_review_body(acknowledged_assumption_ids=[_ASSUMPTION_ID]),
        )

        assert resp.status_code == 200
        assert resp.json()["review_state"] == "approved"
        assert audits["rich"][0]["payload_json"]["acknowledged_assumption_ids"] == [str(_ASSUMPTION_ID)]

    def test_high_confidence_assumption_does_not_block_approval(self, client: TestClient) -> None:
        proposal = _contract(assumption_confidence="high")

        resp = client.post("/api/v1/determination/review", json=_review_body(proposal=proposal))

        assert resp.status_code == 200
        assert resp.json()["review_state"] == "approved"


class TestRejectAndNeedsReview:
    def test_reject_with_reason_is_recorded(self, client: TestClient, audits: dict[str, list[dict[str, Any]]]) -> None:
        resp = client.post(
            "/api/v1/determination/review",
            json=_review_body(decision="reject", reason="Evidence covers repositories only"),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["review_state"] == "rejected"
        assert data["proposal"]["review_state"] == "rejected"
        assert data["activation_status"] == "not_activated"
        assert data["reason"] == "Evidence covers repositories only"
        assert audits["rich"][0]["payload_json"]["reason"] == "Evidence covers repositories only"
        assert audits["rich"][0]["payload_json"]["review_state"] == "rejected"

    def test_reject_without_reason_is_refused(self, client: TestClient, audits: dict[str, list[dict[str, Any]]]) -> None:
        resp = client.post("/api/v1/determination/review", json=_review_body(decision="reject"))

        assert resp.status_code == 422
        assert "reason" in resp.json()["detail"]
        assert not audits["rich"]

    def test_needs_review_is_recorded(self, client: TestClient) -> None:
        resp = client.post("/api/v1/determination/review", json=_review_body(decision="needs_review"))

        assert resp.status_code == 200
        data = resp.json()
        assert data["review_state"] == "needs_review"
        assert data["proposal"]["review_state"] == "needs_review"
        assert data["activation_status"] == "not_activated"


class TestCorrections:
    def test_corrected_proposal_is_returned_validated(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/determination/review",
            json=_review_body(
                acknowledged_assumption_ids=[_ASSUMPTION_ID],
                corrected_derived_elements=[{"id": str(_DERIVED_ID), "statement": "Delivery stage observed", "confidence": "high"}],
            ),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["corrected_derived_element_ids"] == [str(_DERIVED_ID)]
        corrected = data["proposal"]["derived_elements"][0]
        assert corrected["statement"] == "Delivery stage observed"
        assert corrected["confidence"] == "high"
        # The returned contract is a complete, valid ProposalContract.
        ProposalContract.model_validate(data["proposal"])

    def test_removed_derived_element_is_returned(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/determination/review",
            json=_review_body(
                acknowledged_assumption_ids=[_ASSUMPTION_ID],
                removed_derived_element_ids=[_DERIVED_ID],
            ),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["removed_derived_element_ids"] == [str(_DERIVED_ID)]
        assert not data["proposal"]["derived_elements"]

    def test_correction_of_observed_fact_is_refused(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/determination/review",
            json=_review_body(
                corrected_derived_elements=[{"id": str(_OBSERVED_ID), "statement": "Rewritten"}],
            ),
        )

        assert resp.status_code == 422
        assert "derived elements" in resp.json()["detail"]


class TestRequestValidation:
    def test_invalid_decision_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/determination/review", json=_review_body(decision="maybe"))

        assert resp.status_code == 422

    def test_proposal_id_mismatch_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/determination/review",
            json=_review_body(proposal_id=uuid.uuid4(), acknowledged_assumption_ids=[_ASSUMPTION_ID]),
        )

        assert resp.status_code == 422
        assert "proposal_id" in resp.json()["detail"]

    def test_invalid_proposal_contract_returns_422(self, client: TestClient) -> None:
        body = _review_body()
        body["proposal"]["derived_elements"][0]["kind"] = "observed"

        resp = client.post("/api/v1/determination/review", json=body)

        assert resp.status_code == 422

    def test_viewer_returns_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post("/api/v1/determination/review", json=_review_body())

        assert resp.status_code == 403

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post("/api/v1/determination/review", json=_review_body())

        assert resp.status_code in (401, 403)


class TestNoActivation:
    def test_review_writes_no_orm_state_and_records_not_activated(self, client: TestClient, audits: dict[str, list[dict[str, Any]]]) -> None:
        session = _make_mock_session()
        _override_app("operator", session)

        resp = client.post(
            "/api/v1/determination/review",
            json=_review_body(acknowledged_assumption_ids=[_ASSUMPTION_ID]),
        )

        assert resp.status_code == 200
        assert resp.json()["activation_status"] == "not_activated"
        # No pipeline/trigger/connector row is created, updated or deleted: the
        # only persistence is the audit append (stubbed out here).
        session.add.assert_not_called()
        session.add_all.assert_not_called()
        session.delete.assert_not_called()
        session.flush.assert_not_called()
        assert audits["rich"][0]["payload_json"]["activation_status"] == "not_activated"

    def test_audit_append_failure_is_not_reported_as_success(self, client: TestClient) -> None:
        async def failing_audit(_session: Any, **_kwargs: Any) -> None:
            raise RuntimeError("audit chain unavailable")

        with (
            patch("farna.api.routes.determination.append_audit_event", new=failing_audit),
            patch("farna.api.routes.determination.set_rls_org", new=AsyncMock()),
            patch("farna.api.routes.determination.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.post(
                "/api/v1/determination/review",
                json=_review_body(acknowledged_assumption_ids=[_ASSUMPTION_ID]),
            )

        assert resp.status_code == 500


def test_review_request_model_round_trips_from_route_payload() -> None:
    """The wire payload the tests (and the frontend) send parses as the request model."""
    parsed = ProposalReviewRequest.model_validate(_review_body(acknowledged_assumption_ids=[_ASSUMPTION_ID]))

    assert parsed.decision == "approve"
    assert parsed.proposal.proposal_id == _PROPOSAL_ID
    assert parsed.acknowledged_assumption_ids == [_ASSUMPTION_ID]
