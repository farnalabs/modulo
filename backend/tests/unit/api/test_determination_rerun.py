"""Tests for the determination rerun endpoint.

Covers the happy path (a new attempt, the prior attempt untouched, unchanged
inputs retaining every element), invalid corrections, the read-only guarantee,
permission enforcement, the error-handling matrix and explicit incompleteness
when a source is unavailable or missing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.connectors.base import ConnectorType
from modulo.core.audit_coverage import audit_session
from modulo.core.connector_hub import ConnectorDecryptError
from modulo.determination.inference import build_proposal_contract
from modulo.determination.models import ProposalContract
from modulo.determination.rerun import ProposalRerunRequest
from modulo.determination.scanner import ScanSample
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_CONNECTOR_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")


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


def _mock_page(items: list[Any]) -> MagicMock:
    page = MagicMock()
    page.items = items
    page.total = len(items)
    page.page = 1
    page.page_size = 100
    return page


def _mock_connector_instance() -> MagicMock:
    ci = MagicMock()
    ci.id = _CONNECTOR_ID
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


def _available_sample() -> ScanSample:
    return ScanSample(
        connector_id=_CONNECTOR_ID,
        connector_type=ConnectorType.GITHUB,
        resource="repos",
        records=[{"name": "owner/repo1"}],
        sample_count=1,
    )


def _unavailable_sample() -> ScanSample:
    return ScanSample(
        connector_id=_CONNECTOR_ID,
        connector_type=ConnectorType.GITHUB,
        resource="repos",
        records=[],
        sample_count=0,
        error="timeout",
    )


def _body(
    prior: ProposalContract,
    *,
    corrected: list[dict[str, Any]] | None = None,
    removed: list[uuid.UUID] | None = None,
) -> dict[str, Any]:
    return {
        "prior_proposal": prior.model_dump(mode="json"),
        "corrected_derived_elements": corrected or [],
        "removed_derived_element_ids": [str(element_id) for element_id in (removed or [])],
    }


def _override_app(role: str | None, session: AsyncMock) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[audit_session] = override_session
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    if role is not None:
        app.dependency_overrides[get_current_tenant_user] = lambda: _make_principal(role)


@pytest.fixture
def audits() -> Generator[list[dict[str, Any]], None, None]:
    """Capture the coarse ``audited()`` append."""
    calls: list[dict[str, Any]] = []

    async def fake_audit(_session: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    with patch("modulo.core.audit_logger.append_audit_event", new=fake_audit):
        yield calls


@pytest.fixture
def client(audits: list[dict[str, Any]]) -> Generator[TestClient, None, None]:
    _override_app("operator", _make_mock_session())
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client(audits: list[dict[str, Any]]) -> Generator[TestClient, None, None]:
    _override_app("viewer", _make_mock_session())
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client(audits: list[dict[str, Any]]) -> Generator[TestClient, None, None]:
    _override_app(None, _make_mock_session())
    yield TestClient(app)
    app.dependency_overrides.clear()


@contextmanager
def _scan_environment(
    samples: list[ScanSample] | None,
    *,
    scan_error: BaseException | None = None,
) -> Generator[None, None, None]:
    with (
        patch(
            "modulo.api.routes.determination.list_connector_instances",
            return_value=_mock_page([_mock_connector_instance()]),
        ),
        patch("modulo.api.routes.determination.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.determination.set_rls_user_context", new=AsyncMock()),
        patch("modulo.api.routes.determination.create_secrets_backend"),
        patch("modulo.api.routes.determination.ConnectorHub", _mock_hub_context()),
        patch(
            "modulo.api.routes.determination.run_scan",
            new_callable=AsyncMock,
            return_value=samples,
            side_effect=scan_error,
        ),
    ):
        yield


class TestRerunHappyPath:
    def test_rerun_returns_a_new_attempt_and_retains_unchanged_elements(
        self, client: TestClient, audits: list[dict[str, Any]]
    ) -> None:
        sample = _available_sample()
        prior = build_proposal_contract([sample])

        with _scan_environment([sample]):
            resp = client.post("/api/v1/determination/rerun", json=_body(prior))

        assert resp.status_code == 200
        data = resp.json()
        assert data["proposal"]["attempt"]["id"] != str(prior.attempt.id)
        assert data["prior_attempt"]["attempt_id"] == str(prior.attempt.id)
        assert data["current_attempt"]["attempt_id"] == data["proposal"]["attempt"]["id"]
        assert data["input_set_unchanged"] is True
        assert data["element_changes"]
        assert {change["status"] for change in data["element_changes"]} == {"retained"}

        assert audits[-1]["event_type"] == "determination_rerun_performed"
        assert audits[-1]["resource_type"] == "determination_attempt"
        assert audits[-1]["payload_json"]["outcome"] == "success"

    def test_rerun_is_read_only(self, client: TestClient) -> None:
        sample = _available_sample()
        prior = build_proposal_contract([sample])
        session = _make_mock_session()
        _override_app("operator", session)

        with _scan_environment([sample]):
            resp = client.post("/api/v1/determination/rerun", json=_body(prior))

        assert resp.status_code == 200
        session.add.assert_not_called()
        session.add_all.assert_not_called()
        session.delete.assert_not_called()
        session.flush.assert_not_called()

    def test_correction_is_carried_forward(self, client: TestClient) -> None:
        sample = _available_sample()
        prior = build_proposal_contract([sample])
        element_id = prior.derived_elements[0].id
        statement = prior.derived_elements[0].statement

        with _scan_environment([sample]):
            resp = client.post(
                "/api/v1/determination/rerun",
                json=_body(
                    prior,
                    corrected=[{"id": str(element_id), "statement": f"{statement} (confirmed)"}],
                ),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["corrections"]["corrected_derived_element_ids"] == [str(element_id)]
        assert any(
            element["statement"] == f"{statement} (confirmed)" for element in data["proposal"]["derived_elements"]
        )


class TestRerunIncompleteness:
    def test_newly_unavailable_source_marks_the_rerun_incomplete(self, client: TestClient) -> None:
        prior = build_proposal_contract([_available_sample()])

        with _scan_environment([_unavailable_sample()]):
            resp = client.post("/api/v1/determination/rerun", json=_body(prior))

        assert resp.status_code == 200
        data = resp.json()
        assert data["source_changes"]["newly_unavailable"] == [f"{_CONNECTOR_ID}:repos"]
        assert data["incomplete"] is True
        assert data["proposal"]["attempt"]["status"] == "completed_with_gaps"

    def test_missing_source_is_surfaced_as_incomplete(self, client: TestClient) -> None:
        pulls = ScanSample(
            connector_id=_CONNECTOR_ID,
            connector_type=ConnectorType.GITHUB,
            resource="pulls",
            records=[{"created_at": "2026-01-01T00:00:00Z"}],
            sample_count=1,
        )
        prior = build_proposal_contract([_available_sample(), pulls])

        with _scan_environment([_available_sample()]):
            resp = client.post("/api/v1/determination/rerun", json=_body(prior))

        assert resp.status_code == 200
        data = resp.json()
        missing = [item["source_id"] for item in data["source_changes"]["missing_sources"]]
        assert missing == [f"{_CONNECTOR_ID}:pulls"]
        assert data["incomplete"] is True
        assert f"source no longer sampled: {_CONNECTOR_ID}:pulls" in data["incomplete_reasons"]


class TestRerunValidation:
    def test_unknown_correction_id_returns_422(self, client: TestClient) -> None:
        sample = _available_sample()
        prior = build_proposal_contract([sample])

        with _scan_environment([sample]):
            resp = client.post(
                "/api/v1/determination/rerun",
                json=_body(prior, corrected=[{"id": str(uuid.uuid4()), "confidence": "low"}]),
            )

        assert resp.status_code == 422
        assert "derived elements" in resp.json()["detail"]

    def test_invalid_prior_contract_returns_422(self, client: TestClient) -> None:
        body = _body(build_proposal_contract([_available_sample()]))
        body["prior_proposal"]["derived_elements"] = "not-a-list"

        with _scan_environment([_available_sample()]):
            resp = client.post("/api/v1/determination/rerun", json=body)

        assert resp.status_code == 422

    def test_viewer_returns_403(self, viewer_client: TestClient) -> None:
        with _scan_environment([_available_sample()]):
            resp = viewer_client.post(
                "/api/v1/determination/rerun",
                json=_body(build_proposal_contract([_available_sample()])),
            )

        assert resp.status_code == 403

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        with _scan_environment([_available_sample()]):
            resp = unauth_client.post(
                "/api/v1/determination/rerun",
                json=_body(build_proposal_contract([_available_sample()])),
            )

        assert resp.status_code in (401, 403)


def _make_exc(error_type: type[Exception]) -> Exception:
    if issubclass(error_type, IntegrityError):
        return IntegrityError("stmt", {}, Exception("mock constraint violation"))
    if issubclass(error_type, ProgrammingError):
        return ProgrammingError("stmt", {}, Exception("mock table does not exist"))
    if issubclass(error_type, SQLAlchemyError):
        return SQLAlchemyError("mock", "mock", "mock")
    return error_type("mock unexpected error")


@pytest.mark.parametrize(
    ("error_type", "expected_status"),
    [
        (ProgrammingError, 501),
        (SQLAlchemyError, 503),
        (ValueError, 500),
    ],
    ids=["programming_error", "sqlalchemy_error", "unexpected_error"],
)
def test_rerun_error_mapping(
    client: TestClient,
    error_type: type[Exception],
    expected_status: int,
) -> None:
    with _scan_environment(None, scan_error=_make_exc(error_type)):
        resp = client.post(
            "/api/v1/determination/rerun",
            json=_body(build_proposal_contract([_available_sample()])),
        )

    assert resp.status_code == expected_status


@contextmanager
def _connector_listing_error(error: BaseException) -> Generator[None, None, None]:
    with (
        patch("modulo.api.routes.determination.list_connector_instances", side_effect=error),
        patch("modulo.api.routes.determination.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.determination.set_rls_user_context", new=AsyncMock()),
    ):
        yield


@contextmanager
def _hub_initialise_error(error: BaseException) -> Generator[None, None, None]:
    hub = AsyncMock()
    hub.initialise = AsyncMock(side_effect=error)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=hub)
    cm.__aexit__ = AsyncMock(return_value=False)
    with (
        patch(
            "modulo.api.routes.determination.list_connector_instances",
            return_value=_mock_page([_mock_connector_instance()]),
        ),
        patch("modulo.api.routes.determination.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.determination.set_rls_user_context", new=AsyncMock()),
        patch("modulo.api.routes.determination.create_secrets_backend"),
        patch("modulo.api.routes.determination.ConnectorHub", MagicMock(return_value=cm)),
    ):
        yield


class TestRerunErrorHandlingDetails:
    def test_connector_listing_programming_error_returns_501(self, client: TestClient) -> None:
        exc = ProgrammingError("stmt", {}, Exception("mock table does not exist"))

        with _connector_listing_error(exc):
            resp = client.post(
                "/api/v1/determination/rerun",
                json=_body(build_proposal_contract([_available_sample()])),
            )

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_connector_listing_integrity_error_returns_409(self, client: TestClient) -> None:
        exc = IntegrityError("stmt", {}, Exception("mock constraint violation"))

        with _connector_listing_error(exc):
            resp = client.post(
                "/api/v1/determination/rerun",
                json=_body(build_proposal_contract([_available_sample()])),
            )

        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()

    def test_connector_decrypt_error_returns_502(self, client: TestClient) -> None:
        with _hub_initialise_error(ConnectorDecryptError(_CONNECTOR_ID)):
            resp = client.post(
                "/api/v1/determination/rerun",
                json=_body(build_proposal_contract([_available_sample()])),
            )

        assert resp.status_code == 502
        assert str(_CONNECTOR_ID) in resp.json()["detail"]

    def test_cancelled_error_propagates(self) -> None:
        request = ProposalRerunRequest(prior_proposal=build_proposal_contract([_available_sample()]))

        from modulo.api.routes.determination import rerun_determination

        with (
            _scan_environment(None, scan_error=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            asyncio.run(
                rerun_determination(
                    request,
                    session=_make_mock_session(),
                    principal=_make_principal("operator"),
                    settings=_make_settings(),
                )
            )
