"""BDD step definitions: System admin flows — orgs, users, config."""

import contextlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Register feature files
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/system_admin/system_admin_orgs.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/system_admin/system_admin_users.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/system_admin/system_admin_config.feature")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


# ===========================================================================
# Given steps
# ===========================================================================


@given("I am authenticated as a system admin")
def _auth_system_admin(request):
    request.node._is_system_admin = True


@given("I am authenticated as an org admin")
def _auth_org_admin(request):
    request.node._is_system_admin = False


@given(parsers.parse('I am authenticated as an org admin in org "{org}"'))
def _auth_org_admin_in_org(org: str, request):
    request.node._is_system_admin = False


@given(parsers.parse('an organisation with slug "{slug}" already exists'))
def _org_slug_taken(slug: str, request):
    request.node._org_slug_taken = slug


@given(parsers.parse('an organisation "{slug}" exists'))
def _org_exists(slug: str, request):
    org_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, slug)
    request.node._org_uuid = org_uuid
    request.node._org_slug = slug


@given("the organisation holds its own license key")
def _org_holds_license(request):
    request.node._org_has_key = True


@given("the organisation holds an invalid license key")
def _org_holds_invalid_license(request):
    request.node._org_has_invalid_key = True


# ===========================================================================
# Helpers
# ===========================================================================


def _set_auth_override(is_system_admin: bool):
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal

    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="sysadmin" if is_system_admin else "testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=is_system_admin,
    )


def _make_mock_org(**kwargs):
    org = MagicMock()
    org.id = kwargs.get("id", uuid.uuid4())
    org.name = kwargs.get("name", "Test Org")
    org.slug = kwargs.get("slug", "test-org")
    org.status = kwargs.get("status", "active")
    org.created_at = kwargs.get("created_at", datetime.now(UTC))
    return org


def _make_mock_account(**kwargs):
    account = MagicMock()
    account.id = kwargs.get("id", uuid.uuid4())
    account.email = kwargs.get("email", "test@test.com")
    account.display_name = kwargs.get("display_name", "Test User")
    account.auth_provider = kwargs.get("auth_provider", "internal")
    account.created_at = kwargs.get("created_at", datetime.now(UTC))
    return account


def _make_mock_membership(**kwargs):
    membership = MagicMock()
    membership.role = kwargs.get("role", "runner")
    return membership


def _make_mock_config_entry(**kwargs):
    entry = MagicMock()
    entry.key = kwargs.get("key", "default_plan")
    entry.value = kwargs.get("value", "team")
    entry.updated_at = kwargs.get("updated_at", datetime.now(UTC))
    return entry


# ===========================================================================
# When steps — Orgs
# ===========================================================================


@when(parsers.parse('I create an organisation with name "{name}" and slug "{slug}"'))
def _create_org(name: str, slug: str, request, client):
    is_system_admin = getattr(request.node, "_is_system_admin", False)
    _set_auth_override(is_system_admin)

    mock_org = _make_mock_org(name=name, slug=slug, status="active")

    with (
        patch("modulo.api.routes.admin_orgs.get_organisation_by_slug", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.admin_orgs.create_organisation", new_callable=AsyncMock, return_value=mock_org),
    ):
        resp = client.post("/api/v1/admin/orgs", json={"name": name, "slug": slug})
    request.node._resp = resp


@when(parsers.parse('I attempt to create an organisation with name "{name}" and slug "{slug}"'))
def _attempt_create_org(name: str, slug: str, request, client):
    resp = client.post("/api/v1/admin/orgs", json={"name": name, "slug": slug})
    request.node._resp = resp


@when(parsers.parse('I attempt to create an organisation with slug "{slug}"'))
def _attempt_create_org_duplicate(slug: str, request, client):
    _set_auth_override(True)

    existing_org = _make_mock_org(slug=slug)
    with patch(
        "modulo.api.routes.admin_orgs.get_organisation_by_slug", new_callable=AsyncMock, return_value=existing_org
    ):
        resp = client.post("/api/v1/admin/orgs", json={"name": "Temp Org", "slug": slug})
    request.node._resp = resp


@when("I delete the organisation")
def _delete_org(request, client):
    _set_auth_override(True)

    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    mock_org = _make_mock_org(id=org_id, slug=getattr(request.node, "_org_slug", "test-org"))

    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=mock_org),
        patch("modulo.api.routes.admin_orgs.delete_organisation", new_callable=AsyncMock, return_value=True),
    ):
        resp = client.delete(f"/api/v1/admin/orgs/{org_id}")
    request.node._resp = resp


