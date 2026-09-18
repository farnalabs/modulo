"""Unit tests for the Remy session-create endpoint (POST /sessions).

Covers provider/model resolution, in particular the opencode default
fallback which must only apply when no model was resolved.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.remy.config_service import RemyConfig
from modulo.settings import Settings, get_settings

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )


def _make_chat_session(created: dict[str, Any]) -> Any:
    """Return a ChatSession side_effect that records constructor kwargs."""

    def _ctor(**kwargs: Any) -> MagicMock:
        created.update(kwargs)
        inst = MagicMock()
        inst.id = uuid.uuid4()
        inst.account_id = USER_ID
        inst.name = kwargs.get("name")
        inst.session_number = kwargs.get("session_number", 1)
        inst.provider = kwargs.get("provider")
        inst.model = kwargs.get("model")
        inst.context_window_tokens = kwargs.get("context_window_tokens")
        inst.system_prompt_hash = kwargs.get("system_prompt_hash")
        inst.created_at = datetime.now(UTC)
        inst.updated_at = datetime.now(UTC)
        return inst

    return _ctor


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="test-user",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="admin",
    )

    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)
    mock_session.add = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalar = MagicMock(return_value=None)
    scalar_result.scalar_one_or_none = MagicMock(return_value=None)
    scalar_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    mock_session.execute = AsyncMock(return_value=scalar_result)

    async def _override_get_db_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = _override_get_db_session

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


class TestCreateSession:
    """POST /sessions — provider/model resolution."""

    def test_explicit_opencode_model_is_preserved(self, client):
        created: dict[str, Any] = {}

        with (
            patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.remy.ChatSession", side_effect=_make_chat_session(created)),
        ):
            resp = client.post(
                "/api/v1/remy/sessions",
                json={
                    "provider": "opencode",
                    "model": "deepseek-reasoner",
                    "context_window_tokens": 200000,
                    "name": "Explicit",
                },
            )

        assert resp.status_code == 201
        assert created["provider"] == "opencode"
        assert created["model"] == "deepseek-reasoner"
        assert resp.json()["model"] == "deepseek-reasoner"

    def test_opencode_default_only_applies_when_no_model_resolved(self, client):
        created: dict[str, Any] = {}

        with (
            patch("modulo.api.routes.remy.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.remy.RemyConfigService") as mock_svc,
            patch("modulo.api.routes.remy.ChatSession", side_effect=_make_chat_session(created)),
        ):
            mock_svc.return_value.get_config = AsyncMock(
                return_value=RemyConfig(default_provider="opencode", default_model="")
            )
            resp = client.post(
                "/api/v1/remy/sessions",
                json={"provider": "opencode", "context_window_tokens": 200000},
            )

        assert resp.status_code == 201
        assert created["provider"] == "opencode"
        assert created["model"] == "deepseek-v4-flash"
