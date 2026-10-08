"""Route-level tests for the lifecycle-map CRUD/version/journey endpoints (FAR-574).

The service layer is mocked at the route boundary (unit tier: no DB) — these
tests pin the wire contract (status codes, payload shapes, the route error
convention ProgrammingError→501 / IntegrityError→409 / SQLAlchemyError→503 /
Exception→500) for every endpoint in ``api/routes/lifecycle_maps.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError, SQLAlchemyError

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
    j.latest_provenance = "agent"
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
    # A real row always carries the provenance column (FAR-1566).
    r.execution_origin = None
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


def test_get_map_detail_versions_carry_uuid(client: tuple[TestClient, _Harness]) -> None:
    """FAR-833: the version meta in the detail response must include the version id."""
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.get(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["versions"]) == 1
    version_meta = body["versions"][0]
    assert "id" in version_meta
    assert version_meta["id"] == str(_MAP_ID)


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


def test_list_journeys_status_filter_passes_through(client: tuple[TestClient, _Harness]) -> None:
    stub = AsyncMock(return_value=([(_journey_row(), False)], None))
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))
    harness.stub("list_map_journeys", stub)

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys", params={"status": "failed"})

    assert resp.status_code == 200, resp.text
    assert stub.call_args.kwargs["status"] == "failed"
    assert stub.call_args.kwargs["updated_since"] is None


def test_list_journeys_invalid_status_returns_422(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys", params={"status": "running"})

    assert resp.status_code == 422


def test_list_journeys_updated_since_passes_through(client: tuple[TestClient, _Harness]) -> None:
    since = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
    stub = AsyncMock(return_value=([(_journey_row(), False)], None))
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))
    harness.stub("list_map_journeys", stub)

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys", params={"updated_since": "2026-08-25T00:00:00Z"})

    assert resp.status_code == 200, resp.text
    assert stub.call_args.kwargs["updated_since"] == since
    assert stub.call_args.kwargs["status"] is None


def test_list_journeys_malformed_updated_since_returns_422(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))

    resp = http.get(f"{_BASE}/{_MAP_ID}/journeys", params={"updated_since": "not-a-date"})

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


# ---------------------------------------------------------------------------
# FAR-1514: the request-time team gate — DENY paths, the member pass, the
# soft-deleted restore fix (CRITICAL 1) and the WITH CHECK transfer (MAJOR 2)
# ---------------------------------------------------------------------------
#
# Coverage before this section stopped at "the dependency is attached"
# (tests/architecture/test_team_scope_wiring.py) and "the DB policy denies"
# (tests/integration/test_rls_isolation.py): nothing drove the real route and
# observed the gate DENY a request. Every test below goes through TestClient
# with the real FastAPI dependency stack — only the session and the service
# layer are doubled.

_TEAM_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
#: The team a transfer/assignment targets in the MAJOR 2 test — deliberately
#: not ``_TEAM_ID`` so the transition validator has a real denial to raise.
_OTHER_TEAM_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
_NON_MEMBER_DETAIL = "Not a member of the team that owns this resource"
_REASSIGN_DETAIL = "Cannot reassign a resource to a team you are not a member of"


def _result(*, first: Any = None, scalar_one_or_none: Any = None) -> MagicMock:
    result = MagicMock()
    result.first.return_value = first
    result.scalar_one_or_none.return_value = scalar_one_or_none
    scalars = MagicMock()
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    return result


def _team_map_row(*, owner_team_id: uuid.UUID | None, visibility: str) -> MagicMock:
    row = _map_row()
    row.owner_team_id = owner_team_id
    row.visibility = visibility
    return row


def _team_gate_session(
    *,
    is_member_of: set[uuid.UUID],
    owner_team_id: uuid.UUID | None = _TEAM_ID,
    visibility: str = "team",
    row_present: bool = True,
    row_deleted: bool = False,
) -> AsyncMock:
    """Session double that answers the lifecycle-map team gate by SQL text.

    Dispatch is on the statement text (never call order), so a dependency
    reordering cannot silently swap answers:

    * ``authz_enforce`` — require_permission's kill-switch read: no override;
    * ``set_config`` — the RLS preamble (dialect reports ``sqlite``, so only
      ``session.info`` is written);
    * ``FROM lifecycle_maps`` — the team-scope resolver's projection. When the
      map is SOFT-DELETED (``row_deleted=True``, the restore case), a
      statement carrying a ``deleted_at`` predicate does NOT see it, exactly as
      Postgres would behave: that is what makes the restore test fail when the
      stock (deleted-filtering) resolver is wired;
    * ``team_memberships`` — membership for the QUERIED team only (the team id
      is read out of the compiled bind params), so a transfer to a foreign
      team is denied while the caller's own team still passes;
    * ``FROM teams`` — the transition validator's existence check: the target
      team exists in this org.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    session.info = {}
    session.refresh = AsyncMock(return_value=None)
    session.add = MagicMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)

    async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
        sql = str(stmt)
        if "set_config" in sql:
            return _result()
        if "authz_enforce" in sql:
            return _result(scalar_one_or_none=None)
        if "team_memberships" in sql:
            queried = _queried_team_id(stmt)
            return _result(first=queried if queried in is_member_of else None)
        if "FROM teams" in sql:
            return _result(first=_OTHER_TEAM_ID)
        if "FROM lifecycle_maps" in sql:
            if not row_present or (row_deleted and "deleted_at" in sql):
                return _result(first=None)  # absent row / filtered-out deleted row
            return _result(first=(owner_team_id, visibility))
        raise AssertionError(f"Unexpected session.execute(): {sql}")

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _queried_team_id(stmt: object) -> uuid.UUID | None:
    """The team id a ``team_membership_exists`` statement is filtering on."""
    params = getattr(stmt, "compile", None)
    if params is None:
        return None
    for value in stmt.compile().params.values():  # type: ignore[union-attr]
        if isinstance(value, uuid.UUID) and value != _USER_ID:
            return value
    return None


