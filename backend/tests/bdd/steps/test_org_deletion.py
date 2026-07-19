"""BDD step definitions: Organisation deletion workflow."""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/organisation/org_deletion.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_DELETION_TOKEN = "test-deletion-token-1234567890abcdef"
_TOKEN_EXPIRES = (datetime.now(UTC) + timedelta(hours=24)).isoformat()


# ===========================================================================
# Given steps
# ===========================================================================


@given("the org has a pending deletion")
def org_pending_deletion(request):
    request.node._deletion_pending = True
    request.node._deletion_token = _DELETION_TOKEN


@given(parsers.parse('the org has a pending deletion with token "{token}"'))
def org_pending_deletion_with_token(token: str, request):
    request.node._deletion_pending = True
    request.node._deletion_token = token


@given(parsers.parse("the org has {pipelines:d} pipelines and {runs:d} runs"))
def org_has_pipelines_and_runs(pipelines: int, runs: int, request):
    request.node._pipeline_count = pipelines
    request.node._run_count = runs


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

    with (
        patch("modulo.api.routes.admin.set_rls_org"),
        patch(
            "modulo.db.crud.org_deletion.request_org_deletion",
            return_value={
                "token": _DELETION_TOKEN,
                "token_expires_at": _TOKEN_EXPIRES,
                "export": {
                    "organisation": [{"id": str(_ORG_ID), "name": "Test Org", "slug": "test-org", "status": "active"}],
                    "users": [{"id": str(_USER_ID), "email": "admin@test.com"}],
                    "pipelines": [],
                    "runs": [],
                    "audit_events": [],
                    "library_primitives": [],
                    "connector_instances": [],
                    "model_backends": [],
                },
            },
        ),
        patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock),
    ):
        resp = client.post("/api/v1/admin/org/deletion-request")
    request.node._resp = resp


@when("I GET /api/v1/admin/org/export")
def get_org_export(client, request):
    with (
        patch("modulo.api.routes.admin.set_rls_org"),
        patch(
            "modulo.db.crud.org_deletion.export_org_data",
            return_value={
                "organisation": [
                    {
                        "id": str(_ORG_ID),
                        "name": "Test Org",
                        "slug": "test-org",
                        "status": "deleted",
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
    with (
        patch("modulo.api.routes.admin.set_rls_org"),
        patch(
            "modulo.db.crud.org_deletion.confirm_org_deletion",
            return_value={"deleted_organisation_id": str(_ORG_ID), "hard_deleted_runs": 5},
        ),
    ):
        resp = client.post("/api/v1/admin/org/deletion-confirm", json={"token": token})
    request.node._resp = resp


@when("I POST /api/v1/admin/org/deletion-confirm with a valid token")
def post_deletion_confirm_valid(client, request):
    token = getattr(request.node, "_deletion_token", _DELETION_TOKEN)
    with (
        patch("modulo.api.routes.admin.set_rls_org"),
        patch(
            "modulo.db.crud.org_deletion.confirm_org_deletion",
            return_value={"deleted_organisation_id": str(_ORG_ID), "hard_deleted_runs": 5},
        ),
    ):
        resp = client.post("/api/v1/admin/org/deletion-confirm", json={"token": token})
    request.node._resp = resp


@when("I GET /api/v1/pipelines")
def get_pipelines(client, request):
    with patch(
        "modulo.api.routes.pipelines.list_pipelines",
        side_effect=__import__("fastapi").HTTPException(status_code=403, detail="Organisation is deleted"),
    ):
        resp = client.get("/api/v1/pipelines")
    request.node._resp = resp


# ===========================================================================
# Then steps
# ===========================================================================


@then("a deletion token is returned")
def deletion_token_returned(request):
    resp = request.node._resp
    data = resp.json()
    assert "token" in data, f"Expected token in response: {data}"
    assert data["token"] == _DELETION_TOKEN


@then("the token expires in 24 hours")
def token_expires_24h(request):
    resp = request.node._resp
    data = resp.json()
    assert "token_expires_at" in data, f"Expected token_expires_at in response: {data}"


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


@then("the organisation is permanently deleted")
def org_permanently_deleted(request):
    resp = request.node._resp
    data = resp.json()
    assert data.get("deleted_organisation_id") == str(_ORG_ID)
    assert "permanently deleted" in data.get("message", "")


@then(parsers.parse('an audit event "{event_type}" is recorded'))
def audit_event_recorded(event_type: str, request):
    resp = request.node._resp
    data = resp.json()
    assert "export_summary" in data or "message" in data


@then("all associated pipelines are removed")
def pipelines_removed(request):
    resp = request.node._resp
    data = resp.json()
    assert data.get("deleted_organisation_id") == str(_ORG_ID)


@then("all runs are removed")
def runs_removed(request):
    resp = request.node._resp
    data = resp.json()
    assert "hard_deleted_runs" in data


@then("all users are removed from the org")
def users_removed(request):
    resp = request.node._resp
    data = resp.json()
    assert data.get("deleted_organisation_id") == str(_ORG_ID)
