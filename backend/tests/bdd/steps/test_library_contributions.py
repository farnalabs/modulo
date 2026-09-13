"""Step definitions for the library fixture-contribution flow (contribute.feature).

The contribution REST surface (``backend/src/modulo/api/routes/contributions.py``)
is exercised end-to-end through the real routes with only the DB service functions
patched: the draft -> review_queue -> published state machine, the admin-only
``contribution.publish`` role gate (403 for viewers), the version-bump flow
(201 draft) and its draft-original 409 refusal, plus the contribution list and
version-list surfaces.

Closes the ``feat-library`` contribution BDD gap recorded in
``docs/product-map/library/library.md``: ``contribute.feature`` previously shipped
unregistered (no ``scenarios(...)`` call), so the contribution behaviour was
unit-tested only (``tests/unit/library_service/test_contribution_flow.py``).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.library_service import ContributionInvalidTransitionError
from modulo.db.crud.base import PageResult
from tests.bdd.conftest import ORG_ID, USER_ID, _active_client, _store_response

scenarios("../features/library/contribute.feature")


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context across Given / When / Then steps."""
    return {"response": None, "contrib_id": None}


def _mk_contribution(
    *, status: str, visibility: str, name: str = "My Test Fixture", slug: str = "my-test-fixture"
) -> Any:
    """Build the service-returned primitive for the create/submit/publish/version routes.

    The routes read ``id`` / ``name`` / ``slug`` / ``contribution_status`` /
    ``visibility`` off the returned object before shaping the pydantic response.
    """
    prim = MagicMock()
    prim.id = uuid.uuid4()
    prim.name = name
    prim.slug = slug
    prim.contribution_status = status
    prim.visibility = visibility
    return prim


def _contribution_item(*, name: str, slug: str) -> dict[str, Any]:
    """A ``contribution`` row shaped like ``LibraryPrimitiveResponse`` (list surface)."""
    now = datetime(2025, 1, 1, tzinfo=UTC).isoformat()
    return {
        "id": str(uuid.uuid4()),
        "organisation_id": str(ORG_ID),
        "source": "local",
        "primitive_type": "schema",
        "name": name,
        "slug": slug,
        "description": None,
        "author": "testuser",
        "version": "1.0",
        "tags": [],
        "content_json": {},
        "source_url": None,
        "forked_from": None,
        "checksum": None,
        "ed25519_signature": None,
        "verified": None,
        "download_count": None,
        "average_rating": None,
        "review_count": None,
        "owner_team_id": None,
        "visibility": "org",
        "created_at": now,
        "updated_at": now,
    }


def _mk_version(*, version: str, status: str = "draft") -> Any:
    v = MagicMock()
    v.id = uuid.uuid4()
    v.version = version
    v.contribution_status = status
    v.name = "My Test Fixture"
    v.slug = "my-test-fixture"
    v.account_id = USER_ID
    return v


# ===========================================================================
# Given
# ===========================================================================


@given("the user is authenticated")
def _user_authenticated(request: pytest.FixtureRequest, client: Any) -> None:
    """Background step — the default admin TestClient provides the principal."""
    request.node._client = client


@given("the user is an org admin")
def _user_org_admin(request: pytest.FixtureRequest, client: Any) -> None:
    request.node._client = client


@given("the user is a viewer")
def _user_viewer(request: pytest.FixtureRequest, viewer_client: Any) -> None:
    request.node._client = viewer_client


def _seed_contribution(ctx: dict[str, Any], *, status: str, visibility: str) -> None:
    ctx["contrib_id"] = uuid.uuid4()
    ctx["contrib_status"] = status
    ctx["contrib_visibility"] = visibility


@given("a draft fixture contribution exists")
def _draft_contribution_exists(ctx: dict[str, Any]) -> None:
    _seed_contribution(ctx, status="draft", visibility="org")


@given("a reviewed fixture contribution exists")
def _reviewed_contribution_exists(ctx: dict[str, Any]) -> None:
    _seed_contribution(ctx, status="review_queue", visibility="org")


