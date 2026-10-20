"""Caller-scoped permission resolution endpoint (FAR-2020).

``GET /api/v1/auth/permissions`` is what the SPA resolves its nav permission
gates from. The access token carries no ``permissions`` claim, so the endpoint
is the ONLY source: the assertions here are computed from ``PERMISSIONS`` and
the role hierarchy rather than hand-written lists, so a registry change is
reflected (and a response that drifts from the registry FAILS) instead of
silently disagreeing with what enforcement would allow.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.routes.auth import _SYSTEM_ONLY_PERMISSIONS
from modulo.api.routes.auth import router as auth_router
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.permissions import PERMISSIONS
from modulo.auth.team_rbac import org_role_level
from modulo.settings import get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


#: The registry names a role resolves to, computed from the SAME registry and
#: hierarchy the endpoint reads — never a hand-maintained list. System-only keys
#: are excluded because no org role can reach them (see the drift test below).
def _registry_names_for(role: str | None) -> list[str]:
    if role is None:
        return []
    level = org_role_level(role)
    return [
        name
        for name, min_role in sorted(PERMISSIONS.items())
        if org_role_level(min_role) <= level and name not in _SYSTEM_ONLY_PERMISSIONS
    ]


def _system_only_keys_from_the_live_app() -> set[str]:
    """Derive the strict-system-only key set from the SHIPPED route graph.

    ``require_system_permission`` stamps ``permission_kind == "system"`` on the
    dependency, so the app itself is the marker — no hand-written list. A key
    that ALSO carries an org-reachable tag (tenant / tenant_or_api_key /
    scoped_hybrid / system_or_org) is subtracted: an org role can reach it, so
    it is not system-only.
    """
    from modulo.api.main import app as live_app
    from tests.unit.api.route_introspection import get_all_apiroutes, get_permission_tag

    system_gated: set[str] = set()
    org_reachable: set[str] = set()
    for route in get_all_apiroutes(live_app):
        tag = get_permission_tag(route)
        if tag is None:
            continue
        for one in tag.get("tags", [tag]):
            bucket = system_gated if one["permission_kind"] == "system" else org_reachable
            bucket.add(one["permission"])
    return system_gated - org_reachable


def _make_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(auth_router)
    return _app


@contextmanager
def _client_with_principal(
    app: FastAPI,
    principal: AuthenticatedPrincipal,
) -> Iterator[TestClient]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    from modulo.auth.dependencies import get_current_user

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_settings] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: principal
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _principal(
    *,
    org_role: str | None,
    is_system_admin: bool = False,
    organisation_id: uuid.UUID | None = _ORG_ID,
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="user@example.com",
        organisation_id=organisation_id,
        account_id=_USER_ID,
        org_role=org_role,
        is_system_admin=is_system_admin,
    )


@pytest.mark.parametrize("role", ["viewer", "admin"])
def test_permissions_are_exactly_the_registry_names_for_the_live_role(
    role: str,
    app: FastAPI,
) -> None:
    """The response IS the registry's role bundle - computed here, not typed out."""
    expected = _registry_names_for(role)

    with (
        _client_with_principal(app, _principal(org_role=role)) as client,
        patch(
            "modulo.api.routes.auth.resolve_role_from_membership",
            new=AsyncMock(return_value=role),
        ),
    ):
        resp = client.get("/api/v1/auth/permissions")

    assert resp.status_code == 200, resp.text
    assert resp.json()["permissions"] == expected
    # A role bundle is never empty (viewer is the floor and holds many keys);
    # this guards the equality above from passing on two empty lists.
    assert expected


def test_live_role_wins_over_the_token_claim(app: FastAPI) -> None:
    """ADR 047: the LIVE membership role decides, not the (stale) token claim."""
    with (
        _client_with_principal(app, _principal(org_role="admin")) as client,
        patch(
            "modulo.api.routes.auth.resolve_role_from_membership",
            new=AsyncMock(return_value="viewer"),
        ),
    ):
        resp = client.get("/api/v1/auth/permissions")

    assert resp.status_code == 200, resp.text
    assert resp.json()["permissions"] == _registry_names_for("viewer")


def test_system_admin_gets_the_admin_floor_without_a_membership(app: FastAPI) -> None:
    """A system admin's strict gates ignore the org hierarchy, so the set is full.

    The floor includes the system-only keys a non-system-admin is never told
    about.
    """
    principal = _principal(org_role=None, is_system_admin=True, organisation_id=None)
    expected = [
        name for name, min_role in sorted(PERMISSIONS.items()) if org_role_level(min_role) <= org_role_level("admin")
    ]

    with _client_with_principal(app, principal) as client:
        resp = client.get("/api/v1/auth/permissions")

    assert resp.status_code == 200, resp.text
    assert resp.json()["permissions"] == expected
    # The system-only keys are exactly what a plain org admin does NOT get, so
    # this equality is only meaningful while the difference is non-empty.
    assert set(expected) >= _SYSTEM_ONLY_PERMISSIONS