@contextmanager
def _gate_client(
    session: AsyncMock,
    *,
    org_role: str = "operator",
) -> Generator[tuple[TestClient, _Harness], None, None]:
    """TestClient with the real dependency stack and this team-gate session."""
    harness = _Harness()
    harness.session = session
    _install_overrides(harness, org_role=org_role)
    with harness:
        yield TestClient(app), harness
    app.dependency_overrides.clear()


# --- CRITICAL 1: restore must see the soft-deleted row ----------------------


def test_restore_non_admin_member_restores_a_soft_deleted_team_map() -> None:
    """The FAR-1514 CRITICAL regression: restore 404'd every non-admin.

    The gate resolves the target row BEFORE the handler, and restore's target
    row IS soft-deleted. With the stock ``resolve_lifecycle_map_team_scope``
    (which filters ``deleted_at IS NULL``) the resolver returns ``None`` and
    the dependency answers 404 before the handler runs — while admins, who
    bypass the gate, kept working. The deleted-inclusive resolver wired here
    must let a non-admin TEAM MEMBER through to a 200.
    """
    session = _team_gate_session(is_member_of={_TEAM_ID}, row_deleted=True)
    with _gate_client(session, org_role="operator") as (http, harness):
        harness.stub(
            "restore_lifecycle_map", AsyncMock(return_value=_team_map_row(owner_team_id=_TEAM_ID, visibility="team"))
        )
        resp = http.post(f"{_BASE}/{_MAP_ID}/restore")

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(_MAP_ID)


def test_restore_non_member_is_denied_403() -> None:
    """A non-member still cannot restore: the gate denies, the handler never runs."""
    session = _team_gate_session(is_member_of=set(), row_deleted=True)
    with _gate_client(session, org_role="operator") as (http, harness):
        restore = AsyncMock(return_value=_map_row())
        harness.stub("restore_lifecycle_map", restore)
        resp = http.post(f"{_BASE}/{_MAP_ID}/restore")

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL
    restore.assert_not_awaited()


# --- MAJOR 5: the gate DENIES at request time -------------------------------