@when("I delete a missing organisation")
def _delete_missing_org(request, client):
    _set_auth_override(True)

    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.admin_orgs.delete_organisation", new_callable=AsyncMock, return_value=False),
    ):
        resp = client.delete(f"/api/v1/admin/orgs/{org_id}")
    request.node._resp = resp


@when("I attempt to delete the organisation")
def _attempt_delete_org(request, client):
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    resp = client.delete(f"/api/v1/admin/orgs/{org_id}")
    request.node._resp = resp


# ===========================================================================
# When steps — Org license management
# ===========================================================================


def _system_license():
    return SimpleNamespace(tier="team", features=["sso"], expires_at=None, org_id=None)


@when("I view the organisation's license")
def _view_org_license(request, client):
    _set_auth_override(True)
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    org = _make_mock_org(id=org_id, slug=getattr(request.node, "_org_slug", "test-org"))

    if getattr(request.node, "_org_has_invalid_key", False):
        org.settings_json = {"license_key": "expired.key"}
        validation = SimpleNamespace(valid=False, error="Invalid license key", license_data=None)
        with (
            patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=org),
            patch("modulo.core.license.parse_and_verify", return_value=validation),
            patch("modulo.core.license.get_license", return_value=_system_license()),
        ):
            resp = client.get(f"/api/v1/admin/orgs/{org_id}/license")
    elif getattr(request.node, "_org_has_key", False):
        org.settings_json = {"license_key": "org.license.key"}
        validation = SimpleNamespace(
            valid=True,
            error=None,
            license_data=SimpleNamespace(
                tier="enterprise", features=["sso", "audit"], expires_at=None, org_id=str(org_id)
            ),
        )
        with (
            patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=org),
            patch("modulo.core.license.parse_and_verify", return_value=validation),
            patch("modulo.core.license.get_license", return_value=None),
        ):
            resp = client.get(f"/api/v1/admin/orgs/{org_id}/license")
    else:
        org.settings_json = {}
        with (
            patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=org),
            patch("modulo.core.license.get_license", return_value=_system_license()),
        ):
            resp = client.get(f"/api/v1/admin/orgs/{org_id}/license")
    request.node._resp = resp


@when("I view a missing organisation's license")
def _view_license_missing_org(request, client):
    _set_auth_override(True)
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=None),
        patch("modulo.core.license.get_license", return_value=None),
    ):
        resp = client.get(f"/api/v1/admin/orgs/{org_id}/license")
    request.node._resp = resp


@when(parsers.parse('I set a valid license key "{key}" on the organisation'))
def _set_valid_org_license(key: str, request, client):
    _set_auth_override(True)
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    org = _make_mock_org(id=org_id, slug=getattr(request.node, "_org_slug", "test-org"))
    org.settings_json = {"existing": True}
    validation = SimpleNamespace(
        valid=True,
        error=None,
        license_data=SimpleNamespace(tier="team", features=["sso"], expires_at=None, org_id="test-org"),
    )
    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=org),
        patch(
            "modulo.api.routes.admin_orgs.update_organisation", new_callable=AsyncMock, return_value=org
        ) as mock_update,
        patch("modulo.core.license.parse_and_verify", return_value=validation),
    ):
        resp = client.put(f"/api/v1/admin/orgs/{org_id}/license", json={"license_key": key})
    request.node._resp = resp
    request.node._update_org = mock_update