def test_unauthenticated_is_denied_not_successful_and_not_a_server_error(app: FastAPI) -> None:
    """Authenticated-only: no credentials must never read the registry.

    Observed contract: FastAPI's bearer dependency answers 401
    ``{"detail": "Not authenticated"}`` for a request with no Authorization
    header. The not-200 / not-500 assertions are the load-bearing ones - they
    pin that an absent credential can neither leak the registry (200) nor turn
    the auth gate into an outage (500).
    """
    resp = TestClient(app).get("/api/v1/auth/permissions")

    assert resp.status_code != 200, resp.text
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 401, resp.text


def test_permissions_are_sorted(app: FastAPI) -> None:
    """Deterministic ordering, so the payload is diff-stable for clients."""
    with (
        _client_with_principal(app, _principal(org_role="operator")) as client,
        patch(
            "modulo.api.routes.auth.resolve_role_from_membership",
            new=AsyncMock(return_value="operator"),
        ),
    ):
        resp = client.get("/api/v1/auth/permissions")

    assert resp.status_code == 200, resp.text
    names = resp.json()["permissions"]
    assert names == sorted(names)


def test_org_admin_is_not_told_they_hold_strict_system_admin_keys(app: FastAPI) -> None:
    """A non-system-admin must not be handed keys their role can never reach.

    ``system.config.manage`` is gated by ``require_system_permission`` (strict
    ``is_system_admin``), so an org admin who is not a system admin would get a
    403 from every route that key guards. Reporting it made the nav render
    surfaces that answer 403 — the over-report this test pins shut.
    """
    with (
        _client_with_principal(app, _principal(org_role="admin")) as client,
        patch(
            "modulo.api.routes.auth.resolve_role_from_membership",
            new=AsyncMock(return_value="admin"),
        ),
    ):
        resp = client.get("/api/v1/auth/permissions")

    assert resp.status_code == 200, resp.text
    names = resp.json()["permissions"]
    assert "system.config.manage" not in names
    assert "system.org.manage" not in names
    assert "errors.resolve_instance" not in names
    # Not an empty/over-stripped list: the admin bundle the org role DOES hold
    # is still reported in full.
    assert "analytics.query" in names
    assert "pipeline.graph.update" in names


def test_viewer_still_holds_the_viewer_floor(app: FastAPI) -> None:
    """The exclusion is scoped to system-only keys — the floor is untouched."""
    with (
        _client_with_principal(app, _principal(org_role="viewer")) as client,
        patch(
            "modulo.api.routes.auth.resolve_role_from_membership",
            new=AsyncMock(return_value="viewer"),
        ),
    ):
        resp = client.get("/api/v1/auth/permissions")

    assert resp.status_code == 200, resp.text
    assert "analytics.query" in resp.json()["permissions"]


def test_system_only_key_set_matches_the_shipped_strict_gates() -> None:
    """Drift gate: the excluded set IS the app's strict-system-only key set.

    Derived from the live route graph rather than typed out, so adding a
    ``require_system_permission("new.key")`` call site — or giving an existing
    one an org-reachable gate — fails here until the endpoint is updated in the
    same change.
    """
    assert _system_only_keys_from_the_live_app() == _SYSTEM_ONLY_PERMISSIONS


def test_removed_member_is_denied_with_401(app: FastAPI) -> None:
    """ADR 047: a removed/deactivated member reads no permissions at all."""
    with (
        _client_with_principal(app, _principal(org_role="admin")) as client,
        patch(
            "modulo.api.routes.auth.resolve_role_from_membership",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = client.get("/api/v1/auth/permissions")

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Organisation membership required"
    assert "permissions" not in resp.json()


def test_unknown_live_role_fails_closed_with_an_empty_list(app: FastAPI) -> None:
    """An unrecognised role matches nothing rather than defaulting to a floor."""
    with (
        _client_with_principal(app, _principal(org_role="viewer")) as client,
        patch(
            "modulo.api.routes.auth.resolve_role_from_membership",
            new=AsyncMock(return_value="wizard"),
        ),
    ):
        resp = client.get("/api/v1/auth/permissions")

    assert resp.status_code == 200, resp.text
    assert not resp.json()["permissions"]
