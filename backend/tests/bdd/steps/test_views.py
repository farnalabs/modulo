"""Step definitions for Saved View CRUD feature."""

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/views/views.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
VIEW_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


def _make_view(**overrides: Any) -> MagicMock:
    v = MagicMock()
    v.id = overrides.get("id", VIEW_ID)
    v.organisation_id = overrides.get("organisation_id", ORG_ID)
    v.name = overrides.get("name", "Test View")
    v.description = overrides.get("description")
    v.view_type = overrides.get("view_type", "run_list")
    v.filters = overrides.get("filters", {})
    v.columns = overrides.get("columns")
    v.sort_by = overrides.get("sort_by")
    v.sort_order = overrides.get("sort_order", "desc")
    v.created_by = overrides.get("created_by", USER_ID)
    v.created_at = _NOW
    v.updated_at = _NOW
    return v


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("I am authenticated as an admin user")
def _auth_admin() -> None:
    """No-op — the client fixture already provides an admin principal."""


@given(parsers.parse('a saved view exists with name "{name}"'))
def _view_exists_with_name(name: str, ctx: dict[str, Any]) -> None:
    ctx["view_id"] = VIEW_ID
    ctx["view_name"] = name
    ctx["view"] = _make_view(name=name)


@given("a saved view exists")
def _view_exists(ctx: dict[str, Any]) -> None:
    ctx["view_id"] = VIEW_ID
    ctx["view_name"] = "My View"
    ctx["view"] = _make_view()


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('I POST /api/v1/views with name "{name}" and type "{view_type}"'))
def _create_view(name: str, view_type: str, ctx: dict[str, Any], client: Any) -> None:
    view = _make_view(name=name, view_type=view_type)
    with (
        patch("modulo.api.routes.views.create_view", new_callable=AsyncMock, return_value=view),
        patch("modulo.api.routes.views.set_rls_org"),
        patch("modulo.api.routes.views.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/views", json={"name": name, "view_type": view_type})
    ctx["response"] = resp
    ctx["view_id"] = view.id
    ctx["view"] = view


@when("I GET /api/v1/views")
def _list_views(ctx: dict[str, Any], client: Any) -> None:
    page_result = MagicMock(items=[ctx.get("view", _make_view())], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.views.list_views", return_value=page_result),
        patch("modulo.api.routes.views.set_rls_org"),
        patch("modulo.api.routes.views.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/views")
    ctx["response"] = resp


@when(parsers.parse("I GET /api/v1/views/{view_id}"))
def _get_view(view_id: str, ctx: dict[str, Any], client: Any) -> None:
    if view_id == "00000000-0000-0000-0000-000000000000":
        actual_id = uuid.UUID(view_id)
        mock_return = None
    else:
        actual_id = ctx.get("view_id", VIEW_ID)
        mock_return = ctx.get("view", _make_view())
    with (
        patch("modulo.api.routes.views.get_view", return_value=mock_return),
        patch("modulo.api.routes.views.set_rls_org"),
        patch("modulo.api.routes.views.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/views/{actual_id}")
    ctx["response"] = resp


@when(parsers.parse('I PATCH /api/v1/views/{view_id} with name "{name}"'))
def _update_view(name: str, ctx: dict[str, Any], client: Any) -> None:
    view_id = ctx.get("view_id", VIEW_ID)
    updated = _make_view(name=name)
    with (
        patch("modulo.api.routes.views.update_view", return_value=updated),
        patch("modulo.api.routes.views.set_rls_org"),
        patch("modulo.api.routes.views.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/views/{view_id}", json={"name": name})
    ctx["response"] = resp


@when(parsers.parse("I DELETE /api/v1/views/{view_id}"))
def _delete_view(ctx: dict[str, Any], client: Any) -> None:
    view_id = ctx.get("view_id", VIEW_ID)
    with (
        patch("modulo.api.routes.views.delete_view", return_value=True),
        patch("modulo.api.routes.views.set_rls_org"),
        patch("modulo.api.routes.views.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/views/{view_id}")
    ctx["response"] = resp


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse("the response status is {status:d}"))
def _check_response_status(status: int, ctx: dict[str, Any]) -> None:
    resp = ctx.get("response")
    assert resp is not None, "No response stored in context"
    assert resp.status_code == status, f"Expected status {status}, got {resp.status_code}: {resp.text[:200]}"


@then(parsers.parse('the response contains a view with name "{expected}"'))
def _response_contains_view_name(expected: str, ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert data["name"] == expected, f"Expected view name {expected!r}, got {data['name']!r}"


@then("the response contains a list of views")
def _response_contains_view_list(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "items" in data, "Response missing 'items' field"
    assert isinstance(data["items"], list), "Response 'items' is not a list"
    assert "total" in data, "Response missing 'total' field"


@then(parsers.parse('the view name is "{expected}"'))
def _view_name_is(expected: str, ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert data["name"] == expected, f"Expected name {expected!r}, got {data['name']!r}"
