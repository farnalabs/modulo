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
from typing import Any
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


@given("a draft library collection exists")
def _draft_collection_exists(ctx: dict[str, Any]) -> None:
    ctx["collection_id"] = uuid.uuid4()
    ctx["install"] = _mk_install()
    ctx["install_id"] = ctx["install"].install_id
    ctx["install_error"] = CollectionNotPublishedError(
        "Collection 'Test Collection' must be published before installing (current status: draft)"
    )


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
