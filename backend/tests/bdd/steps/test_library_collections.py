"""Step definitions for the library collection install/uninstall/grant lifecycle.

The collection REST surface (``backend/src/modulo/api/routes/library.py``) is
exercised end-to-end through the real routes with the library_service
functions patched: installing a published collection (201 with an
``installed`` install record and a runnability verdict), the error mappings
for a non-published collection (400), an unresolvable pin (422) and a repeat
install (400); uninstall deletion-vs-detach semantics (modified entities are
detached, unmodified ones deleted); and the community-sourced grant gate
(200 when community-sourced / already granted, 400 for local installs, 404
for an unknown install) with ``agents_granted`` reflected in the response.

Closes the ``feat-library-collections`` BDD gap recorded in
``docs/product-map/library/library-collections.md``: collection
install/uninstall/grant previously shipped BDD-free (integration-only via
``backend/tests/integration/test_library_collection_lifecycle.py``), so the
REST orchestration + error-contract surface was untested at the behaviour
layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.library_service.grant import (
    InstallNotFoundError as GrantInstallNotFoundError,
)
from modulo.core.library_service.grant import (
    NotCommunitySourcedError,
)
from modulo.core.library_service.install import (
    CollectionInstallError,
    CollectionNotPublishedError,
    PinResolutionError,
)
from modulo.core.library_service.primitive_types import MAX_COLLECTION_PINS
from modulo.core.library_service.uninstall import InstallNotFoundError as UninstallInstallNotFoundError
from tests.bdd.conftest import ORG_ID, _active_client, _store_response

scenarios("../features/library/library_collections.feature")


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context across Given / When / Then steps."""
    return {"response": None}


def _mk_install(*, community_sourced: bool = False, agents_granted: bool = False) -> MagicMock:
    """Build the service-returned ``CollectionInstall``-shaped object.

    The install/uninstall/grant routes read ``install_id`` / ``collection_id`` /
    ``collection_version`` / ``organisation_id`` / ``status`` /
    ``community_sourced`` / ``agents_granted`` / ``resolved_manifest`` /
    ``connector_checklist`` / ``installed_entities`` / ``created_at`` off the
    object before shaping the pydantic response.
    """
    inst = MagicMock()
    inst.install_id = uuid.uuid4()
    inst.collection_id = uuid.uuid4()
    inst.collection_version = "1.0"
    inst.organisation_id = ORG_ID
    inst.status = "installed"
    inst.community_sourced = community_sourced
    inst.agents_granted = agents_granted
    inst.resolved_manifest = {
        "schemas": {"input-schema": str(uuid.uuid4())},
        "agents": {"test-agent": str(uuid.uuid4())},
        "pipelines": {"main": str(uuid.uuid4())},
    }
    inst.connector_checklist = [{"connector_type_id": "github", "status": "pending"}]
    inst.installed_entities = [
        {"id": str(uuid.uuid4()), "entity_type": "schema"},
        {"id": str(uuid.uuid4()), "entity_type": "agent"},
    ]
    inst.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    return inst


def _uninstall_result(
    *,
    install_id: str,
    deleted: list[dict[str, str]] | None = None,
    detached: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "install_id": install_id,
        "deleted": deleted
        or [
            {"id": str(uuid.uuid4()), "entity_type": "schema"},
            {"id": str(uuid.uuid4()), "entity_type": "agent"},
        ],
        "detached": detached or [],
    }


def _mk_collection_primitive(
    ctx: dict[str, Any],
    *,
    status: str,
    slug: str = "my-collection",
    pins: list[dict[str, str]] | None = None,
) -> MagicMock:
    """Build the ``LibraryPrimitive``-shaped object authoring routes return.

    The create/update/publish routes read ``id`` / ``name`` / ``slug`` /
    ``description`` / ``status`` / ``manifest_pins`` / ``trust_header`` /
    ``created_at`` / ``updated_at`` / ``organisation_id`` / ``primitive_type``
    off the object before shaping the ``CollectionResponse`` or enforcing draft
    state in ``_load_draft_collection``.
    """
    prim = MagicMock()
    prim.id = ctx.get("collection_id") or uuid.uuid4()
    ctx["collection_id"] = prim.id
    prim.organisation_id = ORG_ID
    prim.name = ctx.get("collection_name") or "My Collection"
    prim.slug = slug
    prim.description = "A collection of test primitives"
    prim.status = status
    prim.manifest_pins = pins or [
        {"slug": "input-schema", "version": "1.0"},
        {"slug": "output-schema", "version": "2.0"},
    ]
    prim.trust_header = None
    prim.primitive_type = "library_collection"
    prim.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    prim.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
    return prim


