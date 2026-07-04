"""Unit tests: library API routes return 501 on ProgrammingError.

Tests that all 12 DB-accessing routes gracefully return 501 Not Implemented
when the database raises ProgrammingError (e.g. missing table because
migrations haven't run yet).
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "00000000-0000-0000-0000-000000000002"
_PRIMITIVE_ID = "00000000-0000-0000-0000-000000000003"
_PIPELINE_ID = "00000000-0000-0000-0000-000000000004"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session_raising_programming_error() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=ProgrammingError("relation does not exist", None, None))
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


@pytest.fixture()
def admin_client() -> TestClient:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _override_session(session) -> None:
    async def _get_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = _get_session


class TestListPrimitivesProgrammingError:
    """GET /api/v1/libraries → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_list_primitives_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get("/api/v1/libraries")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetPrimitiveProgrammingError:
    """GET /api/v1/libraries/{id} → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_get_primitive_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/libraries/{_PRIMITIVE_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestCreatePrimitiveProgrammingError:
    """POST /api/v1/libraries → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_create_primitive_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(
            "/api/v1/libraries",
            json={
                "primitive_type": "schema",
                "name": "Test Schema",
                "slug": "test-schema",
                "content_json": {},
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestUpdatePrimitiveProgrammingError:
    """PATCH /api/v1/libraries/{id} → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_update_primitive_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.patch(
            f"/api/v1/libraries/{_PRIMITIVE_ID}",
            json={"name": "Updated Schema"},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestDeletePrimitiveProgrammingError:
    """DELETE /api/v1/libraries/{id} → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_delete_primitive_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.delete(f"/api/v1/libraries/{_PRIMITIVE_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestCopyToAdaptProgrammingError:
    """POST /api/v1/libraries/{id}/adapt → 501 on ProgrammingError.

    The route handler wraps copy_to_adapt in a try/except ProgrammingError,
    so patching the service function lets us simulate the error directly.
    """

    def test_copy_to_adapt_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        with patch(
            "modulo.api.routes.library.copy_to_adapt",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", {}, ""),
        ):
            resp = admin_client.post(f"/api/v1/libraries/{_PRIMITIVE_ID}/adapt", json={})
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestExportPipelineProgrammingError:
    """POST /api/v1/libraries/export/{id} → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_export_pipeline_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(f"/api/v1/libraries/export/{_PIPELINE_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestCreatePipelineFromTemplateProgrammingError:
    """POST /api/v1/libraries/{id}/create-pipeline → 501 on ProgrammingError.

    The route handler calls get_primitive (from library_service) which has
    its own ProgrammingError catch that returns None.  To trigger the route's
    ProgrammingError handler we patch get_primitive to raise instead.
    """

    def test_create_pipeline_from_template_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        with patch(
            "modulo.api.routes.library.get_primitive",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("mock", {}, ""),
        ):
            resp = admin_client.post(
                f"/api/v1/libraries/{_PRIMITIVE_ID}/create-pipeline",
                json={},
            )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestListRatingsProgrammingError:
    """GET /api/v1/libraries/{id}/ratings → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_list_ratings_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/libraries/{_PRIMITIVE_ID}/ratings")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetRatingAggregateProgrammingError:
    """GET /api/v1/libraries/{id}/ratings/aggregate → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_get_rating_aggregate_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"/api/v1/libraries/{_PRIMITIVE_ID}/ratings/aggregate")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestSubmitRatingProgrammingError:
    """POST /api/v1/libraries/{id}/ratings → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_submit_rating_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(
            f"/api/v1/libraries/{_PRIMITIVE_ID}/ratings",
            json={"thumbs_up": True},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestSubmitAbuseReportProgrammingError:
    """POST /api/v1/libraries/{id}/ratings/abuse → 501 on ProgrammingError.

    The route handler has its own async with session.begin():, so the
    mock session raising ProgrammingError triggers at the route level.
    """

    def test_submit_abuse_report_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(
            f"/api/v1/libraries/{_PRIMITIVE_ID}/ratings/abuse",
            json={"reason": "x" * 10},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()
