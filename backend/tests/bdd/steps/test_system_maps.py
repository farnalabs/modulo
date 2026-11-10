"""Step definitions for the System Maps BDD features (FAR-2288).

These scenarios drive the real REST router through ``TestClient``. The
organisation inventory and the repository are the only seams replaced: the
inventory reads are served by the shared mock session, and the repository is a
spec'd double for the revision endpoints (discovery constructs its own
``SystemMapDiscoveryRepository``, so its persistence is patched at the class).
The router's own gate (internal access + ``system_maps`` feature flag) and the
handlers' validation/branches run for real.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import get_plan_context, get_settings
from modulo.api.main import app
from modulo.api.routes.system_maps import get_repo
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.system_map_persistence import SystemMapDiscoveryRepository, SystemMapNameConflictError
from modulo.db.repositories.system_map import SystemMapRepository
from tests.bdd.conftest import ORG_ID, USER_ID, make_settings

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/system_maps/access_and_discovery.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/system_maps/revisions.feature")

_MAP_ID = uuid.UUID("11111111-2222-3333-4444-555555555551")
_REV_ID = uuid.UUID("11111111-2222-3333-4444-555555555552")
_NEW_REV_ID = uuid.UUID("11111111-2222-3333-4444-555555555553")
_PIPELINE_ID = uuid.UUID("11111111-2222-3333-4444-555555555561")
_TRIGGER_ID = uuid.UUID("11111111-2222-3333-4444-555555555562")
_CONNECTOR_ID = uuid.UUID("11111111-2222-3333-4444-555555555563")
_FOLDER_ID = uuid.UUID("11111111-2222-3333-4444-555555555564")

_BASE = "/api/v1/system-maps"


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# Backend doubles
# ---------------------------------------------------------------------------


@pytest.fixture
def system_map_repo() -> MagicMock:
    """Spec'd repository double; each scenario stubs the methods it drives."""
    repo = MagicMock(spec=SystemMapRepository)
    repo.get_map_by_id = AsyncMock(return_value=_map_row())
    repo.get_revision_by_id = AsyncMock(return_value=_revision_row())
    repo.validate_publish = AsyncMock(return_value=(True, []))
    repo.publish_revision = AsyncMock(return_value=_revision_row(status="published"))
    repo.list_revisions = AsyncMock(return_value=[])
    repo.diff_revisions = AsyncMock(return_value={})
    repo.restore_revision_as_new_draft = AsyncMock(return_value=_revision_row(rev_id=_NEW_REV_ID))
    repo.get_revision_by_time = AsyncMock(return_value=None)
    repo.get_current_revision = AsyncMock(return_value=None)
    repo.list_revisions_history = AsyncMock(return_value=[])
    return repo


@pytest.fixture(autouse=True)
def _install_repo(system_map_repo: MagicMock, client: Any) -> Any:
    """Point the router's repository dependency at the spec'd double."""
    app.dependency_overrides[get_repo] = lambda: system_map_repo
    yield
    app.dependency_overrides.pop(get_repo, None)


def _map_row(*, published_revision_id: uuid.UUID | None = _REV_ID) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=_MAP_ID,
        organisation_id=ORG_ID,
        name="Dogfood Map",
        description=None,
        owner_team_id=None,
        visibility="org",
        default_map=False,
        published_revision_id=published_revision_id,
        account_id=USER_ID,
        updated_by=USER_ID,
        deleted_at=None,
        deleted_by=None,
        created_at=now,
        updated_at=now,
    )


def _revision_row(
    *,
    rev_id: uuid.UUID = _REV_ID,
    revision_number: int = 1,
    status: str = "draft",
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=rev_id,
        system_map_id=_MAP_ID,
        organisation_id=ORG_ID,
        revision_number=revision_number,
        status=status,
        content_json={},
        published_at=now if status == "published" else None,
        effective_from=effective_from,
        effective_to=effective_to,
        deleted_at=None,
        deleted_by=None,
        created_at=now,
        updated_at=now,
    )


