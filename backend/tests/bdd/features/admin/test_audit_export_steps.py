"""Step definitions for admin audit export BDD scenarios."""

from unittest.mock import patch

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

scenarios("audit_export.feature")


@given("I am authenticated as an admin")
def _bdd_auth_admin() -> None:
    """No-op — fixture handles this."""


@given("I am not authenticated")
def _bdd_not_authenticated(request) -> None:
    """Flag scenario for unauth client."""
    request.node._unauth = True


@when("I request GET /api/v1/admin/audit/export")
def _bdd_get_export(client: TestClient, unauth_client: TestClient, request) -> None:
    if getattr(request.node, "_unauth", False):
        resp = unauth_client.get("/api/v1/admin/audit/export")
        request.node._resp = resp
        return
    with (
        patch("modulo.api.routes.audit.export_chain") as mock_export,
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        mock_export.return_value = {
            "items": [{"id": "evt-1", "event_type": "pipeline.run"}],
            "total": 1,
            "page": 1,
            "page_size": 100,
        }
        resp = client.get("/api/v1/admin/audit/export")
        request.node._resp = resp
        request.node._mock_export = mock_export


@when(parsers.parse("I request GET /api/v1/admin/audit/export?event_type={event_type}"))
def _bdd_get_export_filtered(client: TestClient, request, event_type: str) -> None:
    with (
        patch("modulo.api.routes.audit.export_chain") as mock_export,
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        mock_export.return_value = {
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": 100,
        }
        resp = client.get(f"/api/v1/admin/audit/export?event_type={event_type}")
        request.node._resp = resp
        request.node._mock_export = mock_export


@when("I request GET /api/v1/admin/audit/verify")
def _bdd_get_verify(client: TestClient, request) -> None:
    with (
        patch("modulo.api.routes.audit.verify_chain") as mock_verify,
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        mock_verify.return_value = {
            "valid": True,
            "total_events": 3,
            "checked_events": 3,
            "event_count": 3,
            "first_gap_index": None,
            "first_tampered_id": None,
            "chain_head_match": True,
        }
        resp = client.get("/api/v1/admin/audit/verify")
        request.node._resp = resp
        request.node._mock_verify = mock_verify


@then("the response contains items, total, page, and page_size")
def _bdd_check_export_fields(request) -> None:
    data = request.node._resp.json()
    assert "items" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data


@then(parsers.parse("the export endpoint receives event_type filter {expected}"))
def _bdd_check_export_filter(request, expected: str) -> None:
    _, kwargs = request.node._mock_export.call_args
    assert kwargs.get("event_type") == expected


@then("the verify response contains event_count field")
def _bdd_check_verify_event_count(request) -> None:
    data = request.node._resp.json()
    assert "event_count" in data