# ===========================================================================
# Route-patch helpers
# ===========================================================================

_ROUTE = "modulo.api.routes.library"


def _start_route_patches(
    patches: list[Any],
    *,
    install_collection: AsyncMock,
    uninstall_collection: AsyncMock | None = None,
    grant_collection_agents: AsyncMock | None = None,
    compute_runnable: AsyncMock | None = None,
) -> None:
    patches.append(patch(f"{_ROUTE}._require_library_collection_flag", new=AsyncMock()))
    patches.append(patch(f"{_ROUTE}._set_rls_context", new=AsyncMock()))
    patches.append(patch(f"{_ROUTE}.install_collection", new=install_collection))
    patches.append(patch(f"{_ROUTE}.compute_runnable", new=compute_runnable or AsyncMock(return_value=True)))
    if uninstall_collection is not None:
        patches.append(patch(f"{_ROUTE}.uninstall_collection", new=uninstall_collection))
    if grant_collection_agents is not None:
        patches.append(patch(f"{_ROUTE}.grant_collection_agents", new=grant_collection_agents))
    for p in patches:
        p.start()


# ===========================================================================
# Given
# ===========================================================================


@given("an org operator is authenticated")
def _operator_auth(request: pytest.FixtureRequest, client: Any) -> None:
    """Background step — the default admin TestClient provides the principal."""
    request.node._client = client


@given("a published library collection exists")
def _collection_exists(ctx: dict[str, Any]) -> None:
    ctx["collection_id"] = uuid.uuid4()
    ctx["install"] = _mk_install()
    ctx["install_id"] = ctx["install"].install_id
    ctx["collection"] = _mk_collection_primitive(ctx, status="published", slug="my-collection")


@given("a draft library collection exists")
def _draft_collection_exists(ctx: dict[str, Any]) -> None:
    ctx["collection_id"] = uuid.uuid4()
    ctx["install"] = _mk_install()
    ctx["install_id"] = ctx["install"].install_id
    ctx["install_error"] = CollectionNotPublishedError(
        "Collection 'Test Collection' must be published before installing (current status: draft)"
    )
    ctx["collection"] = _mk_collection_primitive(ctx, status="draft")


@given("the collection pins do not resolve")
def _collection_pins_unresolved(ctx: dict[str, Any]) -> None:
    ctx["install_error"] = PinResolutionError(
        "Pin 'input-schema@1.0' does not resolve to a visible primitive in this organisation"
    )


@given("the collection is already installed in the organisation")
def _collection_already_installed(ctx: dict[str, Any]) -> None:
    ctx["install_error"] = CollectionInstallError(
        "Collection 'Test Collection' is already installed in this organisation"
    )


@given("the collection is installed")
def _collection_installed(ctx: dict[str, Any]) -> None:
    pass


@given("the install is unknown")
def _install_unknown(ctx: dict[str, Any]) -> None:
    ctx["grant_error"] = GrantInstallNotFoundError(f"Install {ctx['install_id']} not found")


@given("no installed entity has been modified")
def _no_entity_modified(ctx: dict[str, Any]) -> None:
    ctx["uninstall_result"] = _uninstall_result(install_id=str(ctx["install_id"]))


@given("one installed schema has been modified")
def _one_schema_modified(ctx: dict[str, Any]) -> None:
    ctx["uninstall_result"] = _uninstall_result(
        install_id=str(ctx["install_id"]),
        deleted=[
            {"id": str(uuid.uuid4()), "entity_type": "agent"},
            {"id": str(uuid.uuid4()), "entity_type": "pipeline"},
        ],
        detached=[{"id": str(uuid.uuid4()), "entity_type": "schema"}],
    )