def _install_principal(role: str, grants: frozenset[str] | None) -> None:
    """Replace the authenticated principal for the running scenario."""
    principal = TenantPrincipal(
        username=role,
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role=role,
        key_grants=grants,
    )
    auth_user = AuthenticatedPrincipal(
        username=role,
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role=role,
        key_grants=grants,
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: principal
    app.dependency_overrides[get_current_user] = lambda: auth_user


class _NoSystemMapsPlan:
    """Plan context where the ``system_maps`` flag is off."""

    def feature_enabled(self, name: str) -> bool:
        return name != "system_maps"

    def list_enabled_features(self) -> list[str]:
        return []

    def tier(self) -> str:
        return "community"

    def has_license_key(self) -> bool:
        return False


def _inventory_rows() -> dict[str, list[SimpleNamespace]]:
    pipeline = SimpleNamespace(
        id=_PIPELINE_ID,
        name="build",
        folder_id=_FOLDER_ID,
        run_context_defaults={},
        organisation_id=ORG_ID,
        deleted_at=None,
        archived_at=None,
    )
    trigger = SimpleNamespace(
        id=_TRIGGER_ID,
        name="webhook",
        pipeline_id=_PIPELINE_ID,
        trigger_type="webhook",
        organisation_id=ORG_ID,
        deleted_at=None,
    )
    connector = SimpleNamespace(
        id=_CONNECTOR_ID,
        name="github",
        connector_type_id="github",
        organisation_id=ORG_ID,
        deleted_at=None,
    )
    folder = SimpleNamespace(id=_FOLDER_ID, name="Build Folder", organisation_id=ORG_ID)
    return {
        "FROM pipelines": [pipeline],
        "FROM triggers": [trigger],
        "FROM connector_instances": [connector],
        "FROM pipeline_folders": [folder],
    }


def _serve_inventory(mock_session: Any) -> None:
    """Answer the four inventory SELECTs (plus the authz kill-switch read)."""
    inventory = _inventory_rows()

    def _dispatch(statement: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(statement)
        if "organisations.authz_enforce" in text:
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=True)
            return result
        for snippet, rows in inventory.items():
            if snippet in text:
                result = MagicMock()
                result.scalars = MagicMock(return_value=list(rows))
                return result
        raise AssertionError(f"unexpected System Maps query: {text}")

    mock_session.execute = AsyncMock(side_effect=_dispatch)


# ---------------------------------------------------------------------------
# Access-gate Given steps
# ---------------------------------------------------------------------------


@given("the caller's credential carries no system maps grant")
def caller_without_grant(client: Any) -> None:
    _install_principal("operator", frozenset({"pipeline.list"}))


@given("developer mode is disabled for the instance")
def developer_mode_disabled(client: Any) -> None:
    app.dependency_overrides[get_settings] = lambda: make_settings().model_copy(update={"modulo_dev_mode": False})


@given("the system maps feature is disabled on the plan")
def system_maps_feature_disabled(client: Any) -> None:
    app.dependency_overrides[get_plan_context] = lambda: _NoSystemMapsPlan()


# ---------------------------------------------------------------------------
# Discovery Given/When/Then
# ---------------------------------------------------------------------------


@given("the organisation inventory has a pipeline, a trigger, a connector and a folder")
def organisation_inventory(mock_session: Any) -> None:
    _serve_inventory(mock_session)


@given("an existing draft map with the same name already exists")
def existing_draft_map_already_exists(ctx: dict[str, Any]) -> None:
    ctx["conflict_name"] = "Existing Map"


@when(parsers.parse('I request System Maps discovery for a map named "{name}"'))
def request_discovery(
    name: str,
    request: Any,
    ctx: dict[str, Any],
) -> None:
    """POST discovery through the router with persistence patched at the class."""
    draft_map = SimpleNamespace(id=_MAP_ID)
    persisted: list[dict[str, Any]] = []

    async def _save(*_args: object, **kwargs: Any) -> tuple[SimpleNamespace, uuid.UUID, int]:
        persisted.append(kwargs["content"])
        return draft_map, _REV_ID, 1

    async def _get_or_create(*_args: object, **kwargs: Any) -> SimpleNamespace:
        if ctx.get("conflict_name") == kwargs.get("name"):
            raise SystemMapNameConflictError(f"A system map named '{kwargs.get('name')}' already exists")
        return draft_map

    ctx["persisted_discovery"] = persisted
    client = request.node._client
    with (
        patch.object(SystemMapDiscoveryRepository, "get_or_create_draft", new=AsyncMock(side_effect=_get_or_create)),
        patch.object(SystemMapDiscoveryRepository, "save_discovery_revision", new=AsyncMock(side_effect=_save)),
    ):
        resp = client.post(f"{_BASE}/discover", json={"name": name})
    _store_response(request, ctx, resp)


@then("the discovery report is unpublished and names the draft")
def discovery_report_unpublished(request: Any) -> None:
    body = request.node._resp.json()
    assert body["published"] is False
    assert body["draft_map_id"] == str(_MAP_ID)
    assert body["draft_revision_id"] == str(_REV_ID)
    assert body["node_count"] == 4


@then("the discovery report infers suggested edges")
def discovery_report_suggested_edges(request: Any) -> None:
    edges = request.node._resp.json()["inferred_edges"]
    assert len(edges) == 2
    assert {edge["relationship_type"] for edge in edges} == {"trigger", "flow"}
    assert all(edge["config_json"]["suggested"] is True for edge in edges)


@then("the persisted discovery draft records honest stage types")
def discovery_persisted_stage_types(ctx: dict[str, Any]) -> None:
    persisted = ctx["persisted_discovery"]
    assert len(persisted) == 1
    content = persisted[0]
    stage_types = {stage["name"]: stage["type"] for stage in content["stages"]}
    assert stage_types == {
        "build": "modulo",
        "webhook": "manual",
        "github": "placeholder",
        "Build Folder": "placeholder",
    }
    assert content["edges"][0]["config_json"]["suggested"] is True
    assert content["discovery_fingerprint"]


@then("the discovery reads are tenant scoped")
def discovery_reads_tenant_scoped(mock_session: Any) -> None:
    assert mock_session.info["org_id"] == ORG_ID
    assert mock_session.info["user_id"] == USER_ID
    statements = [call.args[0] for call in mock_session.execute.await_args_list]
    inventory_tables = ("FROM pipelines", "FROM triggers", "FROM connector_instances", "FROM pipeline_folders")
    inventory_selects = [statement for statement in statements if any(t in str(statement) for t in inventory_tables)]
    assert len(inventory_selects) == 4
    for statement in inventory_selects:
        assert "organisation_id" in str(statement)


@then("the discovery denial names the required role")
def discovery_denial_role(request: Any) -> None:
    assert "requires 'operator' role" in request.node._resp.json()["detail"]


@then("the discovery denial reports the key is not granted")
def discovery_denial_grant(request: Any) -> None:
    assert "is not granted to this API key" in request.node._resp.json()["detail"]


@then("the discovery denial requires developer mode")
def discovery_denial_dev_mode(request: Any) -> None:
    assert "requires developer mode" in request.node._resp.json()["detail"]


@then("the discovery denial names the system_maps feature")
def discovery_denial_feature(request: Any) -> None:
    body = request.node._resp.json()
    assert body["type"] == "urn:problem:modulo:feature_required"
    assert "system_maps" in body["detail"]


@then("the discovery conflict reports the name collision")
def discovery_conflict_reports_name(request: Any) -> None:
    detail = request.node._resp.json()["detail"]
    assert "already exists" in detail


@given("a published system map exists in the organisation")
def published_system_map_exists(system_map_repo: MagicMock, mock_session: Any) -> None:
    pub_map = _map_row(published_revision_id=_REV_ID)
    system_map_repo.list_maps_by_org = AsyncMock(return_value=[pub_map])
    system_map_repo.get_map_by_id = AsyncMock(return_value=pub_map)
    mock_session.execute.return_value.scalar = MagicMock(return_value=1)


@when("I request the published system maps list")
def request_published_system_maps_list(request: Any, ctx: dict[str, Any]) -> None:
    client = request.node._client
    resp = client.get(_BASE)
    _store_response(request, ctx, resp)


@then("the response includes the published system map")
def response_includes_published_map(request: Any) -> None:
    body = request.node._resp.json()
    assert body["items"]
    assert body["items"][0]["id"] == str(_MAP_ID)
    assert body["items"][0]["published_revision_id"] == str(_REV_ID)


@given("an unpublished draft system map exists in the organisation")
def unpublished_draft_map_exists(system_map_repo: MagicMock) -> None:
    draft_map = _map_row(published_revision_id=None)
    system_map_repo.get_map_by_id = AsyncMock(return_value=draft_map)


@when("I request the unpublished system map")
def request_unpublished_system_map(request: Any, ctx: dict[str, Any]) -> None:
    client = request.node._client
    resp = client.get(f"{_BASE}/{_MAP_ID}")
    _store_response(request, ctx, resp)


# ---------------------------------------------------------------------------
# Revision-lifecycle Given/When/Then
# ---------------------------------------------------------------------------


@given("a draft revision whose graph fails publish validation")
def draft_revision_invalid(system_map_repo: MagicMock) -> None:
    system_map_repo.get_revision_by_id = AsyncMock(return_value=_revision_row(status="draft"))
    system_map_repo.validate_publish = AsyncMock(return_value=(False, ["Cycle detected: node-a -> node-a"]))
    system_map_repo.publish_revision = AsyncMock(return_value=_revision_row(status="published"))


@given("a published revision and a newer draft with one added node")
def published_and_draft_revisions(system_map_repo: MagicMock) -> None:
    published = _revision_row(status="published", revision_number=1)
    draft = _revision_row(rev_id=_NEW_REV_ID, status="draft", revision_number=2)
    system_map_repo.get_revision_by_id = AsyncMock(side_effect=[published, draft])
    system_map_repo.list_revisions = AsyncMock(return_value=[draft])
    system_map_repo.diff_revisions = AsyncMock(
        return_value={
            "base_revision_id": _REV_ID,
            "base_revision_number": 1,
            "target_revision_id": _NEW_REV_ID,
            "target_revision_number": 2,
            "nodes_added": ["pipeline:new-node"],
            "content_json_changed": True,
        }
    )


@given(parsers.parse("an archived revision with revision number {number:d}"))
def archived_revision(system_map_repo: MagicMock, number: int) -> None:
    system_map_repo.get_revision_by_id = AsyncMock(
        return_value=_revision_row(status="archived", revision_number=number)
    )
    system_map_repo.restore_revision_as_new_draft = AsyncMock(
        return_value=_revision_row(rev_id=_NEW_REV_ID, status="draft", revision_number=number + 1)
    )


@given(parsers.parse("a draft revision with revision number {number:d}"))
def draft_revision(system_map_repo: MagicMock, number: int) -> None:
    system_map_repo.get_revision_by_id = AsyncMock(return_value=_revision_row(status="draft", revision_number=number))


@given(parsers.parse('a published revision effective from "{effective_from}"'))
def published_revision_effective_from(system_map_repo: MagicMock, effective_from: str) -> None:
    revision = _revision_row(
        status="published",
        effective_from=datetime.fromisoformat(effective_from),
        effective_to=None,
    )
    system_map_repo.get_revision_by_time = AsyncMock(return_value=revision)


@given(parsers.parse('no revision is effective at "{at}"'))
def no_revision_effective_at(system_map_repo: MagicMock, at: str) -> None:
    system_map_repo.get_revision_by_time = AsyncMock(return_value=None)


@when("I publish the revision")
def publish_revision(request: Any, ctx: dict[str, Any], system_map_repo: MagicMock, client: Any) -> None:
    resp = client.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish")
    ctx["publish_mock"] = system_map_repo.publish_revision
    _store_response(request, ctx, resp)


@then("the publish response carries validation errors")
def publish_response_validation_errors(request: Any) -> None:
    errors = request.node._resp.json()["validation_errors"]
    assert len(errors) == 1
    assert "Cycle detected" in errors[0]["message"]


@then("the invalid revision was not published")
def invalid_revision_not_published(ctx: dict[str, Any]) -> None:
    ctx["publish_mock"].assert_not_awaited()


@when("I diff the published revision against the draft")
def diff_against_draft(request: Any, ctx: dict[str, Any], client: Any) -> None:
    resp = client.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/diff", json={})
    _store_response(request, ctx, resp)


@then("the diff report names the added node")
def diff_report_added_node(request: Any) -> None:
    body = request.node._resp.json()
    assert body["nodes_added"] == ["pipeline:new-node"]
    assert body["target_revision_id"] == str(_NEW_REV_ID)
    assert body["content_json_changed"] is True


@when("I restore the revision as new")
def restore_revision(request: Any, ctx: dict[str, Any], client: Any) -> None:
    resp = client.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/restore-as-new", json={})
    _store_response(request, ctx, resp)


@then("the restore response names a new draft revision")
def restore_response_new_revision(request: Any) -> None:
    body = request.node._resp.json()
    assert body["new_revision"]["id"] == str(_NEW_REV_ID)
    assert body["new_revision"]["status"] == "draft"
    assert body["new_revision"]["revision_number"] == 3


@then("the restore response cites the source revision")
def restore_response_source_revision(request: Any) -> None:
    body = request.node._resp.json()
    assert body["source_revision_id"] == str(_REV_ID)
    assert body["source_revision_number"] == 2


@when(parsers.parse('I read the revision active at "{at}"'))
def read_revision_by_time(request: Any, ctx: dict[str, Any], client: Any, at: str) -> None:
    resp = client.get(f"{_BASE}/{_MAP_ID}/revisions/by-time", params={"at": at})
    _store_response(request, ctx, resp)


@then("the by-time read reports the published revision")
def by_time_reports_revision(request: Any) -> None:
    body = request.node._resp.json()
    assert body["matched"] is True
    assert body["revision"]["id"] == str(_REV_ID)
    assert body["revision"]["effective_from"] == "2026-10-01T00:00:00Z"


@then("the by-time read reports no active revision")
def by_time_reports_none(request: Any) -> None:
    body = request.node._resp.json()
    assert "No revision active at" in body["detail"]
