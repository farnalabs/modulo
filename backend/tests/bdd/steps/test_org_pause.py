"""BDD step definitions: org-wide "pause all pipeline triggers" kill-switch.

Asserts STATUS/BODY ONLY — BDD steps never assert TriggerEvent rows (row
assertions against the mocked session are silent no-ops and would give a false
signal).
"""

import contextlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

from tests.bdd.conftest import ORG_ID
from tests.bdd.steps.test_alpha_triggers import _post_webhook

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/triggers/pause.feature")


@given(parsers.parse('org "{org}" has trigger "{name}" with webhook secret "{secret}"'))
def org_has_trigger(org: str, name: str, secret: str, request) -> None:
    request.node._trigger_name = name
    request.node._webhook_secret = secret


def _use_viewer_auth() -> None:
    """Set the dependency override for viewer auth (overrides client fixture)."""
    from modulo.api.main import app as _app
    from modulo.auth.dependencies import get_current_user as _get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal as _Principal

    _app.dependency_overrides[_get_current_user] = lambda: _Principal(
        username="viewer",
        organisation_id=ORG_ID,
        account_id=uuid.uuid4(),
        org_role="viewer",
    )


def _put_pause(client, request, *, paused: bool, resolve_role: str | None = "admin") -> None:
    org_mock = MagicMock()
    org_mock.id = ORG_ID
    org_mock.triggers_paused = False
    org_mock.triggers_paused_at = None
    with (
        patch("modulo.api.routes.admin_orgs.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=org_mock),
        patch("modulo.api.routes.admin_orgs.append_audit_event", new_callable=AsyncMock),
        patch("modulo.api.dependencies._resolve_live_org_role", new_callable=AsyncMock, return_value=resolve_role),
    ):
        resp = client.put(
            f"/api/v1/admin/orgs/{ORG_ID}/triggers/pause",
            json={"paused": paused},
        )
    request.node._resp = resp


@when(parsers.parse("I PUT /api/v1/admin/orgs/{org}/triggers/pause with paused {paused}"))
def admin_pause_org(org: str, paused: str, client, request) -> None:
    _put_pause(client, request, paused=paused.lower() == "true")


@when(parsers.parse("I PUT /api/v1/admin/orgs/{org}/triggers/pause with paused {paused} as a non-admin"))
def non_admin_pause_org(org: str, paused: str, client, request) -> None:
    _use_viewer_auth()
    # The mocked live-role read is a MagicMock -> assert_org_role denies (403)
    # before the handler runs; no need to resolve a role.
    with (
        patch("modulo.api.dependencies._resolve_live_org_role", new_callable=AsyncMock, return_value=None),
    ):
        resp = client.put(
            f"/api/v1/admin/orgs/{ORG_ID}/triggers/pause",
            json={"paused": paused.lower() == "true"},
        )
    request.node._resp = resp


@when(parsers.parse("I POST /api/v1/triggers/{name}/webhook with payload {payload} and org pause is {state}"))
def webhook_pause_state(name: str, payload, state: str, client, request) -> None:
    payload_dict = json.loads(payload) if isinstance(payload, str) else payload
    _post_webhook(client, request, payload_dict, paused=(state == "paused"))


@then("the response body pause state is true")
def pause_state_true(request) -> None:
    data = request.node._resp.json()
    assert data.get("paused") is True, f"Expected paused:true, got {data}"


@then("the webhook is paused")
def webhook_paused(request) -> None:
    data = request.node._resp.json()
    assert data == {"status": "paused"}, f"Expected {{'status': 'paused'}}, got {data}"


@then("the webhook is accepted")
def webhook_accepted(request) -> None:
    data = request.node._resp.json()
    assert data.get("status") == "accepted", f"Expected accepted, got {data}"