@given("the collection is installed from the community")
def _collection_installed_community(ctx: dict[str, Any]) -> None:
    ctx["install"] = _mk_install(community_sourced=True, agents_granted=True)
    ctx["install_id"] = ctx["install"].install_id


@given("the collection is installed from a local source")
def _collection_installed_local(ctx: dict[str, Any]) -> None:
    ctx["grant_error"] = NotCommunitySourcedError(
        "Install is not community-sourced; grant is only available for community-sourced collection installs"
    )


@given("the install already has agent access granted")
def _install_already_granted(ctx: dict[str, Any]) -> None:
    ctx["install"] = _mk_install(community_sourced=True, agents_granted=True)
    ctx["install_id"] = ctx["install"].install_id


# ===========================================================================
# When
# ===========================================================================


@when("the user installs the collection")
def _install_collection(ctx: dict[str, Any], request: pytest.FixtureRequest, patches: list[Any]) -> None:
    client = _active_client(request)
    error = ctx.get("install_error")
    install = AsyncMock(side_effect=error) if error else AsyncMock(return_value=ctx["install"])
    _start_route_patches(patches, install_collection=install)
    resp = client.post(f"/api/v1/libraries/collections/{ctx['collection_id']}/install")
    _store_response(request, ctx, resp)


@when("the user uninstalls the collection")
def _uninstall_collection(ctx: dict[str, Any], request: pytest.FixtureRequest, patches: list[Any]) -> None:
    client = _active_client(request)
    result = ctx.get("uninstall_result") or _uninstall_result(install_id=str(ctx["install_id"]))
    _start_route_patches(
        patches,
        install_collection=AsyncMock(return_value=ctx["install"]),
        uninstall_collection=AsyncMock(return_value=result),
    )
    resp = client.post(
        f"/api/v1/libraries/collections/{ctx['collection_id']}/uninstall",
        json={"install_id": str(ctx["install_id"])},
    )
    _store_response(request, ctx, resp)


@when("the user uninstalls the collection with an unknown install id")
def _uninstall_collection_unknown(ctx: dict[str, Any], request: pytest.FixtureRequest, patches: list[Any]) -> None:
    client = _active_client(request)
    unknown_id = uuid.uuid4()
    _start_route_patches(
        patches,
        install_collection=AsyncMock(return_value=ctx["install"]),
        uninstall_collection=AsyncMock(
            side_effect=UninstallInstallNotFoundError(f"Install {unknown_id} not found for organisation {ORG_ID}")
        ),
    )
    resp = client.post(
        f"/api/v1/libraries/collections/{ctx['collection_id']}/uninstall",
        json={"install_id": str(unknown_id)},
    )
    _store_response(request, ctx, resp)


@when("the user grants tool access to the installed agents")
def _grant_collection_agents(ctx: dict[str, Any], request: pytest.FixtureRequest, patches: list[Any]) -> None:
    client = _active_client(request)
    error = ctx.get("grant_error")
    grant = AsyncMock(side_effect=error) if error else AsyncMock(return_value=ctx["install"])
    _start_route_patches(
        patches,
        install_collection=AsyncMock(return_value=ctx["install"]),
        grant_collection_agents=grant,
    )
    resp = client.post(f"/api/v1/libraries/collections/{ctx['collection_id']}/installs/{ctx['install_id']}/grant")
    _store_response(request, ctx, resp)


# ===========================================================================
# Then
# ===========================================================================


@then(parsers.parse('the install response has status "{expected}"'))
def _install_response_has_status(ctx: dict[str, Any], expected: str) -> None:
    data = ctx["response"].json()
    assert data.get("status") == expected, f"Expected install status '{expected}', got {data.get('status')!r}"