def test_get_team_private_map_member_is_allowed() -> None:
    session = _team_gate_session(is_member_of={_TEAM_ID})
    with _gate_client(session, org_role="operator") as (http, harness):
        harness.stub(
            "get_lifecycle_map", AsyncMock(return_value=_team_map_row(owner_team_id=_TEAM_ID, visibility="team"))
        )
        resp = http.get(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Delivery Map"


def test_get_team_private_map_non_member_is_denied_403() -> None:
    """Non-member GET on a team-private map: 403 from the gate, not a 200."""
    session = _team_gate_session(is_member_of=set())
    with _gate_client(session, org_role="operator") as (http, harness):
        read = AsyncMock(return_value=_team_map_row(owner_team_id=_TEAM_ID, visibility="team"))
        harness.stub("get_lifecycle_map", read)
        resp = http.get(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL
    read.assert_not_awaited()


def test_get_unknown_map_is_404_from_the_gate() -> None:
    """Resolver returns no row -> 404, never "allowed with a missing row"."""
    session = _team_gate_session(is_member_of={_TEAM_ID}, row_present=False)
    with _gate_client(session, org_role="operator") as (http, harness):
        harness.stub("get_lifecycle_map", AsyncMock(return_value=None))
        resp = http.get(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 404, resp.text


def test_delete_non_member_is_denied_403() -> None:
    """The gate is wired on every {lifecycle_map_id} mutation, not just read."""
    session = _team_gate_session(is_member_of=set())
    with _gate_client(session, org_role="operator") as (http, harness):
        delete = AsyncMock(return_value=True)
        harness.stub("delete_lifecycle_map", delete)
        resp = http.delete(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL
    delete.assert_not_awaited()


# --- MAJOR 2: a team transfer is validated BEFORE the write -----------------


def test_update_transfer_to_a_foreign_team_is_403_before_the_write() -> None:
    """The RLS WITH CHECK denial must surface as 403, never 503.

    ``LifecycleMapUpdate`` carries ``owner_team_id``/``visibility``. Without
    the pre-write ``validate_team_transition_for_update`` a non-admin handing
    the map to a foreign team trips the policy's WITH CHECK (SQLSTATE 42501),
    which lands in the route's ``except SQLAlchemyError`` arm and answers
    ``503 "Database temporarily unavailable"`` — a permission denial reported
    as a DB outage. The transition is now checked first, and the write must
    not run.
    """
    session = _team_gate_session(is_member_of={_TEAM_ID})
    with _gate_client(session, org_role="operator") as (http, harness):
        harness.stub(
            "get_lifecycle_map", AsyncMock(return_value=_team_map_row(owner_team_id=_TEAM_ID, visibility="team"))
        )
        updater = AsyncMock(return_value=_map_row())
        harness.stub("update_lifecycle_map", updater)
        resp = http.put(f"{_BASE}/{_MAP_ID}", json={"owner_team_id": str(_OTHER_TEAM_ID)})

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _REASSIGN_DETAIL
    updater.assert_not_awaited()


def test_update_same_team_transfer_still_succeeds() -> None:
    """The validator must not blanket-block: a transfer to the caller's own team lands."""
    session = _team_gate_session(is_member_of={_TEAM_ID})
    with _gate_client(session, org_role="operator") as (http, harness):
        harness.stub(
            "get_lifecycle_map", AsyncMock(return_value=_team_map_row(owner_team_id=_TEAM_ID, visibility="team"))
        )
        harness.stub(
            "update_lifecycle_map", AsyncMock(return_value=_team_map_row(owner_team_id=_TEAM_ID, visibility="team"))
        )
        resp = http.put(f"{_BASE}/{_MAP_ID}", json={"owner_team_id": str(_TEAM_ID)})

    assert resp.status_code == 200, resp.text


def test_update_transfer_missing_map_returns_404_before_the_write() -> None:
    """A transfer for a map that vanished since the team gate answers 404 itself.

    The pre-write ``get_lifecycle_map`` re-read added with the FAR-1514
    transition check is the route's only chance to observe a row missing from
    the handler's own view (the gate resolver resolved it, but a concurrent
    delete can drop it before the write). It must answer 404 BEFORE calling
    ``update_lifecycle_map`` — not fall through to the write and discover the
    absence there.
    """
    session = _team_gate_session(is_member_of={_TEAM_ID})
    with _gate_client(session, org_role="operator") as (http, harness):
        harness.stub("get_lifecycle_map", AsyncMock(return_value=None))
        updater = AsyncMock(return_value=_map_row())
        harness.stub("update_lifecycle_map", updater)
        resp = http.put(f"{_BASE}/{_MAP_ID}", json={"owner_team_id": str(_TEAM_ID)})

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Lifecycle map not found"
    updater.assert_not_awaited()


def test_update_visibility_only_missing_map_returns_404_before_the_write() -> None:
    """The same pre-write read guards a ``visibility``-only update.

    ``visibility`` alone (no ``owner_team_id``) still triggers the transition
    check, so a missing row must 404 there too rather than reach the write.
    """
    session = _team_gate_session(is_member_of={_TEAM_ID})
    with _gate_client(session, org_role="operator") as (http, harness):
        harness.stub("get_lifecycle_map", AsyncMock(return_value=None))
        updater = AsyncMock(return_value=_map_row())
        harness.stub("update_lifecycle_map", updater)
        resp = http.put(f"{_BASE}/{_MAP_ID}", json={"visibility": "org"})

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Lifecycle map not found"
    updater.assert_not_awaited()


def _rls_denial(sqlstate: str = "42501") -> Exception:
    """A driver error carrying ``sqlstate``, as asyncpg's does."""

    class _DriverError(Exception):
        pass

    error = _DriverError("new row violates row-level security policy")
    error.sqlstate = sqlstate  # type: ignore[attr-defined]
    return error


def test_update_real_rls_denial_programming_error_maps_to_403(client: tuple[TestClient, _Harness]) -> None:
    """The REAL production path: asyncpg 42501 arrives as a ProgrammingError.

    asyncpg's ``InsufficientPrivilegeError`` subclasses ``SyntaxOrAccessError``,
    which SQLAlchemy's asyncpg dialect maps to ``ProgrammingError`` — and that
    arm PRECEDES the base ``SQLAlchemyError`` arm, so checking only the latter
    (the first shipped version of this test) reported 501 "migration required"
    for a permission denial. The test builds the exception type production
    actually raises, and must observe 403.
    """
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))

    denial = ProgrammingError("UPDATE lifecycle_maps", {}, _rls_denial("42501"))
    harness.stub("update_lifecycle_map", AsyncMock(side_effect=denial))

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"owner_team_id": str(_OTHER_TEAM_ID)})

    assert resp.status_code == 403, resp.text