@when(parsers.parse('I set an invalid license key "{key}" on the organisation'))
def _set_invalid_org_license(key: str, request, client):
    _set_auth_override(True)
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    org = _make_mock_org(id=org_id, slug=getattr(request.node, "_org_slug", "test-org"))
    org.settings_json = {}
    validation = SimpleNamespace(valid=False, error="Invalid license key", license_data=None)
    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=org),
        patch(
            "modulo.api.routes.admin_orgs.update_organisation", new_callable=AsyncMock, return_value=org
        ) as mock_update,
        patch("modulo.core.license.parse_and_verify", return_value=validation),
    ):
        resp = client.put(f"/api/v1/admin/orgs/{org_id}/license", json={"license_key": key})
    request.node._resp = resp
    request.node._update_org = mock_update


@when(parsers.parse('I set a valid license key "{key}" on a missing organisation'))
def _set_license_missing_org(key: str, request, client):
    _set_auth_override(True)
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=None),
        patch(
            "modulo.api.routes.admin_orgs.update_organisation", new_callable=AsyncMock, return_value=None
        ) as mock_update,
        patch("modulo.core.license.parse_and_verify"),
    ):
        resp = client.put(f"/api/v1/admin/orgs/{org_id}/license", json={"license_key": key})
    request.node._resp = resp
    request.node._update_org = mock_update


@when("I remove the organisation's license")
def _remove_org_license(request, client):
    _set_auth_override(True)
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    org = _make_mock_org(id=org_id, slug=getattr(request.node, "_org_slug", "test-org"))
    org.settings_json = {"license_key": "old.key"} if getattr(request.node, "_org_has_key", False) else {"other": 1}
    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=org),
        patch(
            "modulo.api.routes.admin_orgs.update_organisation", new_callable=AsyncMock, return_value=org
        ) as mock_update,
    ):
        resp = client.delete(f"/api/v1/admin/orgs/{org_id}/license")
    request.node._resp = resp
    request.node._update_org = mock_update


@when("I remove a missing organisation's license")
def _remove_license_missing_org(request, client):
    _set_auth_override(True)
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=None),
        patch(
            "modulo.api.routes.admin_orgs.update_organisation", new_callable=AsyncMock, return_value=None
        ) as mock_update,
    ):
        resp = client.delete(f"/api/v1/admin/orgs/{org_id}/license")
    request.node._resp = resp
    request.node._update_org = mock_update


@when("I attempt to view the organisation's license")
def _attempt_view_org_license(request, client):
    _set_auth_override(False)
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    with patch("modulo.api.dependencies._resolve_live_org_role", new_callable=AsyncMock, return_value=None):
        resp = client.get(f"/api/v1/admin/orgs/{org_id}/license")
    request.node._resp = resp


@when(parsers.parse('I attempt to set a license "{key}" on the organisation'))
def _attempt_set_org_license(key: str, request, client):
    _set_auth_override(False)
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    with patch("modulo.api.dependencies._resolve_live_org_role", new_callable=AsyncMock, return_value=None):
        resp = client.put(f"/api/v1/admin/orgs/{org_id}/license", json={"license_key": key})
    request.node._resp = resp


@when("I attempt to remove the organisation's license")
def _attempt_remove_org_license(request, client):
    _set_auth_override(False)
    org_id = getattr(request.node, "_org_uuid", uuid.uuid4())
    with patch("modulo.api.dependencies._resolve_live_org_role", new_callable=AsyncMock, return_value=None):
        resp = client.delete(f"/api/v1/admin/orgs/{org_id}/license")
    request.node._resp = resp


# ===========================================================================
# When steps — Users
# ===========================================================================