@given("a published fixture contribution exists")
def _published_contribution_exists(ctx: dict[str, Any]) -> None:
    _seed_contribution(ctx, status="published", visibility="community")


@given("a published fixture contribution with versions exists")
def _published_contribution_with_versions_exists(ctx: dict[str, Any]) -> None:
    _seed_contribution(ctx, status="published", visibility="community")
    ctx["contrib_versions"] = [
        _mk_version(version="1.0", status="published"),
        _mk_version(version="1.1"),
    ]


# ===========================================================================
# When
# ===========================================================================


@when("the user submits a contribution")
def _create_contribution(docstring: str, ctx: dict[str, Any], request: pytest.FixtureRequest) -> None:
    body = json.loads(docstring or "{}")
    client = _active_client(request)
    prim = _mk_contribution(
        status="draft",
        visibility="org",
        name=body.get("name", "My Test Fixture"),
        slug=body.get("slug", "my-test-fixture"),
    )
    with (
        patch("modulo.api.routes.contributions.contribute_fixture", new=AsyncMock(return_value=prim)),
        patch("modulo.api.routes.contributions.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.contributions.set_rls_user_context", new=AsyncMock()),
    ):
        resp = client.post("/api/v1/library/contribute", json=body)
    _store_response(request, ctx, resp)


@when("the user submits the contribution for review")
def _submit_contribution(ctx: dict[str, Any], request: pytest.FixtureRequest) -> None:
    client = _active_client(request)
    contrib_id = ctx["contrib_id"]
    if ctx["contrib_status"] == "draft":
        prim = _mk_contribution(status="review_queue", visibility="org")
        submit = AsyncMock(return_value=prim)
    else:
        submit = AsyncMock(side_effect=ContributionInvalidTransitionError("cannot submit a non-draft contribution"))
    with (
        patch("modulo.api.routes.contributions.submit_contribution_for_review", new=submit),
        patch("modulo.api.routes.contributions.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.contributions.set_rls_user_context", new=AsyncMock()),
    ):
        resp = client.post(f"/api/v1/library/contribute/{contrib_id}/submit")
    _store_response(request, ctx, resp)


@when("the user publishes the contribution")
def _publish_contribution(ctx: dict[str, Any], request: pytest.FixtureRequest) -> None:
    client = _active_client(request)
    contrib_id = ctx["contrib_id"]
    prim = _mk_contribution(status="published", visibility="community")
    with (
        patch("modulo.api.routes.contributions.publish_contribution", new=AsyncMock(return_value=prim)),
        patch("modulo.api.routes.contributions.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.contributions.set_rls_user_context", new=AsyncMock()),
    ):
        resp = client.post(f"/api/v1/library/contribute/{contrib_id}/publish")
    _store_response(request, ctx, resp)


@when("the user submits a new version of the contribution")
def _submit_contribution_version(
    docstring: str,
    ctx: dict[str, Any],
    request: pytest.FixtureRequest,
) -> None:
    body = json.loads(docstring or "{}")
    client = _active_client(request)
    contrib_id = ctx["contrib_id"]
    if ctx["contrib_status"] == "published":
        prim = _mk_contribution(
            status="draft",
            visibility="org",
            name=body.get("name", "Updated Fixture"),
            slug=body.get("slug", "updated-fixture"),
        )
        version = AsyncMock(return_value=prim)
    else:
        version = AsyncMock(side_effect=ContributionInvalidTransitionError("cannot version a draft contribution"))
    with (
        patch("modulo.api.routes.contributions.submit_contribution_version", new=version),
        patch("modulo.api.routes.contributions.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.contributions.set_rls_user_context", new=AsyncMock()),
    ):
        resp = client.post(f"/api/v1/library/contribute/{contrib_id}/versions", json=body)
    _store_response(request, ctx, resp)