def test_update_rls_denial_on_the_base_sqlalchemy_arm_maps_to_403(client: tuple[TestClient, _Harness]) -> None:
    """Dialect backstop: a 42501 surfaced as a plain DBAPI error is also 403.

    A backend whose driver does NOT fold 42501 into ProgrammingError reaches
    the ``except SQLAlchemyError`` arm; the same check runs there so the
    denial is never reported as a 503 outage either.
    """
    http, harness = client
    harness.stub("get_lifecycle_map", AsyncMock(return_value=_map_row()))

    denial = DBAPIError("UPDATE lifecycle_maps", {}, _rls_denial("42501"))
    harness.stub("update_lifecycle_map", AsyncMock(side_effect=denial))

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"owner_team_id": str(_OTHER_TEAM_ID)})

    assert resp.status_code == 403, resp.text


def test_update_non_42501_programming_error_still_maps_to_501(client: tuple[TestClient, _Harness]) -> None:
    """The 42501 check must discriminate on the SQLSTATE, not the exception type.

    A genuinely missing migration (``42P01`` undefined_table) still answers
    501 "migration required" — only ``insufficient_privilege`` becomes 403.
    """
    http, harness = client
    harness.stub(
        "update_lifecycle_map", AsyncMock(side_effect=ProgrammingError("SELECT nope", {}, _rls_denial("42P01")))
    )

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"name": "Renamed"})

    assert resp.status_code == 501, resp.text