@when(parsers.parse('I create a user with email "{email}" in org "{org_slug}"'))
def _create_org_user(email: str, org_slug: str, request, client):
    _set_auth_override(True)

    org_id = uuid.uuid5(uuid.NAMESPACE_DNS, org_slug)
    mock_org = _make_mock_org(slug=org_slug, id=org_id)
    mock_account = _make_mock_account(email=email)
    mock_membership = _make_mock_membership(role="runner")

    with (
        patch("modulo.api.routes.admin_orgs.get_organisation", new_callable=AsyncMock, return_value=mock_org),
        patch("modulo.api.routes.admin_orgs.get_account_by_email", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.admin_orgs.validate_password_strength"),
        patch("modulo.api.routes.admin_orgs.hash_password", return_value="mock_hash"),
        patch("modulo.api.routes.admin_orgs.create_account", new_callable=AsyncMock, return_value=mock_account),
        patch(
            "modulo.api.routes.admin_orgs.create_membership",
            new_callable=AsyncMock,
            return_value=mock_membership,
        ) as mock_create_membership,
    ):
        resp = client.post(
            f"/api/v1/admin/orgs/{org_id}/users",
            json={
                "email": email,
                "display_name": "New User",
                "password": "password123",
                "org_role": "runner",
            },
        )
    request.node._resp = resp
    request.node._mock_create_membership = mock_create_membership
    request.node._org_uuid = org_id


@when(parsers.parse('I attempt to create a user in org "{org_slug}"'))
def _attempt_create_org_user(org_slug: str, request, client):
    org_id = uuid.uuid5(uuid.NAMESPACE_DNS, org_slug)
    resp = client.post(
        f"/api/v1/admin/orgs/{org_id}/users",
        json={
            "email": "user@test.com",
            "display_name": "User",
            "password": "password123",
            "org_role": "runner",
        },
    )
    request.node._resp = resp


# ===========================================================================
# When steps — Config
# ===========================================================================


@when(parsers.parse('I set system config "{key}" to "{value}"'))
def _set_system_config(key: str, value: str, request, client):
    _set_auth_override(True)

    mock_entry = _make_mock_config_entry(key=key, value=value)

    with patch("modulo.api.routes.admin_system_config.update_config", new_callable=AsyncMock, return_value=mock_entry):
        resp = client.put(f"/api/v1/system-admin/config/{key}", json={"value": value})
    request.node._resp = resp


@when("I list all system config")
def _list_system_config(request, client):
    _set_auth_override(True)

    mock_entries = [
        _make_mock_config_entry(key="default_plan", value="starter"),
        _make_mock_config_entry(key="max_users_per_org", value=50),
    ]

    with patch("modulo.api.routes.admin_system_config.list_config", new_callable=AsyncMock, return_value=mock_entries):
        resp = client.get("/api/v1/system-admin/config")
    request.node._resp = resp


@when("I attempt to list system config")
def _attempt_list_system_config(request, client):
    resp = client.get("/api/v1/system-admin/config")
    request.node._resp = resp


@when(parsers.parse('I delete system config "{key}"'))
def _delete_system_config(key: str, request, client):
    _set_auth_override(True)

    with patch("modulo.api.routes.admin_system_config.delete_config", new_callable=AsyncMock, return_value=True):
        resp = client.delete(f"/api/v1/system-admin/config/{key}")
    request.node._resp = resp


@when(parsers.parse('I delete a missing config key "{key}"'))
def _delete_missing_system_config(key: str, request, client):
    _set_auth_override(True)

    with patch("modulo.api.routes.admin_system_config.delete_config", new_callable=AsyncMock, return_value=False):
        resp = client.delete(f"/api/v1/system-admin/config/{key}")
    request.node._resp = resp


@when(parsers.parse('I attempt to delete system config "{key}"'))
def _attempt_delete_system_config(key: str, request, client):
    resp = client.delete(f"/api/v1/system-admin/config/{key}")
    request.node._resp = resp


# ===========================================================================
# Then steps
# ===========================================================================


@then("the organisation is created successfully")
def _org_created(request):
    resp = request.node._resp
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert "id" in body
    assert "name" in body
    assert "slug" in body
    assert "status" in body
    assert "created_at" in body


@then("the organisation is deleted")
def _org_deleted(request):
    resp = request.node._resp
    assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text[:200]}"