@when("the user lists the contributions")
def _list_contributions(client: Any, ctx: dict[str, Any], request: pytest.FixtureRequest) -> None:
    items = [
        _contribution_item(name="My Test Fixture", slug="my-test-fixture"),
        _contribution_item(name="Published Fixture", slug="published-fixture"),
    ]
    result = PageResult(items=items, total=len(items), page=1, page_size=20)
    with (
        patch("modulo.api.routes.contributions.list_contributions", new=AsyncMock(return_value=result)),
        patch("modulo.api.routes.contributions.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.contributions.set_rls_user_context", new=AsyncMock()),
    ):
        resp = client.get("/api/v1/library/contribute")
    _store_response(request, ctx, resp)


@when(parsers.parse('the user lists the contributions filtered by status "{status}"'))
def _list_contributions_filtered(
    client: Any,
    status: str,
    ctx: dict[str, Any],
    request: pytest.FixtureRequest,
) -> None:
    items = [_contribution_item(name="My Test Fixture", slug="my-test-fixture")]
    ctx["list_filter"] = status

    async def _filtered(  # type: ignore[no-untyped-def]
        session: Any,
        organisation_id: Any,
        **kwargs: Any,
    ) -> PageResult:
        assert kwargs.get("contribution_status") == status, f"filter not forwarded, got {kwargs}"
        return PageResult(items=items, total=len(items), page=1, page_size=20)

    with (
        patch("modulo.api.routes.contributions.list_contributions", new=_filtered),
        patch("modulo.api.routes.contributions.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.contributions.set_rls_user_context", new=AsyncMock()),
    ):
        resp = client.get("/api/v1/library/contribute", params={"contribution_status": status})
    _store_response(request, ctx, resp)


@when("the user lists the contribution versions")
def _list_contribution_versions(
    client: Any,
    ctx: dict[str, Any],
    request: pytest.FixtureRequest,
) -> None:
    versions = ctx.get("contrib_versions") or [_mk_version(version="1.0", status="published")]
    with (
        patch("modulo.api.routes.contributions.list_contribution_versions", new=AsyncMock(return_value=versions)),
        patch("modulo.api.routes.contributions.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.contributions.set_rls_user_context", new=AsyncMock()),
    ):
        resp = client.get(f"/api/v1/library/contribute/{ctx['contrib_id']}/versions")
    _store_response(request, ctx, resp)


# ===========================================================================
# Then
# ===========================================================================


@then(parsers.parse('the response has contribution_status "{expected}"'))
def _response_has_contribution_status(ctx: dict[str, Any], expected: str) -> None:
    data = ctx["response"].json()
    assert data.get("contribution_status") == expected, (
        f"Expected contribution_status '{expected}', got {data.get('contribution_status')!r}"
    )


@then(parsers.parse('the response has visibility "{expected}"'))
def _response_has_visibility(ctx: dict[str, Any], expected: str) -> None:
    data = ctx["response"].json()
    assert data.get("visibility") == expected, f"Expected visibility '{expected}', got {data.get('visibility')!r}"


@then(parsers.parse('the new version has contribution_status "{expected}"'))
def _new_version_has_contribution_status(ctx: dict[str, Any], expected: str) -> None:
    data = ctx["response"].json()
    assert data.get("contribution_status") == expected, (
        f"Expected new-version contribution_status '{expected}', got {data.get('contribution_status')!r}"
    )


@then("the response contains a list of contributions")
def _response_contains_contributions(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "items" in data, f"Response missing 'items', got {data}"
    assert isinstance(data["items"], list), "Response 'items' should be a list"
    assert data["items"], "Expected at least one contribution"


@then("the response contains only draft contributions")
def _response_only_draft_contributions(ctx: dict[str, Any]) -> None:
    assert ctx.get("list_filter") == "draft", f"status filter not forwarded, got {ctx.get('list_filter')!r}"
    data = ctx["response"].json()
    names = [item.get("name") for item in data["items"]]
    assert "My Test Fixture" in names, f"Expected the draft contribution, got {names}"
    assert "Published Fixture" not in names, f"Draft-only response must exclude published items, got {names}"


@then("the response contains a list of versions")
def _response_contains_versions(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert "versions" in data, f"Response missing 'versions', got {data}"
    assert isinstance(data["versions"], list), "Response 'versions' should be a list"
    assert data["versions"], "Expected at least one version"
