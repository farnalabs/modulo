"""Unit tests for /api/v1/libraries endpoints — CRUD, export/import, ratings, community.

Complements test_library_endpoint.py (list filtering + template→pipeline).
Unit tier: no DB — CRUD/service functions are patched at the route-module
boundary and the SQLAlchemy session is a contract-correct AsyncMock.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.library_service import (
    CommunityPrimitiveReadOnlyError,
    ContributionInvalidTransitionError,
    ContributionNotFoundError,
)
from modulo.db.crud.base import PageResult
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _principal(*, is_system_admin: bool = False) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=is_system_admin,
    )


def _build_client(principal: AuthenticatedPrincipal) -> tuple[TestClient, AsyncMock]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    test_client = TestClient(app)
    test_client.mock_session = mock_session  # type: ignore[attr-defined]
    return test_client, mock_session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    test_client, _ = _build_client(_principal())
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def system_admin_client() -> Generator[TestClient, None, None]:
    test_client, _ = _build_client(_principal(is_system_admin=True))
    yield test_client
    app.dependency_overrides.clear()


def _make_listable_primitive(
    *,
    pid: uuid.UUID | None = None,
    primitive_type: str = "workflow",
    name: str = "PR Review Workflow",
    source: str = "local",
    verified: bool | None = None,
    source_url: str | None = None,
) -> MagicMock:
    p = MagicMock()
    p.id = pid or uuid.uuid4()
    p.organisation_id = _ORG_ID
    p.source = source
    p.primitive_type = primitive_type
    p.name = name
    p.slug = "pr-review-workflow"
    p.description = "Automated workflow"
    p.author = _USER_ID.hex
    p.version = "1.0"
    p.tags = ["workflow"]
    p.content_json = {"agents": []}
    p.source_url = source_url
    p.forked_from = None
    p.checksum = None
    p.ed25519_signature = None
    p.verified = verified
    p.trust_tier = None
    p.tier = "native"
    p.download_count = 0
    p.average_rating = None
    p.review_count = 0
    p.owner_team_id = None
    p.visibility = "org"
    p.account_id = _USER_ID
    p.auto_update = True
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


# ---------------------------------------------------------------------------
# ping / get
# ---------------------------------------------------------------------------


def test_ping_returns_pong(client: TestClient) -> None:
    resp = client.get("/api/v1/libraries/ping")
    assert resp.status_code == 200
    assert resp.json() == {"pong": True}


def test_get_primitive_returns_validated_response(client: TestClient) -> None:
    prim = _make_listable_primitive()
    with (
        patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/libraries/{prim.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(prim.id)
    assert body["name"] == "PR Review Workflow"
    assert body["trust_tier"] is None


def test_get_primitive_missing_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/libraries/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_primitive_missing_table_returns_501(client: TestClient) -> None:
    err = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
    with patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, side_effect=err):
        resp = client.get(f"/api/v1/libraries/{uuid.uuid4()}")
    assert resp.status_code == 501


def test_get_primitive_db_error_returns_503(client: TestClient) -> None:
    with patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, side_effect=SQLAlchemyError()):
        resp = client.get(f"/api/v1/libraries/{uuid.uuid4()}")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# create / update / delete / restore
# ---------------------------------------------------------------------------


def _create_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "primitive_type": "workflow",
        "name": "New Workflow",
        "slug": "new-workflow",
        "content_json": {"agents": []},
    }
    payload.update(overrides)
    return payload


def test_create_primitive_returns_201(client: TestClient) -> None:
    prim = _make_listable_primitive(name="New Workflow", primitive_type="workflow")
    with (
        patch("modulo.api.routes.library.get_primitive_by_slug", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.library.create_library_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post("/api/v1/libraries", json=_create_payload())
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "New Workflow"


def test_create_primitive_slug_collision_returns_409(client: TestClient) -> None:
    existing = _make_listable_primitive()
    with patch("modulo.api.routes.library.get_primitive_by_slug", new_callable=AsyncMock, return_value=existing):
        resp = client.post("/api/v1/libraries", json=_create_payload())
    assert resp.status_code == 409


def test_create_primitive_integrity_error_returns_409(client: TestClient) -> None:
    err = IntegrityError("INSERT", {}, Exception("unique constraint"))
    with (
        patch("modulo.api.routes.library.get_primitive_by_slug", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.library.create_library_primitive", new_callable=AsyncMock, side_effect=err),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post("/api/v1/libraries", json=_create_payload())
    assert resp.status_code == 409


def test_create_primitive_invalid_type_rejected(client: TestClient) -> None:
    resp = client.post("/api/v1/libraries", json=_create_payload(primitive_type="bogus"))
    assert resp.status_code == 422


def test_update_primitive_returns_200(client: TestClient) -> None:
    prim = _make_listable_primitive(name="Renamed Workflow")
    with (
        patch("modulo.api.routes.library.update_library_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.patch(f"/api/v1/libraries/{prim.id}", json={"name": "Renamed Workflow"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed Workflow"


def test_update_primitive_missing_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.update_library_primitive", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.patch(f"/api/v1/libraries/{uuid.uuid4()}", json={"name": "Whatever"})
    assert resp.status_code == 404


def test_delete_primitive_returns_200(client: TestClient) -> None:
    prim = _make_listable_primitive()
    with (
        patch("modulo.api.routes.library.soft_delete_library_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.delete(f"/api/v1/libraries/{prim.id}")
    assert resp.status_code == 200, resp.text


def test_delete_primitive_missing_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.soft_delete_library_primitive", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.delete(f"/api/v1/libraries/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_restore_primitive_returns_200(client: TestClient) -> None:
    prim = _make_listable_primitive()
    with (
        patch("modulo.api.routes.library.restore_library_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{prim.id}/restore")
    assert resp.status_code == 200, resp.text


def test_restore_primitive_not_deleted_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.restore_library_primitive", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{uuid.uuid4()}/restore")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# copy-to-adapt
# ---------------------------------------------------------------------------


def test_copy_to_adapt_returns_200(client: TestClient) -> None:
    prim = _make_listable_primitive()
    with (
        patch("modulo.api.routes.library.copy_to_adapt", new_callable=AsyncMock, return_value=prim),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{prim.id}/adapt", json={})
    assert resp.status_code == 200, resp.text


def test_copy_to_adapt_community_read_only_returns_403(client: TestClient) -> None:
    with (
        patch(
            "modulo.api.routes.library.copy_to_adapt",
            new_callable=AsyncMock,
            side_effect=CommunityPrimitiveReadOnlyError("read only"),
        ),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{uuid.uuid4()}/adapt", json={})
    assert resp.status_code == 403


def test_copy_to_adapt_missing_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.copy_to_adapt", new_callable=AsyncMock, side_effect=LookupError("nope")),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{uuid.uuid4()}/adapt", json={})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def _make_pipeline_mock() -> MagicMock:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.name = "My Pipeline/v1"
    return p


def test_export_pipeline_v1_returns_zip(client: TestClient) -> None:
    pipeline = _make_pipeline_mock()
    with (
        patch("modulo.api.routes.library.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
        patch("modulo.api.routes.library.export_pipeline_bundle", new_callable=AsyncMock, return_value=b"zipbytes"),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/export/{pipeline.id}", params={"format": "v1"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert "My_Pipeline_v1.modulo.zip" in resp.headers["content-disposition"]


def test_export_pipeline_v2_returns_yaml(client: TestClient) -> None:
    pipeline = _make_pipeline_mock()
    with (
        patch("modulo.api.routes.library.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
        patch("modulo.api.routes.library.export_pipeline_bundle_v2", new_callable=AsyncMock, return_value="a: 1"),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/export/{pipeline.id}", params={"format": "v2"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/x-yaml"


def test_export_pipeline_missing_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.get_pipeline", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/export/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_export_pipeline_invalid_format_rejected(client: TestClient) -> None:
    resp = client.post(f"/api/v1/libraries/export/{uuid.uuid4()}", params={"format": "v9"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# import: upload-zip / analyse / confirm
# ---------------------------------------------------------------------------


def _bundle_resolution(**overrides: Any) -> Any:
    from modulo.api.routes.library import _BundleResolution

    defaults: dict[str, Any] = {
        "pipeline_name": "Imported Pipeline",
        "warnings": ["Watch out"],
        "resolved_schemas": [],
        "resolved_connectors": [],
        "resolved_model_backends": [],
        "name_conflicts": [],
        "available_teams": [{"id": str(uuid.uuid4()), "name": "Team A"}],
    }
    defaults.update(overrides)
    return _BundleResolution(**defaults)


def test_upload_zip_happy_path_returns_analysis(client: TestClient) -> None:
    bundle = {"pipeline": {"name": "Zipped"}}
    with (
        patch("modulo.api.routes.library.extract_bundle_json_from_zip", return_value=bundle),
        patch(
            "modulo.api.routes.library._resolve_import_bundle",
            new_callable=AsyncMock,
            return_value=_bundle_resolution(pipeline_name="Zipped"),
        ),
    ):
        resp = client.post(
            "/api/v1/libraries/import/upload-zip",
            files={"file": ("bundle.modulo.zip", b"PK", "application/zip")},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pipeline_name"] == "Zipped"
    assert body["warnings"] == ["Watch out"]


def test_upload_zip_rejects_non_zip_filename(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/libraries/import/upload-zip",
        files={"file": ("bundle.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400
    assert ".zip" in resp.json()["detail"]


def test_upload_zip_missing_bundle_json_returns_400(client: TestClient) -> None:
    with patch(
        "modulo.api.routes.library.extract_bundle_json_from_zip",
        side_effect=LookupError("bundle.json not found in zip"),
    ):
        resp = client.post(
            "/api/v1/libraries/import/upload-zip",
            files={"file": ("bundle.zip", b"PK", "application/zip")},
        )
    assert resp.status_code == 400


def test_read_zip_upload_enforces_size_limit() -> None:
    from modulo.api.routes.library import _MAX_UPLOAD_SIZE, _read_zip_upload

    oversized = MagicMock()
    oversized.filename = "big.zip"
    oversized.size = _MAX_UPLOAD_SIZE + 1
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_read_zip_upload(oversized))
    assert exc_info.value.status_code == 413


def test_analyse_bundle_returns_resolution(client: TestClient) -> None:
    with patch(
        "modulo.api.routes.library._resolve_import_bundle",
        new_callable=AsyncMock,
        return_value=_bundle_resolution(),
    ):
        resp = client.post("/api/v1/libraries/import/analyse", json={"bundle": {"pipeline": {"name": "X"}}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pipeline_name"] == "Imported Pipeline"
    assert body["bundle_json"]


def test_analyse_bundle_db_error_returns_503(client: TestClient) -> None:
    with patch(
        "modulo.api.routes.library._resolve_import_bundle",
        new_callable=AsyncMock,
        side_effect=SQLAlchemyError(),
    ):
        resp = client.post("/api/v1/libraries/import/analyse", json={"bundle": {}})
    assert resp.status_code == 503


def test_confirm_import_returns_summary(client: TestClient) -> None:
    materialized = {
        "pipeline_id": uuid.uuid4(),
        "pipeline_name": "Imported",
        "primitive_id": uuid.uuid4(),
        "agent_count": 2,
        "edge_count": 1,
        "schema_count": 1,
        "warnings": [],
    }
    with (
        patch("modulo.api.routes.library.materialize_import", new_callable=AsyncMock, return_value=materialized),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post("/api/v1/libraries/import/confirm", json={"bundle_json": json.dumps({"pipeline": {}})})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "imported"
    assert body["agent_count"] == 2


def test_confirm_import_invalid_json_returns_400(client: TestClient) -> None:
    resp = client.post("/api/v1/libraries/import/confirm", json={"bundle_json": "{not json"})
    assert resp.status_code == 400


def test_confirm_import_missing_table_returns_501(client: TestClient) -> None:
    err = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
    with (
        patch("modulo.api.routes.library.materialize_import", new_callable=AsyncMock, side_effect=err),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post("/api/v1/libraries/import/confirm", json={"bundle_json": "{}"})
    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# ratings
# ---------------------------------------------------------------------------


def _make_rating() -> MagicMock:
    r = MagicMock()
    r.id = uuid.uuid4()
    r.primitive_id = uuid.uuid4()
    r.user_id = _USER_ID
    r.thumbs_up = True
    r.comment = "Great"
    r.created_at = _NOW
    return r


def test_list_ratings_returns_items(client: TestClient) -> None:
    page = PageResult(items=[_make_rating()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.library.list_ratings_for_primitive", new_callable=AsyncMock, return_value=page),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/libraries/{uuid.uuid4()}/ratings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_rating_aggregate_returns_values(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.get_rating_aggregate", new_callable=AsyncMock, return_value=(4.5, 2)),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/libraries/{uuid.uuid4()}/ratings/aggregate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["average_rating"] == 4.5
    assert body["review_count"] == 2


def test_submit_rating_returns_201(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.submit_rating", new_callable=AsyncMock, return_value=_make_rating()),
        patch("modulo.api.routes.library.update_primitive_ratings_aggregate", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{uuid.uuid4()}/ratings", json={"thumbs_up": True})
    assert resp.status_code == 201, resp.text


def test_submit_rating_self_rating_forbidden(client: TestClient) -> None:
    from modulo.db.crud.rating import SelfRatingError

    with (
        patch(
            "modulo.api.routes.library.submit_rating",
            new_callable=AsyncMock,
            side_effect=SelfRatingError("cannot rate own primitive"),
        ),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{uuid.uuid4()}/ratings", json={"thumbs_up": True})
    assert resp.status_code == 403


def test_submit_rating_duplicate_returns_409(client: TestClient) -> None:
    from modulo.db.crud.rating import DuplicateRatingError

    with (
        patch(
            "modulo.api.routes.library.submit_rating",
            new_callable=AsyncMock,
            side_effect=DuplicateRatingError("already rated"),
        ),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{uuid.uuid4()}/ratings", json={"thumbs_up": True})
    assert resp.status_code == 409


def test_submit_rating_cooldown_returns_429(client: TestClient) -> None:
    from modulo.db.crud.rating import RatingCooldownError

    with (
        patch(
            "modulo.api.routes.library.submit_rating",
            new_callable=AsyncMock,
            side_effect=RatingCooldownError("slow down"),
        ),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{uuid.uuid4()}/ratings", json={"thumbs_up": True})
    assert resp.status_code == 429


def test_submit_rating_overlong_comment_rejected(client: TestClient) -> None:
    resp = client.post(
        f"/api/v1/libraries/{uuid.uuid4()}/ratings",
        json={"thumbs_up": True, "comment": "x" * 2001},
    )
    assert resp.status_code == 422


def test_submit_abuse_report_returns_201(client: TestClient) -> None:
    report = MagicMock()
    report.id = uuid.uuid4()
    report.primitive_id = uuid.uuid4()
    report.rating_id = None
    report.reporter_user_id = _USER_ID
    report.reason = "Inappropriate content here"
    report.status = "open"
    report.created_at = _NOW
    with (
        patch("modulo.api.routes.library.submit_abuse_report", new_callable=AsyncMock, return_value=report),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/libraries/{uuid.uuid4()}/ratings/abuse",
            json={"reason": "Inappropriate content here"},
        )
    assert resp.status_code == 201, resp.text


def test_submit_abuse_report_short_reason_rejected(client: TestClient) -> None:
    resp = client.post(f"/api/v1/libraries/{uuid.uuid4()}/ratings/abuse", json={"reason": "short"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# create-lifecycle-map
# ---------------------------------------------------------------------------


def _make_lifecycle_map_mock() -> MagicMock:
    lm = MagicMock()
    lm.id = uuid.uuid4()
    lm.organisation_id = _ORG_ID
    lm.name = "Imported Map"
    lm.description = None
    lm.owner_team_id = None
    lm.visibility = "org"
    lm.version = 1
    lm.content_json = {"stages": []}
    lm.archived_at = None
    lm.created_at = _NOW
    lm.updated_at = _NOW
    return lm


def test_create_lifecycle_map_returns_201(client: TestClient) -> None:
    prim = _make_listable_primitive(primitive_type="lifecycle_map", name="Map Template")
    lifecycle_map = _make_lifecycle_map_mock()
    with (
        patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=prim),
        patch(
            "modulo.api.routes.library.materialize_map_from_primitive",
            new_callable=AsyncMock,
            return_value=lifecycle_map,
        ),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{prim.id}/create-lifecycle-map")
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "Imported Map"


def test_create_lifecycle_map_wrong_type_returns_422(client: TestClient) -> None:
    prim = _make_listable_primitive(primitive_type="workflow")
    with (
        patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{prim.id}/create-lifecycle-map")
    assert resp.status_code == 422


def test_create_lifecycle_map_missing_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{uuid.uuid4()}/create-lifecycle-map")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# community contributions
# ---------------------------------------------------------------------------


def test_community_contribute_returns_201(client: TestClient) -> None:
    prim = _make_listable_primitive(source_url="https://example.com/p")
    with (
        patch("modulo.api.routes.library.contribute_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/libraries/community/contribute",
            json=_create_payload(name="Contribution", slug="contribution", source_url="https://example.com/p"),
        )
    assert resp.status_code == 201, resp.text


def test_community_contribute_conflict_returns_409(client: TestClient) -> None:
    err = IntegrityError("INSERT", {}, Exception("unique"))
    with (
        patch("modulo.api.routes.library.contribute_primitive", new_callable=AsyncMock, side_effect=err),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/libraries/community/contribute",
            json=_create_payload(name="Contribution", slug="contribution"),
        )
    assert resp.status_code == 409


def test_list_community_contributions_returns_items(client: TestClient) -> None:
    page = PageResult(items=[_make_listable_primitive()], total=1, page=1, page_size=20)
    with patch("modulo.api.routes.library.list_org_contributions", new_callable=AsyncMock, return_value=page):
        resp = client.get("/api/v1/libraries/community/contributions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_admin_publish_contribution_returns_200(system_admin_client: TestClient) -> None:
    prim = _make_listable_primitive(source="registry", verified=True)
    with patch("modulo.api.routes.library.publish_contribution", new_callable=AsyncMock, return_value=prim):
        resp = system_admin_client.post(f"/api/v1/libraries/admin/library/community/publish/{prim.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["trust_tier"] == "green"


def test_admin_publish_missing_contribution_returns_404(system_admin_client: TestClient) -> None:
    with patch(
        "modulo.api.routes.library.publish_contribution",
        new_callable=AsyncMock,
        side_effect=ContributionNotFoundError,
    ):
        resp = system_admin_client.post(f"/api/v1/libraries/admin/library/community/publish/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_admin_publish_invalid_transition_returns_400(system_admin_client: TestClient) -> None:
    with patch(
        "modulo.api.routes.library.publish_contribution",
        new_callable=AsyncMock,
        side_effect=ContributionInvalidTransitionError("cannot publish"),
    ):
        resp = system_admin_client.post(f"/api/v1/libraries/admin/library/community/publish/{uuid.uuid4()}")
    assert resp.status_code == 400


def test_admin_publish_requires_system_admin(client: TestClient) -> None:
    resp = client.post(f"/api/v1/libraries/admin/library/community/publish/{uuid.uuid4()}")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def test_split_primitive_types_variants() -> None:
    from modulo.api.routes.library import _split_primitive_types

    assert _split_primitive_types("workflow, agent") == ["workflow", "agent"]
    assert _split_primitive_types("workflow,agent,schema") == ["workflow", "agent", "schema"]
    assert _split_primitive_types(",  ,") is None
    assert _split_primitive_types("") is None
    assert _split_primitive_types(None) is None


def test_trust_tier_for_provenance() -> None:
    from modulo.api.routes.library import _trust_tier_for

    assert _trust_tier_for("modulo", None) == "modulo"
    assert _trust_tier_for("registry", True) == "green"
    assert _trust_tier_for("registry", False) == "amber"
    assert _trust_tier_for("local", None) is None


def test_require_organisation_id_rejects_missing_org() -> None:
    from modulo.api.routes.library import _require_organisation_id

    anon = AuthenticatedPrincipal(username="u", organisation_id=None, account_id=_USER_ID, org_role=None)
    with pytest.raises(HTTPException) as exc_info:
        _require_organisation_id(anon)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400


def test_warn_duplicate_agent_names() -> None:
    from modulo.api.routes.library import _warn_duplicate_agent_names

    bundle: dict[str, Any] = {"agents": [{"name": "A"}, {"name": "A"}, {"name": "B"}]}
    warnings: list[str] = []
    _warn_duplicate_agent_names(bundle, warnings)
    assert len(warnings) == 1
    assert "A" in warnings[0]


# ---------------------------------------------------------------------------
# Error-convention mapping (ProgrammingError→501, SQLAlchemyError→503,
# IntegrityError→409) per endpoint family
# ---------------------------------------------------------------------------

_PROGRAMMING = ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
_INTEGRITY = IntegrityError("INSERT", {}, Exception("unique constraint"))


def _rls_patches() -> tuple[Any, Any]:
    return (
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    )


def _request(method: str, path: str, *, json_body: Any = None, params: Any = None, files: Any = None) -> Any:
    def _call(c: TestClient) -> Any:
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if params is not None:
            kwargs["params"] = params
        if files is not None:
            kwargs["files"] = files
        return getattr(c, method)(path, **kwargs)

    return _call


def _assert_error_mapping(client: TestClient, patch_target: str, call: Any, cases: list[tuple[Exception, int]]) -> None:
    for exc, expected in cases:
        with ExitStack() as stack:
            stack.enter_context(patch(patch_target, new_callable=AsyncMock, side_effect=exc))
            if "set_rls_org" not in patch_target:
                stack.enter_context(patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock))
            if "set_rls_user_context" not in patch_target:
                stack.enter_context(patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock))
            resp = call(client)
        assert resp.status_code == expected, f"{patch_target} with {type(exc).__name__}: got {resp.status_code}"


_GET_409_501_503 = [(_INTEGRITY, 409), (_PROGRAMMING, 501), (SQLAlchemyError(), 503)]
_GET_501_503 = [(_PROGRAMMING, 501), (SQLAlchemyError(), 503)]


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_get_library_primitive_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.get_primitive",
        _request("get", f"/api/v1/libraries/{uuid.uuid4()}"),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_501_503, ids=["501", "503"])
def test_list_library_primitives_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.set_rls_org",
        _request("get", "/api/v1/libraries"),
        [(exc, expected)],
    )


def test_list_library_primitives_unexpected_error_returns_500(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock, side_effect=RuntimeError),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/libraries")
    assert resp.status_code == 500


def test_list_library_primitives_invalid_item_returns_500(client: TestClient) -> None:
    result = PageResult(items=[MagicMock()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.library.list_primitives", new_callable=AsyncMock, return_value=result),
        _rls_patches()[0],
        _rls_patches()[1],
    ):
        resp = client.get("/api/v1/libraries")
    assert resp.status_code == 500
    assert "out of sync" in resp.json()["detail"]


def test_build_primitive_list_response_invalid_result_returns_500() -> None:
    from modulo.api.routes.library import _build_primitive_list_response

    bogus = SimpleNamespace(total="not-an-int", page="x", page_size="y", next_cursor=None, has_more=False)
    with pytest.raises(HTTPException) as exc_info:
        _build_primitive_list_response([], bogus)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 500


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_create_library_primitive_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    with_exit = patch("modulo.api.routes.library.create_library_primitive", new_callable=AsyncMock, side_effect=exc)
    with (
        with_exit,
        patch("modulo.api.routes.library.get_primitive_by_slug", new_callable=AsyncMock, return_value=None),
        _rls_patches()[0],
        _rls_patches()[1],
    ):
        resp = client.post("/api/v1/libraries", json=_create_payload())
    assert resp.status_code == expected


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_update_library_primitive_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.update_library_primitive",
        _request("patch", f"/api/v1/libraries/{uuid.uuid4()}", json_body={"name": "X"}),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_delete_library_primitive_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.soft_delete_library_primitive",
        _request("delete", f"/api/v1/libraries/{uuid.uuid4()}"),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_501_503, ids=["501", "503"])
def test_restore_library_primitive_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.restore_library_primitive",
        _request("post", f"/api/v1/libraries/{uuid.uuid4()}/restore"),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_copy_to_adapt_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.copy_to_adapt",
        _request("post", f"/api/v1/libraries/{uuid.uuid4()}/adapt", json_body={}),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_export_pipeline_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.get_pipeline",
        _request("post", f"/api/v1/libraries/export/{uuid.uuid4()}"),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_confirm_import_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.materialize_import",
        _request("post", "/api/v1/libraries/import/confirm", json_body={"bundle_json": "{}"}),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_list_ratings_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.list_ratings_for_primitive",
        _request("get", f"/api/v1/libraries/{uuid.uuid4()}/ratings"),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_rating_aggregate_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.get_rating_aggregate",
        _request("get", f"/api/v1/libraries/{uuid.uuid4()}/ratings/aggregate"),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_submit_rating_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.submit_rating",
        _request("post", f"/api/v1/libraries/{uuid.uuid4()}/ratings", json_body={"thumbs_up": True}),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_submit_abuse_report_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.submit_abuse_report",
        _request("post", f"/api/v1/libraries/{uuid.uuid4()}/ratings/abuse", json_body={"reason": "x" * 20}),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_create_lifecycle_map_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    prim = _make_listable_primitive(primitive_type="lifecycle_map")
    with (
        patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=prim),
        patch(
            "modulo.api.routes.library.materialize_map_from_primitive",
            new_callable=AsyncMock,
            side_effect=exc,
        ),
        _rls_patches()[0],
        _rls_patches()[1],
    ):
        resp = client.post(f"/api/v1/libraries/{prim.id}/create-lifecycle-map")
    assert resp.status_code == expected


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_create_pipeline_from_template_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    prim = _make_listable_primitive(primitive_type="pipeline_template")
    with (
        patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.api.routes.library.create_pipeline", new_callable=AsyncMock, side_effect=exc),
        _rls_patches()[0],
        _rls_patches()[1],
    ):
        resp = client.post(f"/api/v1/libraries/{prim.id}/create-pipeline", json={})
    assert resp.status_code == expected


@pytest.mark.parametrize(("exc", "expected"), _GET_501_503, ids=["501", "503"])
def test_community_contribute_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.contribute_primitive",
        _request("post", "/api/v1/libraries/community/contribute", json_body=_create_payload()),
        [(exc, expected)],
    )


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_list_community_contributions_error_mapping(client: TestClient, exc: Exception, expected: int) -> None:
    _assert_error_mapping(
        client,
        "modulo.api.routes.library.list_org_contributions",
        _request("get", "/api/v1/libraries/community/contributions"),
        [(exc, expected)],
    )


def test_list_community_contributions_unexpected_error_returns_500(client: TestClient) -> None:
    with patch("modulo.api.routes.library.list_org_contributions", new_callable=AsyncMock, side_effect=RuntimeError):
        resp = client.get("/api/v1/libraries/community/contributions")
    assert resp.status_code == 500


@pytest.mark.parametrize(("exc", "expected"), _GET_409_501_503, ids=["409", "501", "503"])
def test_admin_publish_error_mapping(system_admin_client: TestClient, exc: Exception, expected: int) -> None:
    with patch("modulo.api.routes.library.publish_contribution", new_callable=AsyncMock, side_effect=exc):
        resp = system_admin_client.post(f"/api/v1/libraries/admin/library/community/publish/{uuid.uuid4()}")
    assert resp.status_code == expected


def test_submit_rating_copy_to_adapt_error_returns_403(client: TestClient) -> None:
    from modulo.db.crud.rating import CopyToAdaptError

    with (
        patch(
            "modulo.api.routes.library.submit_rating",
            new_callable=AsyncMock,
            side_effect=CopyToAdaptError("not allowed"),
        ),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{uuid.uuid4()}/ratings", json={"thumbs_up": True})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Bundle resolution (real _resolve_import_bundle with sub-resolvers patched)
# ---------------------------------------------------------------------------


def _full_bundle() -> dict[str, Any]:
    return {
        "pipeline": {
            "name": "Existing",
            "graph_nodes_json": [{"connector_binding": {"connector_type_id": "ct-1"}}],
        },
        "schemas": [{"name": "S1"}],
        "agents": [
            {
                "name": "AgentA",
                "connector_type_refs": [{"connector_type_id": "ct-1"}],
                "model_backend_name": "MB1",
            },
            {"name": "AgentA"},
        ],
        "model_backends": [{"name": "MB1"}],
    }


def test_resolve_import_bundle_stamps_ids_and_warnings(client: TestClient) -> None:
    import modulo.api.routes.library as lib

    team = MagicMock()
    team.id = uuid.uuid4()
    team.name = "Team A"

    async def _fake_teams(session: Any, principal: Any) -> list[MagicMock]:
        return [team]

    with (
        patch(
            "modulo.api.routes.library.get_existing_pipeline_names",
            new_callable=AsyncMock,
            return_value={"Existing"},
        ),
        patch(
            "modulo.api.routes.library.resolve_schema",
            new_callable=AsyncMock,
            return_value={"schema_id": "s-1", "version": 1, "warning": "schema warning"},
        ),
        patch(
            "modulo.api.routes.library.resolve_connector_type",
            new_callable=AsyncMock,
            return_value={"instance_id": "i-1", "warning": None},
        ),
        patch(
            "modulo.api.routes.library.resolve_model_backend",
            new_callable=AsyncMock,
            return_value={"model_backend_id": "mb-1", "warning": "backend warning"},
        ),
        patch("modulo.api.routes.library.get_existing_agent_names", new_callable=AsyncMock, return_value={"AgentA"}),
        patch("modulo.api.routes.library._fetch_available_teams", side_effect=_fake_teams),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resolution = asyncio.run(
            lib._resolve_import_bundle(client.mock_session, _principal(), _full_bundle())  # type: ignore[attr-defined]
        )

    assert resolution.pipeline_name == "Existing"
    assert resolution.resolved_schemas == [{"schema_id": "s-1", "version": 1, "warning": "schema warning"}]
    assert resolution.resolved_connectors == [{"instance_id": "i-1", "warning": None}]
    assert resolution.resolved_model_backends == [{"model_backend_id": "mb-1", "warning": "backend warning"}]
    assert {"type": "pipeline", "original": "Existing", "suggested": "Existing (imported)"} in resolution.name_conflicts
    assert {"type": "agent", "original": "AgentA"} in [
        {"type": nc["type"], "original": nc["original"]} for nc in resolution.name_conflicts
    ]
    assert "Duplicate agent name 'AgentA' found in bundle. Each agent must have a unique name." in resolution.warnings
    assert resolution.available_teams[0]["name"] == "Team A"
    assert client.mock_session.add.call_count == 0  # type: ignore[attr-defined]


def test_analyse_endpoint_with_real_resolver_returns_conflicts(client: TestClient) -> None:
    team = MagicMock()
    team.id = uuid.uuid4()
    team.name = "Team A"

    async def _fake_teams(session: Any, principal: Any) -> list[MagicMock]:
        return [team]

    with (
        patch("modulo.api.routes.library.get_existing_pipeline_names", new_callable=AsyncMock, return_value=set()),
        patch(
            "modulo.api.routes.library.resolve_schema",
            new_callable=AsyncMock,
            return_value={"schema_id": "s-1", "version": 1},
        ),
        patch(
            "modulo.api.routes.library.resolve_connector_type",
            new_callable=AsyncMock,
            return_value={"instance_id": "i-1"},
        ),
        patch(
            "modulo.api.routes.library.resolve_model_backend",
            new_callable=AsyncMock,
            return_value={"model_backend_id": "mb-1"},
        ),
        patch("modulo.api.routes.library.get_existing_agent_names", new_callable=AsyncMock, return_value=set()),
        patch("modulo.api.routes.library._fetch_available_teams", side_effect=_fake_teams),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post("/api/v1/libraries/import/analyse", json={"bundle": _full_bundle()})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pipeline_name"] == "Existing"
    assert "Duplicate agent name 'AgentA'" in " ".join(body["warnings"])
    binding_node = json.loads(body["bundle_json"])["pipeline"]["graph_nodes_json"][0]
    assert binding_node["connector_binding"]["instance_id"] == "i-1"


def test_analyse_bundle_integrity_error_returns_409(client: TestClient) -> None:
    with patch(
        "modulo.api.routes.library._resolve_import_bundle",
        new_callable=AsyncMock,
        side_effect=_INTEGRITY,
    ):
        resp = client.post("/api/v1/libraries/import/analyse", json={"bundle": {}})
    assert resp.status_code == 409


def test_analyse_bundle_missing_table_returns_501(client: TestClient) -> None:
    with patch(
        "modulo.api.routes.library._resolve_import_bundle",
        new_callable=AsyncMock,
        side_effect=_PROGRAMMING,
    ):
        resp = client.post("/api/v1/libraries/import/analyse", json={"bundle": {}})
    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Remaining helper coverage
# ---------------------------------------------------------------------------


def test_read_zip_upload_rejects_oversized_content() -> None:
    from modulo.api.routes.library import _MAX_UPLOAD_SIZE, _read_zip_upload

    big_file = MagicMock()
    big_file.filename = "big.zip"
    big_file.size = None
    big_file.read = AsyncMock(return_value=b"x" * (_MAX_UPLOAD_SIZE + 1))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_read_zip_upload(big_file))
    assert exc_info.value.status_code == 413


def test_node_label_and_edge_helpers() -> None:
    from modulo.api.routes.library import _convert_template_edges, _node_label

    agents: list[dict[str, Any]] = [{"name": "A1"}]
    assert _node_label({"node_type": "manual", "label": "Gate"}, agents, None) == "Gate"
    assert _node_label({"agent_index": 0}, agents, 0) == "A1"
    assert _node_label({"label": "Custom"}, agents, 0) == "Custom"
    assert _node_label({}, agents, None) == "Node"
    node_map = {"a": str(uuid.uuid4()), "b": str(uuid.uuid4())}
    edges = _convert_template_edges(
        [
            {"source": "a", "target": "b", "edge_type": "normal", "hitl_gate_config": {"timeout": 1}},
            {"source_node_id": "missing", "target_node_id": "b"},
        ],
        node_map,
    )
    assert edges[0]["source_node_id"] == node_map["a"]
    assert edges[0]["hitl_gate_config"] == {"timeout": 1}
    assert edges[1]["edge_type"] == "normal"
    assert edges[1]["source_node_id"] == "missing"


def test_bind_helpers_stamp_resolved_ids() -> None:
    from modulo.api.routes.library import _bind_connector_instances_to_graph, _bind_model_backends_to_agents

    pipeline_info: dict[str, Any] = {"graph_nodes_json": [{"connector_binding": {"connector_type_id": "ct-1"}}]}
    _bind_connector_instances_to_graph(pipeline_info, {"ct-1": "i-1"})
    assert pipeline_info["graph_nodes_json"][0]["connector_binding"]["instance_id"] == "i-1"

    bundle: dict[str, Any] = {"agents": [{"model_backend_name": "MB1"}]}
    _bind_model_backends_to_agents(bundle, {"MB1": "mb-1"})
    assert bundle["agents"][0]["model_backend_id"] == "mb-1"
