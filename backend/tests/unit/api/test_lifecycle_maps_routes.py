"""Route-level tests for the lifecycle-map CRUD/version/journey endpoints (FAR-574).

The service layer is mocked at the route boundary (unit tier: no DB) — these
tests pin the wire contract (status codes, payload shapes, the route error
convention ProgrammingError→501 / IntegrityError→409 / SQLAlchemyError→503 /
Exception→500) for every endpoint in ``api/routes/lifecycle_maps.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_MAP_ID = uuid.uuid4()
_BASE = "/api/v1/lifecycle-maps"
_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

_CONTENT = {
    "stages": [
        {"id": "stage-1", "name": "Build", "type": "modulo", "x": 1.0, "y": 2.0},
        {"id": "stage-2", "name": "Merge", "type": "external", "external_url": "https://ci.example.com"},
    ],
    "edges": [{"id": "e1", "source": "stage-1", "target": "stage-2", "trigger_type": "auto"}],
    "notes": "hello",
}

_SERVICE_FUNCS = [
    "create_lifecycle_map",
    "delete_lifecycle_map",
    "get_lifecycle_map",
    "graduate_stage",
    "list_lifecycle_maps",
    "restore_lifecycle_map",
    "save_map_version",
    "update_lifecycle_map",
    "import_lifecycle_map_envelope",
    "list_map_journeys",
    "get_map_journey",
    "list_journey_runs",
    "advance_journeys",
    "confirm_reported_refs",
]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    stage_result = MagicMock()
    stage_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=stage_result)
    session.refresh = AsyncMock(return_value=None)
    return session


def _map_row() -> MagicMock:
    lm = MagicMock()
    lm.id = _MAP_ID
    lm.organisation_id = _ORG_ID
    lm.name = "Delivery Map"
    lm.description = "desc"
    lm.owner_team_id = None
    lm.visibility = "org"
    lm.version = 3
    lm.content_json = _CONTENT
    lm.archived_at = None
    lm.created_at = _NOW
    lm.updated_at = _NOW
    lm.updated_by = _USER_ID
    lm.account_id = _USER_ID
    return lm


def _journey_row() -> MagicMock:
    j = MagicMock()
    j.map_id = _MAP_ID
    j.map_version = 3
    j.stage_id = "stage-1"
    j.stage_name = "Build"
    j.position = 0
    j.kind = "issue"
    j.ref = "FAR-100"
    j.canonical_work_item_id = uuid.uuid4()
    j.latest_status = "complete"
    j.latest_provenance = "reported"
    j.run_count = 2
    j.latest_terminal_run_id = uuid.uuid4()
    j.updated_at = _NOW
    return j


def _run_row() -> MagicMock:
    r = MagicMock()
    r.id = uuid.uuid4()
    r.status = "complete"
    r.completed_at = _NOW
    r.trigger_type = "cron"
    return r


class _Harness:
    def __init__(self) -> None:
        self.session = _make_session()
        self.patches = [
            patch("modulo.api.routes.lifecycle_maps.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.lifecycle_maps.set_rls_user_context", new_callable=AsyncMock),
            patch("modulo.api.routes.lifecycle_maps.append_audit_event", new_callable=AsyncMock),
        ]
        for name in _SERVICE_FUNCS:
            self.patches.append(patch(f"modulo.api.routes.lifecycle_maps.{name}", new_callable=AsyncMock))

    def __enter__(self) -> Self:
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *args: object) -> None:
        for p in self.patches:
            p.stop()

    def stub(self, name: str, value: object) -> None:
        import modulo.api.routes.lifecycle_maps as route

        setattr(route, name, value)


def _install_overrides(harness: _Harness, *, org_role: str = "admin") -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield harness.session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )


@pytest.fixture
def client() -> Generator[tuple[TestClient, _Harness], None, None]:
    harness = _Harness()
    _install_overrides(harness)
    with harness:
        yield TestClient(app), harness
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET "" (list) + POST "" (create)
# ---------------------------------------------------------------------------


def test_list_maps_returns_page(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    result = MagicMock(items=[_map_row()], total=1, page=1, page_size=20)
    harness.stub("list_lifecycle_maps", AsyncMock(return_value=result))

    resp = http.get(_BASE, params={"page": 1, "page_size": 20, "include_archived": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Delivery Map"
    assert body["items"][0]["version"] == 3


def test_list_maps_sqlalchemy_error_maps_to_503(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("list_lifecycle_maps", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.get(_BASE)

    assert resp.status_code == 503


def test_list_maps_programming_error_maps_to_501(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("list_lifecycle_maps", AsyncMock(side_effect=ProgrammingError("s", {}, Exception())))

    resp = http.get(_BASE)

    assert resp.status_code == 501


def test_create_map_returns_201(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("create_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.post(
        _BASE,
        json={"name": "Delivery Map", "visibility": "org", "version": 1, "content_json": _CONTENT},
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "Delivery Map"


def test_create_map_content_error_maps_to_422(client: tuple[TestClient, _Harness]) -> None:
    from modulo.core.lifecycle_map.validation import LifecycleMapContentError

    http, harness = client
    harness.stub("create_lifecycle_map", AsyncMock(side_effect=LifecycleMapContentError("bad graph")))

    resp = http.post(_BASE, json={"name": "Delivery Map"})

    assert resp.status_code == 422
    assert "bad graph" in resp.json()["detail"]


def test_create_map_pipeline_conflict_maps_to_409(client: tuple[TestClient, _Harness]) -> None:
    from modulo.core.lifecycle_map.validation import LifecycleMapPipelineConflictError

    http, harness = client
    harness.stub(
        "create_lifecycle_map",
        AsyncMock(side_effect=LifecycleMapPipelineConflictError("pipeline already registered")),
    )

    resp = http.post(_BASE, json={"name": "Delivery Map"})

    assert resp.status_code == 409


def test_create_map_integrity_error_maps_to_409(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("create_lifecycle_map", AsyncMock(side_effect=IntegrityError("s", {}, Exception())))

    resp = http.post(_BASE, json={"name": "Delivery Map"})

    assert resp.status_code == 409


def test_create_map_unexpected_error_maps_to_500(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("create_lifecycle_map", AsyncMock(side_effect=RuntimeError("kaboom")))

    resp = http.post(_BASE, json={"name": "Delivery Map"})

    assert resp.status_code == 500


def test_create_map_rejects_empty_name(client: tuple[TestClient, _Harness]) -> None:
    http, _harness = client

    resp = http.post(_BASE, json={"name": ""})

    assert resp.status_code == 422


def test_create_map_rejects_bad_visibility(client: tuple[TestClient, _Harness]) -> None:
    http, _harness = client

    resp = http.post(_BASE, json={"name": "Map", "visibility": "public"})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /import + GET /{id}/export
# ---------------------------------------------------------------------------


def test_import_map_returns_201(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("import_lifecycle_map_envelope", AsyncMock(return_value=_map_row()))

    envelope = {
        "primitive_type": "lifecycle_map",
        "format_version": "2",
        "name": "Imported Map",
        "content_json": _CONTENT,
        "versions": None,
    }
    resp = http.post(f"{_BASE}/import", json=envelope)

    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "Delivery Map"  # from the mocked row


def test_import_bundle_error_maps_to_422(client: tuple[TestClient, _Harness]) -> None:
    from modulo.core.lifecycle_map.import_export import LifecycleMapBundleError

    http, harness = client
    harness.stub("import_lifecycle_map_envelope", AsyncMock(side_effect=LifecycleMapBundleError("bad bundle")))

    envelope = {
        "primitive_type": "lifecycle_map",
        "format_version": "2",
        "name": "Imported Map",
        "content_json": _CONTENT,
    }
    resp = http.post(f"{_BASE}/import", json=envelope)

    assert resp.status_code == 422


def test_export_map_returns_envelope(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.get(f"{_BASE}/{_MAP_ID}/export")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["primitive_type"] == "lifecycle_map"
    assert body["format_version"] == "2"
    assert body["name"] == "Delivery Map"
    assert len(body["versions"]) == 1


def test_export_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/export")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Lifecycle map not found"


def test_export_sqlalchemy_error_maps_to_503(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.get(f"{_BASE}/{_MAP_ID}/export")

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /{id} (detail) + PUT /{id} + DELETE /{id} + POST /{id}/restore
# ---------------------------------------------------------------------------


def test_get_map_detail_decodes_stages_and_edges(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.get(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current_version"] == 3
    assert [s["id"] for s in body["stages"]] == ["stage-1", "stage-2"]
    assert body["stages"][0]["type"] == "modulo"
    assert body["stages"][1]["external_url"] == "https://ci.example.com"
    assert [t["id"] for t in body["transitions"]] == ["e1"]
    assert body["versions"][0]["version"] == 3


def test_get_map_missing_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 404


def test_update_map_returns_row(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("update_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"name": "Renamed"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Delivery Map"  # serialized from the mocked row


def test_update_map_missing_returns_404(client: tuple[TestClient, _Harness]) -> None:
    from fastapi import HTTPException

    http, harness = client
    harness.stub(
        "update_lifecycle_map",
        AsyncMock(side_effect=HTTPException(status_code=404, detail="Lifecycle map not found")),
    )

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"name": "Renamed"})

    assert resp.status_code == 404


def test_update_map_sqlalchemy_error_maps_to_503(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("update_lifecycle_map", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"name": "Renamed"})

    assert resp.status_code == 503


def test_delete_map_returns_204_and_audits(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("delete_lifecycle_map", AsyncMock(return_value=True))

    resp = http.delete(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 204, resp.text


def test_delete_map_missing_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("delete_lifecycle_map", AsyncMock(return_value=False))

    resp = http.delete(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 404


def test_delete_map_sqlalchemy_error_maps_to_503(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("delete_lifecycle_map", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.delete(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 503


def test_restore_map_returns_row(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("restore_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.post(f"{_BASE}/{_MAP_ID}/restore")

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(_MAP_ID)


def test_restore_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("restore_lifecycle_map", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_MAP_ID}/restore")

    assert resp.status_code == 404
    assert "not deleted" in resp.json()["detail"]


def test_restore_integrity_error_maps_to_409(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("restore_lifecycle_map", AsyncMock(side_effect=IntegrityError("s", {}, Exception())))

    resp = http.post(f"{_BASE}/{_MAP_ID}/restore")

    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Versions: GET list, POST save, PUT update, GET one, PATCH graduate
# ---------------------------------------------------------------------------


def test_list_versions_returns_active_version_entry(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.get(f"{_BASE}/{_MAP_ID}/versions")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["version"] == 3
    assert entry["version_number"] == 3
    assert [s["id"] for s in entry["stages"]] == ["stage-1", "stage-2"]
    assert [e["source_stage_id"] for e in entry["edges"]] == ["stage-1"]
    assert entry["notes"] == "hello"


def test_list_versions_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/versions")

    assert resp.status_code == 404


def test_save_version_returns_201(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("save_map_version", AsyncMock(return_value=_map_row()))

    payload = {"stages": [{"id": "s1", "name": "S", "stage_type": "work"}], "edges": [], "notes": "n"}
    resp = http.post(f"{_BASE}/{_MAP_ID}/versions", json=payload)

    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 3


def test_save_version_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("save_map_version", AsyncMock(return_value=None))

    payload = {"stages": [], "edges": [], "notes": ""}
    resp = http.post(f"{_BASE}/{_MAP_ID}/versions", json=payload)

    assert resp.status_code == 404


def test_save_version_content_error_maps_to_422(client: tuple[TestClient, _Harness]) -> None:
    from modulo.core.lifecycle_map.validation import LifecycleMapContentError

    http, harness = client
    harness.stub("save_map_version", AsyncMock(side_effect=LifecycleMapContentError("bad stages")))

    payload = {"stages": [], "edges": [], "notes": ""}
    resp = http.post(f"{_BASE}/{_MAP_ID}/versions", json=payload)

    assert resp.status_code == 422


def test_save_version_pipeline_conflict_maps_to_409(client: tuple[TestClient, _Harness]) -> None:
    from modulo.core.lifecycle_map.validation import LifecycleMapPipelineConflictError

    http, harness = client
    harness.stub(
        "save_map_version",
        AsyncMock(side_effect=LifecycleMapPipelineConflictError("pipeline conflict")),
    )

    payload = {"stages": [], "edges": [], "notes": ""}
    resp = http.post(f"{_BASE}/{_MAP_ID}/versions", json=payload)

    assert resp.status_code == 409


def test_update_version_behaves_like_save(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("save_map_version", AsyncMock(return_value=_map_row()))

    payload = {"stages": [], "edges": [], "notes": "updated"}
    resp = http.put(f"{_BASE}/{_MAP_ID}/versions/{uuid.uuid4()}", json=payload)

    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 3


def test_update_version_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("save_map_version", AsyncMock(return_value=None))

    payload = {"stages": [], "edges": [], "notes": ""}
    resp = http.put(f"{_BASE}/{_MAP_ID}/versions/{uuid.uuid4()}", json=payload)

    assert resp.status_code == 404


def test_get_version_mismatch_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.get(f"{_BASE}/{_MAP_ID}/versions/2")

    assert resp.status_code == 404
    assert "version not found" in resp.json()["detail"]


def test_get_version_match_returns_detail(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.get(f"{_BASE}/{_MAP_ID}/versions/3")

    assert resp.status_code == 200, resp.text
    assert resp.json()["current_version"] == 3


def test_graduate_stage_returns_version_entry(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("graduate_stage", AsyncMock(return_value=_map_row()))

    resp = http.patch(
        f"{_BASE}/{_MAP_ID}/versions/{uuid.uuid4()}/stages/stage-1/graduate",
        json={"pipeline_id": str(uuid.uuid4())},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 3


def test_graduate_stage_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("graduate_stage", AsyncMock(return_value=None))

    resp = http.patch(
        f"{_BASE}/{_MAP_ID}/versions/{uuid.uuid4()}/stages/stage-1/graduate",
        json={"pipeline_id": None},
    )

    assert resp.status_code == 404


def test_graduate_stage_sqlalchemy_error_maps_to_503(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("graduate_stage", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.patch(
        f"{_BASE}/{_MAP_ID}/versions/{uuid.uuid4()}/stages/stage-1/graduate",
        json={"pipeline_id": None},
    )

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Journeys: GET list, GET detail, POST self-report
# ---------------------------------------------------------------------------


def test_list_journeys_returns_summaries(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))
    harness.stub("list_map_journeys", AsyncMock(return_value=([(_journey_row(), False)], "cursor-token")))

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys", params={"kind": "issue", "ref": "FAR-100"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["next_cursor"] == "cursor-token"
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["kind"] == "issue"
    assert item["ref"] == "FAR-100"
    assert item["current_stage"]["stage_id"] == "stage-1"
    assert item["run_count"] == 2
    assert item["unattributed"] is False


def test_list_journeys_without_current_stage(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    j = _journey_row()
    j.map_id = None
    j.stage_id = None
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))
    harness.stub("list_map_journeys", AsyncMock(return_value=([(j, True)], None)))

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["next_cursor"] is None
    assert body["items"][0]["current_stage"] is None
    assert body["items"][0]["unattributed"] is True


def test_list_journeys_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys")

    assert resp.status_code == 404


def test_list_journeys_invalid_cursor_maps_to_422(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))
    harness.stub("list_map_journeys", AsyncMock(side_effect=ValueError("bad cursor")))

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys", params={"cursor": "garbage"})

    assert resp.status_code == 422


def test_get_journey_detail_includes_run_history(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))
    harness.stub("get_map_journey", AsyncMock(return_value=(_journey_row(), False)))
    harness.stub("list_journey_runs", AsyncMock(return_value=[_run_row()]))

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys/issue/FAR-100")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ref"] == "FAR-100"
    assert len(body["runs"]) == 1
    assert body["runs"][0]["provenance"] == "cron"


def test_get_journey_missing_journey_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))
    harness.stub("get_map_journey", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys/issue/FAR-100")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Journey not found"


def test_get_journey_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys/issue/FAR-100")

    assert resp.status_code == 404


def test_self_report_returns_counters(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))
    harness.stub("confirm_reported_refs", AsyncMock(return_value=([{"kind": "issue", "ref": "FAR-100"}], 1)))
    harness.stub("advance_journeys", AsyncMock(return_value=1))

    resp = http.post(
        f"{_BASE}/{_MAP_ID}/journeys/self-report",
        json={"work_item_refs": [{"kind": "issue", "ref": "FAR-100"}], "stage_id": "merge"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"accepted": 1, "rejected": 0, "unmatched": 1}


def test_self_report_counts_malformed_entries(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))
    harness.stub("confirm_reported_refs", AsyncMock(return_value=([], 0)))
    harness.stub("advance_journeys", AsyncMock(return_value=0))

    resp = http.post(
        f"{_BASE}/{_MAP_ID}/journeys/self-report",
        json={"work_item_refs": ["not-a-dict", {"kind": "issue", "ref": "FAR-100"}, 42]},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] == 0
    assert body["rejected"] == 2  # the two malformed entries
    assert body["unmatched"] == 0


def test_self_report_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_MAP_ID}/journeys/self-report", json={"work_item_refs": []})

    assert resp.status_code == 404


def test_self_report_sqlalchemy_error_maps_to_503(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.post(f"{_BASE}/{_MAP_ID}/journeys/self-report", json={"work_item_refs": []})

    assert resp.status_code == 503


def test_self_report_unexpected_error_maps_to_500(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))
    harness.stub("confirm_reported_refs", AsyncMock(side_effect=RuntimeError("kaboom")))

    resp = http.post(f"{_BASE}/{_MAP_ID}/journeys/self-report", json={"work_item_refs": []})

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Exception-mapping matrix — every endpoint honours the route error convention
# (ProgrammingError→501, SQLAlchemyError→503, Exception→500). Each case patches
# the FIRST service call the endpoint makes and asserts the mapped status.
# ---------------------------------------------------------------------------

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_RUNTIME = RuntimeError("kaboom")
_INTEGRITY = IntegrityError("s", {}, Exception())

_MAP_PAYLOADS = {
    "list": ("GET", _BASE, None, "list_lifecycle_maps"),
    "create": ("POST", _BASE, {"name": "Map"}, "create_lifecycle_map"),
    "import": (
        "POST",
        f"{_BASE}/import",
        {"primitive_type": "lifecycle_map", "format_version": "2", "name": "Map", "content_json": {}},
        "import_lifecycle_map_envelope",
    ),
    "export": ("GET", f"{_BASE}/{_MAP_ID}/export", None, "get_lifecycle_map"),
    "get": ("GET", f"{_BASE}/{_MAP_ID}", None, "get_lifecycle_map"),
    "update": ("PUT", f"{_BASE}/{_MAP_ID}", {"name": "Map"}, "update_lifecycle_map"),
    "delete": ("DELETE", f"{_BASE}/{_MAP_ID}", None, "delete_lifecycle_map"),
    "restore": ("POST", f"{_BASE}/{_MAP_ID}/restore", None, "restore_lifecycle_map"),
    "versions_list": ("GET", f"{_BASE}/{_MAP_ID}/versions", None, "get_lifecycle_map"),
    "version_save": ("POST", f"{_BASE}/{_MAP_ID}/versions", {"stages": [], "edges": []}, "save_map_version"),
    "version_update": (
        "PUT",
        f"{_BASE}/{_MAP_ID}/versions/{uuid.uuid4()}",
        {"stages": [], "edges": []},
        "save_map_version",
    ),
    "version_get": ("GET", f"{_BASE}/{_MAP_ID}/versions/3", None, "get_lifecycle_map"),
    "graduate": (
        "PATCH",
        f"{_BASE}/{_MAP_ID}/versions/{uuid.uuid4()}/stages/stage-1/graduate",
        {"pipeline_id": None},
        "graduate_stage",
    ),
    "journeys": ("GET", f"{_BASE}/{_MAP_ID}/journeys", None, "get_lifecycle_map"),
    "journey_detail": ("GET", f"{_BASE}/{_MAP_ID}/journeys/issue/FAR-100", None, "get_lifecycle_map"),
    "self_report": (
        "POST",
        f"{_BASE}/{_MAP_ID}/journeys/self-report",
        {"work_item_refs": []},
        "get_lifecycle_map",
    ),
}


@pytest.mark.parametrize("endpoint", sorted(_MAP_PAYLOADS))
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_PROG, 501),
        (_SQL, 503),
        (_RUNTIME, 500),
    ],
)
def test_service_error_mapping_matrix(
    client: tuple[TestClient, _Harness],
    endpoint: str,
    exc: Exception,
    expected: int,
) -> None:
    http, harness = client
    method, url, payload, service = _MAP_PAYLOADS[endpoint]
    harness.stub(service, AsyncMock(side_effect=exc))

    resp = http.request(method, url, json=payload)

    assert resp.status_code == expected, f"{endpoint} + {type(exc).__name__}: {resp.text}"


@pytest.mark.parametrize(
    "endpoint",
    ["create", "import", "update", "restore", "version_save", "version_update", "graduate"],
)
def test_integrity_error_maps_to_409(
    client: tuple[TestClient, _Harness],
    endpoint: str,
) -> None:
    http, harness = client
    method, url, payload, service = _MAP_PAYLOADS[endpoint]
    harness.stub(service, AsyncMock(side_effect=_INTEGRITY))

    resp = http.request(method, url, json=payload)

    assert resp.status_code == 409, f"{endpoint}: {resp.text}"
