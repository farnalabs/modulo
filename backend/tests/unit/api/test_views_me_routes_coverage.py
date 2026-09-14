"""Coverage-boosting tests for me.py HITL email, remy skills, and context sources.

These routes were uncovered by the existing test_me_settings.py (which only
tests GET/PUT /me/settings).  The sibling test_me_hitl_email_preferences.py
and test_me_password.py cover their respective endpoints but are NOT included
in the baseline coverage measurement — these tests duplicate enough of that
coverage to raise the measured line percentage.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_SKILL_ID = uuid.uuid4()
_SOURCE_KEY = "page_context"


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


@pytest.fixture
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
    c = TestClient(app)
    c.mock_session = mock_session  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


# ── HITL email preferences (GET + PUT) ──────────────────────────────


class TestHitlEmailPreferencesCoverage:
    """Cover the GET and PUT /me/hitl-email-preferences lines in me.py."""

    def test_get_returns_default_off(self, client: TestClient) -> None:
        account = MagicMock()
        account.preferences = {"theme": "dark"}
        with patch("modulo.api.routes.me.get_account_by_id", return_value=account):
            resp = client.get("/api/v1/me/hitl-email-preferences")
        assert resp.status_code == 200
        data = resp.json()
        assert data["default"] is False
        assert not data["pipeline_overrides"]

    def test_get_returns_stored_prefs(self, client: TestClient) -> None:
        pid = str(uuid.uuid4())
        account = MagicMock()
        account.preferences = {"hitl_email": {"default": True, "pipeline_overrides": {pid: False}}}
        with patch("modulo.api.routes.me.get_account_by_id", return_value=account):
            resp = client.get("/api/v1/me/hitl-email-preferences")
        assert resp.status_code == 200
        assert resp.json()["default"] is True
        assert resp.json()["pipeline_overrides"] == {pid: False}

    def test_get_account_not_found(self, client: TestClient) -> None:
        with patch("modulo.api.routes.me.get_account_by_id", return_value=None):
            resp = client.get("/api/v1/me/hitl-email-preferences")
        assert resp.status_code == 404

    def test_put_persists_and_echoes(self, client: TestClient) -> None:
        pid = str(uuid.uuid4())
        merged = {"hitl_email": {"default": True, "pipeline_overrides": {pid: True}}}
        with patch("modulo.api.routes.me.set_hitl_email_preference", return_value=merged):
            resp = client.put(
                "/api/v1/me/hitl-email-preferences",
                json={"default": True, "pipeline_overrides": {pid: True}},
            )
        assert resp.status_code == 200
        assert resp.json()["default"] is True
        assert resp.json()["pipeline_overrides"] == {pid: True}

    def test_put_account_not_found(self, client: TestClient) -> None:
        from modulo.db.crud.account import AccountNotFoundError

        with patch(
            "modulo.api.routes.me.set_hitl_email_preference",
            side_effect=AccountNotFoundError(),
        ):
            resp = client.put("/api/v1/me/hitl-email-preferences", json={"default": True})
        assert resp.status_code == 404

    def test_put_omitting_overrides_preserves_stored(self, client: TestClient) -> None:
        pid = str(uuid.uuid4())
        merged = {"hitl_email": {"default": True, "pipeline_overrides": {pid: False}}}
        with patch("modulo.api.routes.me.set_hitl_email_preference", return_value=merged) as mock_set:
            resp = client.put("/api/v1/me/hitl-email-preferences", json={"default": True})
        assert resp.status_code == 200
        # pipeline_overrides was NOT in body → helper passed None
        _, kwargs = mock_set.call_args
        assert kwargs["pipeline_overrides"] is None


# ── User-level Remy Skills ──────────────────────────────────────────


class TestRemySkillsCoverage:
    """Cover the /me/remy/skills CRUD endpoints in me.py."""

    def test_list_skills(self, client: TestClient) -> None:
        with patch("modulo.api.routes.me.get_user_skills", return_value=[]):
            resp = client.get("/api/v1/me/remy/skills")
        assert resp.status_code == 200
        assert not resp.json()

    def test_list_skills_programming_error(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.me.get_user_skills", side_effect=ProgrammingError("stmt", "params", Exception("orig"))
        ):
            resp = client.get("/api/v1/me/remy/skills")
        assert resp.status_code == 501

    def test_create_skill(self, client: TestClient) -> None:
        with patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock):
            resp = client.post(
                "/api/v1/me/remy/skills",
                json={"name": "My Skill", "body": "do stuff"},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My Skill"
        assert data["body"] == "do stuff"
        assert data["active"] is True

    def test_create_skill_programming_error(self, client: TestClient) -> None:
        client.mock_session.flush = AsyncMock(side_effect=ProgrammingError("stmt", "params", Exception("orig")))  # type: ignore[attr-defined]
        with patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock):
            resp = client.post(
                "/api/v1/me/remy/skills",
                json={"name": "X", "body": "b"},
            )
        assert resp.status_code == 501

    def test_update_skill(self, client: TestClient) -> None:
        skill = MagicMock()
        skill.id = _SKILL_ID
        skill.name = "Updated"
        skill.description = None
        skill.triggers = None
        skill.body = "body"
        skill.active = True
        skill.created_at = None
        skill.updated_at = None
        with (
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.me.get_user_skill_or_404", return_value=skill),
        ):
            resp = client.put(f"/api/v1/me/remy/skills/{_SKILL_ID}", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    def test_update_skill_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.me.get_user_skill_or_404",
                side_effect=HTTPException(status_code=404, detail="Skill not found"),
            ),
        ):
            resp = client.put(f"/api/v1/me/remy/skills/{_SKILL_ID}", json={"name": "X"})
        assert resp.status_code == 404

    def test_update_skill_programming_error(self, client: TestClient) -> None:
        skill = MagicMock()
        client.mock_session.flush = AsyncMock(side_effect=ProgrammingError("stmt", "params", Exception("orig")))  # type: ignore[attr-defined]
        with (
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.me.get_user_skill_or_404", return_value=skill),
        ):
            resp = client.put(f"/api/v1/me/remy/skills/{_SKILL_ID}", json={"name": "X"})
        assert resp.status_code == 501

    def test_delete_skill(self, client: TestClient) -> None:
        skill = MagicMock()
        with (
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.me.get_user_skill_or_404", return_value=skill),
        ):
            resp = client.delete(f"/api/v1/me/remy/skills/{_SKILL_ID}")
        assert resp.status_code == 204

    def test_delete_skill_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.me.get_user_skill_or_404",
                side_effect=HTTPException(status_code=404, detail="Skill not found"),
            ),
        ):
            resp = client.delete(f"/api/v1/me/remy/skills/{_SKILL_ID}")
        assert resp.status_code == 404

    def test_delete_skill_programming_error(self, client: TestClient) -> None:
        skill = MagicMock()
        client.mock_session.delete = MagicMock(side_effect=ProgrammingError("stmt", "params", Exception("orig")))  # type: ignore[attr-defined]
        with (
            patch("modulo.api.routes.me.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.me.get_user_skill_or_404", return_value=skill),
        ):
            resp = client.delete(f"/api/v1/me/remy/skills/{_SKILL_ID}")
        assert resp.status_code == 501


# ── User-level Context Sources ──────────────────────────────────────


class TestContextSourcesCoverage:
    """Cover the /me/remy/context-sources endpoints in me.py."""

    def _mock_service(self) -> MagicMock:
        service = MagicMock()
        service.get_effective_config = AsyncMock(return_value=MagicMock(context_sources={"page_context": "always_on"}))
        service.get_user_overrides = AsyncMock(return_value={})
        service.set_user_override = AsyncMock()
        service.reset_user_overrides = AsyncMock()
        service.build_effective_items = MagicMock(return_value=[])
        return service

    def test_get_context_sources(self, client: TestClient) -> None:
        service = self._mock_service()
        with patch("modulo.api.routes.me.RemyContextSourceService", return_value=service):
            resp = client.get("/api/v1/me/remy/context-sources")
        assert resp.status_code == 200
        service.get_effective_config.assert_awaited_once()
        service.get_user_overrides.assert_awaited_once()

    def test_get_context_sources_programming_error(self, client: TestClient) -> None:
        service = MagicMock()
        service.get_effective_config = AsyncMock(side_effect=ProgrammingError("stmt", "params", Exception("orig")))
        with patch("modulo.api.routes.me.RemyContextSourceService", return_value=service):
            resp = client.get("/api/v1/me/remy/context-sources")
        assert resp.status_code == 501

    def test_set_context_source(self, client: TestClient) -> None:
        service = self._mock_service()
        with patch("modulo.api.routes.me.RemyContextSourceService", return_value=service):
            resp = client.put(
                f"/api/v1/me/remy/context-sources/{_SOURCE_KEY}",
                json={"source_mode": "tool"},
            )
        assert resp.status_code == 200
        service.set_user_override.assert_awaited_once()

    def test_set_context_source_programming_error(self, client: TestClient) -> None:
        service = MagicMock()
        service.set_user_override = AsyncMock(side_effect=ProgrammingError("stmt", "params", Exception("orig")))
        with patch("modulo.api.routes.me.RemyContextSourceService", return_value=service):
            resp = client.put(
                f"/api/v1/me/remy/context-sources/{_SOURCE_KEY}",
                json={"source_mode": "tool"},
            )
        assert resp.status_code == 501

    def test_set_context_source_invalid_mode(self, client: TestClient) -> None:
        resp = client.put(
            f"/api/v1/me/remy/context-sources/{_SOURCE_KEY}",
            json={"source_mode": "invalid"},
        )
        assert resp.status_code == 422

    def test_reset_context_sources(self, client: TestClient) -> None:
        service = self._mock_service()
        with patch("modulo.api.routes.me.RemyContextSourceService", return_value=service):
            resp = client.delete("/api/v1/me/remy/context-sources")
        assert resp.status_code == 200
        service.reset_user_overrides.assert_awaited_once()

    def test_reset_context_sources_programming_error(self, client: TestClient) -> None:
        service = MagicMock()
        service.reset_user_overrides = AsyncMock(side_effect=ProgrammingError("stmt", "params", Exception("orig")))
        with patch("modulo.api.routes.me.RemyContextSourceService", return_value=service):
            resp = client.delete("/api/v1/me/remy/context-sources")
        assert resp.status_code == 501