@then(parsers.parse('it has status "{expected_status}"'))
def _check_org_status(expected_status: str, request):
    body = request.node._resp.json()
    assert body.get("status") == expected_status, f"Expected status {expected_status!r}, got {body.get('status')!r}"


@then("the organisation license falls back to the system license")
def _license_system_fallback(request):
    resp = request.node._resp
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert body.get("has_license") is True, f"expected has_license, got {body}"
    assert body.get("tier") == "team", f"expected system tier 'team', got {body.get('tier')!r}"
    assert "sso" in body.get("features", []), f"expected sso feature, got {body.get('features')!r}"


@then("the organisation license is resolved from its own key")
def _license_resolved_from_org_key(request):
    resp = request.node._resp
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert body.get("has_license") is True, f"expected has_license, got {body}"
    assert body.get("tier") == "enterprise", f"expected org-key tier 'enterprise', got {body.get('tier')!r}"
    assert "sso" in body.get("features", []), f"expected sso feature, got {body.get('features')!r}"
    assert "audit" in body.get("features", []), f"expected audit feature, got {body.get('features')!r}"
    assert body.get("org_id") == str(request.node._org_uuid), f"expected org-scoped key, got {body}"


@then(parsers.parse('the organisation license is set to "{key}"'))
def _license_set(key: str, request):
    resp = request.node._resp
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert body.get("has_license") is True, f"expected has_license, got {body}"
    assert body.get("tier") == "team", f"expected tier 'team', got {body.get('tier')!r}"
    update_mock = getattr(request.node, "_update_org", None)
    assert update_mock is not None, "update_organisation was not called"
    assert update_mock.call_count == 1, f"expected one write, got {update_mock.call_count}"
    settings = update_mock.call_args.args[2]["settings_json"]
    assert settings.get("license_key") == key, f"expected license_key {key!r}, got {settings!r}"
    assert settings.get("existing") is True, "pre-existing settings keys must be preserved alongside the key"


@then("the organisation license is removed")
def _license_removed(request):
    resp = request.node._resp
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert body.get("has_license") is False, f"expected has_license False, got {body}"
    update_mock = getattr(request.node, "_update_org", None)
    assert update_mock is not None, "update_organisation was not called"
    assert update_mock.call_count == 1, f"expected one write, got {update_mock.call_count}"
    settings = update_mock.call_args.args[2]["settings_json"]
    assert "license_key" not in settings, f"expected license_key to be cleared, got {settings!r}"


@then(parsers.parse("I receive a {status:d} {error_type} error"))
def _check_error(status: int, error_type: str, request):
    resp = request.node._resp
    assert resp.status_code == status, f"Expected {status}, got {resp.status_code}: {resp.text[:200]}"


@then("the user is created successfully")
def _user_created(request):
    resp = request.node._resp
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert "id" in body
    assert "email" in body
    assert "org_role" in body


@then(parsers.parse('the user belongs to org "{org_slug}"'))
def _user_belongs_to_org(org_slug: str, request):
    expected_org_id = uuid.uuid5(uuid.NAMESPACE_DNS, org_slug)
    mock_create_membership = getattr(request.node, "_mock_create_membership", None)
    assert mock_create_membership is not None, "No create_membership mock captured"
    mock_create_membership.assert_called_once()
    _, kwargs = mock_create_membership.call_args
    assert kwargs["org_id"] == expected_org_id, f"Expected org_id {expected_org_id}, got {kwargs['org_id']}"


@then("the config value is saved")
def _config_saved(request):
    resp = request.node._resp
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert "key" in body
    assert "value" in body


@then("the config entry is deleted")
def _config_deleted(request):
    resp = request.node._resp
    assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text[:200]}"


@then("I see all configured keys and values")
def _config_listed(request):
    resp = request.node._resp
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert isinstance(body, list), f"Expected list, got {type(body)}"
    assert len(body) >= 2
    assert body[0]["key"] == "default_plan"
    assert body[1]["key"] == "max_users_per_org"