def test_update_generic_sqlalchemy_error_still_maps_to_503(client: tuple[TestClient, _Harness]) -> None:
    """The 42501 arm must not swallow genuine DB outages."""
    http, harness = client
    harness.stub("update_lifecycle_map", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"name": "Renamed"})

    assert resp.status_code == 503


# --- the resolver itself: deleted-inclusive, fail-closed --------------------


async def test_restore_resolver_opts_out_of_the_global_soft_delete_filter() -> None:
    """The stock resolver cannot be used on restore — this one must opt out.

    A soft-deleted row is hidden from a plain ORM SELECT TWICE: the stock
    ``team_scope_resolver`` adds an explicit ``deleted_at IS NULL`` predicate,
    AND the global ``do_orm_execute`` listener (``db.soft_delete``) injects the
    same clause into every SELECT on a ``SoftDeleteMixin`` model. This
    statement must carry NEITHER — no explicit predicate, and the
    ``include_deleted`` execution option the listener reads to skip its
    injection.
    """
    from modulo.api.routes.lifecycle_maps import _resolve_lifecycle_map_team_scope_including_deleted
    from modulo.api.team_scope import resolve_lifecycle_map_team_scope

    request = MagicMock()
    request.path_params = {"lifecycle_map_id": str(_MAP_ID)}

    stock_stmts: list[Any] = []
    restore_stmts: list[Any] = []

    def _recorder(bucket: list[Any]) -> AsyncMock:
        async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
            bucket.append(stmt)
            return _result(first=(_TEAM_ID, "team"))

        return AsyncMock(side_effect=_execute)

    stock_session = AsyncMock()
    stock_session.execute = _recorder(stock_stmts)
    restore_session = AsyncMock()
    restore_session.execute = _recorder(restore_stmts)

    stock = await resolve_lifecycle_map_team_scope(request, stock_session)
    restored = await _resolve_lifecycle_map_team_scope_including_deleted(request, restore_session)

    assert stock is not None
    assert restored is not None
    assert restored.owner_team_id == _TEAM_ID
    assert restored.visibility == "team"
    assert stock_stmts, stock_stmts
    assert "deleted_at" in str(stock_stmts[0]), stock_stmts
    assert restore_stmts, restore_stmts
    assert "deleted_at" not in str(restore_stmts[0]), restore_stmts
    assert restore_stmts[0].get_execution_options().get("include_deleted") is True, restore_stmts[0]


async def test_restore_resolver_missing_row_returns_none() -> None:
    """Fail-closed parity with the stock resolver: no row -> None -> 404."""
    from modulo.api.routes.lifecycle_maps import _resolve_lifecycle_map_team_scope_including_deleted

    request = MagicMock()
    request.path_params = {"lifecycle_map_id": str(_MAP_ID)}
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result(first=None))

    assert await _resolve_lifecycle_map_team_scope_including_deleted(request, session) is None


async def test_restore_resolver_missing_path_param_returns_none() -> None:
    from modulo.api.routes.lifecycle_maps import _resolve_lifecycle_map_team_scope_including_deleted

    request = MagicMock()
    request.path_params = {}
    session = AsyncMock()

    assert await _resolve_lifecycle_map_team_scope_including_deleted(request, session) is None
    session.execute.assert_not_awaited()


async def test_restore_resolver_rejects_a_non_uuid_path_param() -> None:
    """A malformed path param is a 400, never an unscoped read."""
    from fastapi import HTTPException

    from modulo.api.routes.lifecycle_maps import _resolve_lifecycle_map_team_scope_including_deleted

    request = MagicMock()
    request.path_params = {"lifecycle_map_id": "not-a-uuid"}
    session = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_lifecycle_map_team_scope_including_deleted(request, session)

    assert exc_info.value.status_code == 400
    session.execute.assert_not_awaited()
