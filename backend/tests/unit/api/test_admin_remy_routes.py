"""Unit tests for /api/v1/admin/remy endpoints (config, skills, context sources).

Unit tier: no DB — the SQLAlchemy session is a contract-correct AsyncMock and
the RemyContextSourceService is patched at its source module.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.remy_skill import RemySkill
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


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


def _queue_executes(session: AsyncMock, *results: Any) -> None:
    """Route execute() calls: authz-enforce reads get a benign result, others consume the queue."""
    queue = list(results)

    async def _execute(stmt: object, *_a: object, **_k: object) -> Any:
        if "authz_enforce" in str(stmt):
            benign = MagicMock()
            benign.scalar_one_or_none.return_value = None
            return benign
        if not queue:
            raise AssertionError("Unexpected session.execute() — no more stubbed results")
        return queue.pop(0)

    session.execute = AsyncMock(side_effect=_execute)


def _failing_executes(session: AsyncMock, exc: Exception) -> None:
    """Make every non-authz execute() raise; authz-enforce reads fail closed upstream."""

    async def _execute(stmt: object, *_a: object, **_k: object) -> Any:
        if "authz_enforce" in str(stmt):
            benign = MagicMock()
            benign.scalar_one_or_none.return_value = None
            return benign
        raise exc

    session.execute = AsyncMock(side_effect=_execute)


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
    test_client = TestClient(app)
    test_client.mock_session = mock_session  # type: ignore[attr-defined]
    yield test_client
    app.dependency_overrides.clear()


def _config_result(entry: MagicMock | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = entry
    return result


def _make_config_entry(value: dict[str, Any]) -> MagicMock:
    entry = MagicMock()
    entry.value = value
    return entry


def _make_skill(account_id: uuid.UUID | None = None) -> RemySkill:
    return RemySkill(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID if account_id is None else None,
        account_id=account_id,
        name="Grooming Skill",
        description="Helps groom tickets",
        triggers=["groom"],
        body="Groom the ticket body",
        active=True,
    )


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------


def test_get_config_returns_defaults_when_unset(client: TestClient) -> None:
    _queue_executes(client.mock_session, _config_result(None))  # type: ignore[attr-defined]
    resp = client.get("/api/v1/admin/remy/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default_provider"] == "anthropic"
    assert body["default_context_window"] == 200000


def test_get_config_returns_stored_values(client: TestClient) -> None:
    entry = _make_config_entry(
        {
            "system_prompt": "Be concise",
            "default_provider": "openai",
            "default_model": "gpt-4o",
            "access_list": {"org_roles": ["admin"]},
        }
    )
    _queue_executes(client.mock_session, _config_result(entry))  # type: ignore[attr-defined]
    resp = client.get("/api/v1/admin/remy/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["system_prompt"] == "Be concise"
    assert body["default_provider"] == "openai"
    assert body["access_list"]["org_roles"] == ["admin"]


def test_get_config_db_error_returns_503(client: TestClient) -> None:
    _failing_executes(client.mock_session, SQLAlchemyError("connection refused"))  # type: ignore[attr-defined]
    resp = client.get("/api/v1/admin/remy/config")
    assert resp.status_code == 503


def test_get_config_missing_table_returns_501(client: TestClient) -> None:
    _failing_executes(  # type: ignore[attr-defined]
        client.mock_session,
        ProgrammingError("SELECT 1", {}, Exception("relation does not exist")),
    )
    resp = client.get("/api/v1/admin/remy/config")
    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# PUT /config
# ---------------------------------------------------------------------------


def test_update_config_creates_entry_when_missing(client: TestClient) -> None:
    _queue_executes(client.mock_session, _config_result(None))  # type: ignore[attr-defined]
    resp = client.put(
        "/api/v1/admin/remy/config",
        json={"system_prompt": "Be helpful", "default_provider": "anthropic"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["system_prompt"] == "Be helpful"


def test_update_config_updates_existing_entry(client: TestClient) -> None:
    entry = _make_config_entry({"system_prompt": "Old"})
    _queue_executes(client.mock_session, _config_result(entry))  # type: ignore[attr-defined]
    resp = client.put("/api/v1/admin/remy/config", json={"system_prompt": "New prompt"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["system_prompt"] == "New prompt"


def test_update_config_rejects_unsupported_provider(client: TestClient) -> None:
    resp = client.put(
        "/api/v1/admin/remy/config",
        json={"allowed_providers": ["not-a-provider"]},
    )
    assert resp.status_code == 422
    assert "Unsupported providers" in resp.json()["detail"]


def test_update_config_accepts_supported_providers(client: TestClient) -> None:
    _queue_executes(client.mock_session, _config_result(None))  # type: ignore[attr-defined]
    resp = client.put(
        "/api/v1/admin/remy/config",
        json={"allowed_providers": ["openai", "anthropic"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["allowed_providers"] == ["openai", "anthropic"]


# ---------------------------------------------------------------------------
# GET /available-providers
# ---------------------------------------------------------------------------


def test_available_providers_lists_native_and_custom(client: TestClient) -> None:
    resp = client.get("/api/v1/admin/remy/available-providers")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    native_ids = [p["id"] for p in body["native"]]
    custom_ids = [p["id"] for p in body["custom_types"]]
    assert "anthropic" in native_ids
    assert "openai" in native_ids
    assert not set(native_ids) & set(custom_ids)
    assert "azure_openai" in custom_ids
    label_by_id = {p["id"]: p["label"] for p in body["native"]}
    assert label_by_id["anthropic"] == "Anthropic"


# ---------------------------------------------------------------------------
# Skills CRUD
# ---------------------------------------------------------------------------


def test_list_org_skills_returns_items(client: TestClient) -> None:
    skill = _make_skill()
    result = MagicMock()
    result.scalars.return_value = iter([skill])
    _queue_executes(client.mock_session, result)  # type: ignore[attr-defined]
    resp = client.get("/api/v1/admin/remy/skills")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "Grooming Skill"
    assert body[0]["active"] is True


def test_list_org_skills_empty(client: TestClient) -> None:
    result = MagicMock()
    result.scalars.return_value = iter([])
    _queue_executes(client.mock_session, result)  # type: ignore[attr-defined]
    resp = client.get("/api/v1/admin/remy/skills")
    assert resp.status_code == 200, resp.text
    assert not resp.json()


def test_list_org_skills_db_error_returns_503(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_remy.set_rls_org", new_callable=AsyncMock, side_effect=SQLAlchemyError()):
        resp = client.get("/api/v1/admin/remy/skills")
    assert resp.status_code == 503


def test_create_org_skill_returns_201(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/admin/remy/skills",
        json={"name": "New Skill", "body": "Skill body", "triggers": ["x"], "active": True},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "New Skill"
    assert body["active"] is True


def test_create_org_skill_db_error_returns_503(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.admin_remy.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.admin_remy.RemySkill", side_effect=SQLAlchemyError("insert failed")),
    ):
        resp = client.post("/api/v1/admin/remy/skills", json={"name": "X", "body": "b"})
    assert resp.status_code == 503


def test_update_org_skill_applies_partial_fields(client: TestClient) -> None:
    skill = _make_skill()
    client.mock_session.get = AsyncMock(return_value=skill)  # type: ignore[attr-defined]
    resp = client.put(
        f"/api/v1/admin/remy/skills/{skill.id}",
        json={"name": "Renamed Skill", "active": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Renamed Skill"
    assert body["active"] is False
    assert body["body"] == "Groom the ticket body"


def test_update_org_skill_missing_returns_404(client: TestClient) -> None:
    client.mock_session.get = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    resp = client.put(f"/api/v1/admin/remy/skills/{uuid.uuid4()}", json={"name": "X"})
    assert resp.status_code == 404


def test_update_org_user_level_skill_returns_404(client: TestClient) -> None:
    user_skill = _make_skill(account_id=_USER_ID)
    client.mock_session.get = AsyncMock(return_value=user_skill)  # type: ignore[attr-defined]
    resp = client.put(f"/api/v1/admin/remy/skills/{user_skill.id}", json={"name": "X"})
    assert resp.status_code == 404


def test_delete_org_skill_returns_204(client: TestClient) -> None:
    skill = _make_skill()
    client.mock_session.get = AsyncMock(return_value=skill)  # type: ignore[attr-defined]
    resp = client.delete(f"/api/v1/admin/remy/skills/{skill.id}")
    assert resp.status_code == 204


def test_delete_org_skill_missing_returns_404(client: TestClient) -> None:
    client.mock_session.get = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    resp = client.delete(f"/api/v1/admin/remy/skills/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Context sources
# ---------------------------------------------------------------------------


def _patch_context_source_service(org_defaults: dict[str, str]) -> Any:
    service = MagicMock()
    service.get_org_defaults = AsyncMock(return_value=org_defaults)
    service.set_org_default = AsyncMock()
    return patch("modulo.core.remy.context_source_service.RemyContextSourceService", return_value=service)


def test_get_org_context_sources_merges_builtin_and_org(client: TestClient) -> None:
    with _patch_context_source_service({"manual_context": "off"}):
        resp = client.get("/api/v1/admin/remy/context-sources")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_overrides"] == {"manual_context": "off"}
    assert "builtin_defaults" in body
    assert "effective" in body


def test_set_org_context_source_returns_defaults(client: TestClient) -> None:
    with _patch_context_source_service({"page_context": "tool"}):
        resp = client.put(
            "/api/v1/admin/remy/context-sources/page_context",
            json={"source_mode": "tool"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"page_context": "tool"}


def test_set_org_context_source_invalid_mode_rejected(client: TestClient) -> None:
    resp = client.put(
        "/api/v1/admin/remy/context-sources/page_context",
        json={"source_mode": "sometimes"},
    )
    assert resp.status_code == 422


def test_reset_org_context_sources_deletes_rows(client: TestClient) -> None:
    row = MagicMock()
    result = MagicMock()
    result.scalars.return_value = iter([row])
    _queue_executes(client.mock_session, result)  # type: ignore[attr-defined]
    resp = client.delete("/api/v1/admin/remy/context-sources")
    assert resp.status_code == 200, resp.text
    assert not resp.json()
    client.mock_session.delete.assert_awaited()  # type: ignore[attr-defined]


def test_reset_org_context_sources_db_error_returns_503(client: TestClient) -> None:
    _failing_executes(client.mock_session, SQLAlchemyError("boom"))  # type: ignore[attr-defined]
    resp = client.delete("/api/v1/admin/remy/context-sources")
    assert resp.status_code == 503
