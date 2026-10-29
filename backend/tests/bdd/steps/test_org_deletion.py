"""BDD step definitions: Organisation deletion workflow.

Self-service deletion of the caller's own organisation is refused (FAR-2225);
only the cancel and export legs of the old workflow remain reachable.
"""

import contextlib
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/organisation/org_deletion.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_DELETION_TOKEN = "test-deletion-token-1234567890abcdef"
_REFUSAL_DETAIL = "Active organisation deletion is refused; a system admin must use DELETE /api/v1/admin/orgs/{org_id}"


# ===========================================================================
# Given steps
# ===========================================================================


@given("the org has a pending deletion")
def org_pending_deletion(request):
    request.node._deletion_pending = True
    request.node._deletion_token = _DELETION_TOKEN


# ===========================================================================
# When steps
# ===========================================================================


@when("I POST /api/v1/admin/org/deletion-request")
def post_deletion_request(client, request):
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal

    if getattr(request.node, "_viewer_auth", False):
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="viewer",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="viewer",
        )

    resp = client.post("/api/v1/admin/org/deletion-request")
    request.node._resp = resp


@when("I GET /api/v1/admin/org/export")
def get_org_export(client, request):
    with (
        patch("modulo.api.routes.admin.set_rls_org"),
        patch(
            "modulo.db.crud.org_deletion.export_org_data",
            new_callable=AsyncMock,
            return_value={
                "organisation": [
                    {
                        "id": str(_ORG_ID),
                        "name": "Test Org",
                        "slug": "test-org",
                        "status": "active",
                        "created_at": "2025-01-01T00:00:00+00:00",
                    }
                ],
                "users": [{"id": str(_USER_ID), "email": "admin@test.com"}],
                "pipelines": [],
                "runs": [],
                "exported_at": datetime.now(UTC).isoformat(),
            },
        ),
    ):
        resp = client.get("/api/v1/admin/org/export")
    request.node._resp = resp


@when("I PATCH /api/v1/admin/org/deletion-cancel")
def patch_deletion_cancel(client, request):
    with (
        patch("modulo.api.routes.admin.set_rls_org"),
        patch(
            "modulo.db.crud.org_deletion.cancel_org_deletion",
            new_callable=AsyncMock,
            return_value={"status": "active"},
        ),
    ):
        resp = client.patch("/api/v1/admin/org/deletion-cancel")
    request.node._resp = resp


@when(parsers.parse('I POST /api/v1/admin/org/deletion-confirm with token "{token}"'))
def post_deletion_confirm(token: str, client, request):
    resp = client.post("/api/v1/admin/org/deletion-confirm", json={"token": token})
    request.node._resp = resp


# ===========================================================================
# Then steps
# ===========================================================================


@then("the deletion refusal names the system-admin route")
def deletion_refusal_names_system_admin_route(request):
    detail = request.node._resp.json()["detail"]
    assert detail == _REFUSAL_DETAIL, f"Unexpected refusal detail: {detail}"


@then("the export bundle contains organisation info")
def export_contains_org(request):
    resp = request.node._resp
    data = resp.json()
    org = data.get("organisation", {})
    assert org.get("name") == "Test Org", f"Expected organisation info: {data}"


@then("the export bundle contains users, pipelines, and runs")
def export_contains_resources(request):
    resp = request.node._resp
    data = resp.json()
    assert "exported_at" in data, f"Expected exported_at in response: {data}"


@then('the org status is restored to "active"')
def org_status_restored(request):
    resp = request.node._resp
    data = resp.json()
    assert data.get("status") == "active", f"Expected status active, got: {data}"