@then("the install response is runnable")
def _install_response_runnable(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert data.get("runnable") is True, f"Expected runnable True, got {data.get('runnable')!r}"


@then(parsers.parse("the install response has agents_granted {expected}"))
def _install_response_agents_granted(ctx: dict[str, Any], expected: str) -> None:
    data = ctx["response"].json()
    assert data.get("agents_granted") is (expected != "false"), (
        f"Expected agents_granted {expected}, got {data.get('agents_granted')!r}"
    )


@then("the uninstall response lists deleted entities")
def _uninstall_response_deleted(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert data.get("deleted"), f"Expected deleted entities, got {data.get('deleted')!r}"


@then("the uninstall response detaches no entities")
def _uninstall_response_no_detached(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    assert not data.get("detached"), f"Expected no detached entities, got {data.get('detached')!r}"


@then("the uninstall response detaches the modified entity")
def _uninstall_response_detached(ctx: dict[str, Any]) -> None:
    data = ctx["response"].json()
    detached = data.get("detached") or []
    assert any(item.get("entity_type") == "schema" for item in detached), (
        f"Expected the modified schema to be detached, got {detached!r}"
    )


@then(parsers.parse('the error mentions "{text}"'))
def _error_mentions(ctx: dict[str, Any], text: str) -> None:
    """Assert the response error detail mentions ``text`` (case-insensitive)."""
    body = ctx["response"].json()
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    assert text.lower() in str(detail).lower(), f"Expected error to mention {text!r}, got {detail!r}"


# ============================================================================
# Authoring (FAR-760): create / update / publish a collection with manifest pins
#
# The create/update/publish routes under ``/api/v1/libraries/collections`` are
# exercised end-to-end through the real routes with only the feature-flag /
# RLS / CRUD seams patched, so the scenarios assert the actual authoring API
# contract: draft state on create, duplicate-slug 409, draft-only mutation
# (400 for a published collection), and publish-time pin validation (empty
# manifest 422, duplicate pins 422, >MAX_COLLECTION_PINS 422, unknown
# primitive 422, non-draft 400).
# ============================================================================


def _authoring_route_patch_ctx(ctx: dict[str, Any]) -> MagicMock:
    """Return the collection primitive the patched CRUD seams hand back."""
    if "collection" not in ctx:
        ctx["collection"] = _mk_collection_primitive(ctx, status="draft")
    return cast(MagicMock, ctx["collection"])


# --- Authoring Given steps --------------------------------------------------


@given(parsers.parse('the operator authors a collection named "{name}"'))
def _operator_authors_collection(ctx: dict[str, Any], name: str) -> None:
    ctx["collection_name"] = name
    ctx["collection_slug"] = "my-collection"


@given(parsers.parse('a collection with slug "{slug}" already exists'))
def _collection_slug_exists(ctx: dict[str, Any], slug: str) -> None:
    ctx["existing_slug"] = slug
    ctx["slug_conflict"] = True


@given("the collection pins resolve to known primitives")
def _collection_pins_known(ctx: dict[str, Any]) -> None:
    ctx["pin_lookup"] = "known"


@given("the collection has an empty manifest")
def _collection_manifest_empty(ctx: dict[str, Any]) -> None:
    prim = _authoring_route_patch_ctx(ctx)
    prim.manifest_pins = []


@given("the collection pins are duplicated")
def _collection_pins_duplicated(ctx: dict[str, Any]) -> None:
    prim = _authoring_route_patch_ctx(ctx)
    prim.manifest_pins = [
        {"slug": "input-schema", "version": "1.0"},
        {"slug": "input-schema", "version": "1.0"},
    ]


@given("the collection has more than 25 pins")
def _collection_pins_over_cap(ctx: dict[str, Any]) -> None:
    prim = _authoring_route_patch_ctx(ctx)
    prim.manifest_pins = [
        {"slug": f"schema-{i}", "version": "1.0"} for i in range(MAX_COLLECTION_PINS + 1)
    ]


# --- Authoring When steps ---------------------------------------------------


@when(parsers.parse('the operator creates the collection with pins for "{spec}"'))
def _operator_creates_collection(
    ctx: dict[str, Any], request: pytest.FixtureRequest, patches: list[Any], spec: str
) -> None:
    client = _active_client(request)
    prim = ctx.get("collection") or _mk_collection_primitive(ctx, status="draft")
    existing = None
    if ctx.get("slug_conflict"):
        existing = MagicMock()
        existing.slug = ctx["existing_slug"]
    patches.append(patch(f"{_ROUTE}._require_library_collection_flag", new=AsyncMock()))
    patches.append(patch(f"{_ROUTE}._set_rls_context", new=AsyncMock()))
    patches.append(patch(f"{_ROUTE}.get_primitive_by_slug", new=AsyncMock(return_value=existing)))
    patches.append(patch(f"{_ROUTE}.create_library_primitive", new=AsyncMock(return_value=prim)))
    for p in patches:
        p.start()

    resp = client.post(
        "/api/v1/libraries/collections",
        json={
            "name": ctx.get("collection_name") or "My Collection",
            "slug": ctx.get("collection_slug") or "my-collection",
            "manifest_pins": [_parse_pin_spec(spec)],
        },
    )
    _store_response(request, ctx, resp)


@when(parsers.parse('the operator updates the collection to pin "{spec}"'))
def _operator_updates_collection(
    ctx: dict[str, Any], request: pytest.FixtureRequest, patches: list[Any], spec: str
) -> None:
    client = _active_client(request)
    prim = ctx.get("collection") or _mk_collection_primitive(ctx, status="draft")
    prim.manifest_pins = [_parse_pin_spec(spec)]
    patches.append(patch(f"{_ROUTE}._require_library_collection_flag", new=AsyncMock()))
    patches.append(patch(f"{_ROUTE}._set_rls_context", new=AsyncMock()))
    patches.append(patch(f"{_ROUTE}.get_primitive", new=AsyncMock(return_value=prim)))
    for p in patches:
        p.start()

    resp = client.patch(
        f"/api/v1/libraries/collections/{ctx['collection_id']}",
        json={"manifest_pins": [_parse_pin_spec(spec)]},
    )
    _store_response(request, ctx, resp)


@when("the operator publishes the collection")
def _operator_publishes_collection(
    ctx: dict[str, Any], request: pytest.FixtureRequest, patches: list[Any]
) -> None:
    client = _active_client(request)
    prim = ctx.get("collection") or _mk_collection_primitive(ctx, status="draft")
    lookup = ctx.get("pin_lookup")
    if lookup == "known":
        pin_prim = MagicMock()
        pin_prim.slug = "input-schema"
        pin_prim.version = "1.0"
        pin_prim.primitive_type = "schema"
        lookup_effect: Any = AsyncMock(return_value=pin_prim)
    else:
        lookup_effect = AsyncMock(return_value=None)

    patches.append(patch(f"{_ROUTE}._require_library_collection_flag", new=AsyncMock()))
    patches.append(patch(f"{_ROUTE}._set_rls_context", new=AsyncMock()))
    patches.append(patch(f"{_ROUTE}.get_primitive", new=AsyncMock(return_value=prim)))
    patches.append(patch(f"{_ROUTE}._lookup_pin_primitive", new=lookup_effect))
    for p in patches:
        p.start()

    resp = client.post(f"/api/v1/libraries/collections/{ctx['collection_id']}/publish")
    _store_response(request, ctx, resp)


# --- Authoring Then steps ---------------------------------------------------


@then(parsers.parse('the collection response has status "{expected}"'))
def _collection_response_status(ctx: dict[str, Any], expected: str) -> None:
    data = ctx["response"].json()
    assert data.get("status") == expected, f"Expected collection status '{expected}', got {data.get('status')!r}"


@then(parsers.parse('the collection response echoes manifest pins for "{spec}"'))
def _collection_response_pins(ctx: dict[str, Any], spec: str) -> None:
    data = ctx["response"].json()
    pins = data.get("manifest_pins") or []
    expected = _parse_pin_spec(spec)
    assert expected in pins, f"Expected pin {expected} in response manifest_pins, got {pins!r}"


def _parse_pin_spec(spec: str) -> dict[str, str]:
    """Parse a ``slug@version`` pin spec (e.g. ``input-schema@1.0``) into a dict."""
    slug, version = spec.split("@", 1)
    return {"slug": slug, "version": version}
