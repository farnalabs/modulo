"""Unit tests for /me/remy/skills endpoints — create and list consistency."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = str(uuid.uuid4())
_USER_ID = str(uuid.uuid4())
_SKILL_NAME = "My Test Skill"


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
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind_mock)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    client = TestClient(app)
    client.mock_session = mock_session  # type: ignore[attr-defined]
    yield client
    app.dependency_overrides.clear()


def _make_skill(**overrides: object) -> MagicMock:
    s = MagicMock()
    pk = uuid.uuid4()
    s.id = pk
    s.organisation_id = None
    s.user_id = _USER_ID
    s.name = overrides.get("name", _SKILL_NAME)
    s.description = None
    s.triggers = []
    s.body = "You are a helpful assistant."
    s.active = True
    s.source_mode = None
    s.created_at = None
    s.updated_at = None
    return s


class TestListUserSkills:
    def test_list_user_skills_returns_empty_on_no_skills(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.me.get_user_skills", new_callable=AsyncMock, return_value=[]),
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.get("/api/v1/me/remy/skills")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_user_skills_returns_created_skills(self, client: TestClient) -> None:
        skill = _make_skill()
        with (
            patch("modulo.api.routes.me.get_user_skills", new_callable=AsyncMock, return_value=[skill]),
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.get("/api/v1/me/remy/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == str(skill.id)
        assert data[0]["name"] == _SKILL_NAME

    def test_list_user_skills_calls_set_rls_org(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.me.get_user_skills", new_callable=AsyncMock, return_value=[]),
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock) as mock_set_rls_org,
        ):
            client.get("/api/v1/me/remy/skills")
        mock_set_rls_org.assert_awaited_once()


class TestCreateUserSkill:
    def test_create_skill_returns_201(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
            patch.object(client.mock_session, "add"),  # type: ignore[attr-defined]
            patch.object(client.mock_session, "flush", new_callable=AsyncMock),  # type: ignore[attr-defined]
        ):
            resp = client.post(
                "/api/v1/me/remy/skills",
                json={
                    "name": _SKILL_NAME,
                    "body": "You are a helpful assistant.",
                    "active": True,
                },
            )
        assert resp.status_code == 201

    def test_create_skill_calls_set_rls_org(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock) as mock_set_rls_org,
            patch.object(client.mock_session, "add"),  # type: ignore[attr-defined]
            patch.object(client.mock_session, "flush", new_callable=AsyncMock),  # type: ignore[attr-defined]
        ):
            resp = client.post(
                "/api/v1/me/remy/skills",
                json={
                    "name": _SKILL_NAME,
                    "body": "You are a helpful assistant.",
                    "active": True,
                },
            )
        assert resp.status_code == 201
        mock_set_rls_org.assert_awaited_once()
