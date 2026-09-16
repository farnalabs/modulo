"""Tests for response / secret hygiene fixes (FAR-896, GitHub #94 #95 #128 #130 #131).

Verifies that:
- MCP create_agent does not leak raw exception text (#94)
- MCP get_org_config masks nested keys and secret-pattern values (#95)
- admin_feature_flags does not embed flag_name in 500 messages (#128)
- health check details do not expose raw exception strings (#130)
- admin_email test-send raises 500 on decrypt failure instead of using ciphertext (#131)
"""

import base64
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.mcp_server import (
    _is_sensitive_key,
    _value_looks_sensitive,
    create_agent,
    get_org_config,
)
from modulo.api.routes.health import (
    _check_checkpointer,
    _check_database,
    _check_migrations,
    _check_redis,
)
from modulo.settings import Settings, get_settings
from tests.unit.mcp.helpers import AuthContext, make_session_context

# ---------------------------------------------------------------------------
# #94 — MCP create_agent does not leak raw exception text
# ---------------------------------------------------------------------------


class TestCreateAgentNoLeak(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch(
        "modulo.db.crud.agent.create_agent",
        side_effect=RuntimeError("db host 10.0.0.54 timeout"),
    )
    async def test_error_detail_is_static_no_exception_text(
        self,
        mock_create_agent: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value = make_session_context(AsyncMock())

        result = await create_agent(
            name="test-agent",
            prompt_template="You are a test agent.",
        )

        assert result["error"] == "internal_error"
        assert result["detail"] == "Failed to create agent"
        # The raw exception text must NOT appear in the response
        assert "10.0.0.54" not in result["detail"]
        assert "timeout" not in result["detail"]
        assert "db host" not in result["detail"]


# ---------------------------------------------------------------------------
# #95 — MCP get_org_config masks nested keys and secret-pattern values
# ---------------------------------------------------------------------------


def _make_config(*, key: str, value: object) -> MagicMock:
    cfg = MagicMock()
    cfg.key = key
    cfg.value = value
    return cfg


class TestIsSensitiveKey:
    def test_flat_sensitive_key(self) -> None:
        assert _is_sensitive_key("fernet_key") is True

    def test_nested_key_with_sensitive_segment(self) -> None:
        assert _is_sensitive_key("remy_config:org123:api_key") is True

    def test_nested_key_with_secret_in_segment(self) -> None:
        assert _is_sensitive_key("a:b:secret_key") is True

    def test_non_sensitive_key_not_masked(self) -> None:
        assert _is_sensitive_key("APP_NAME") is False

    def test_non_sensitive_nested_key(self) -> None:
        assert _is_sensitive_key("remy_config:org123:enabled") is False

    def test_case_insensitive(self) -> None:
        assert _is_sensitive_key("FERNET_KEY") is True
        assert _is_sensitive_key("A:B:API_KEY") is True


class TestValueLooksSensitive:
    def test_github_pat_masked(self) -> None:
        val = "ghp_abc123def456ghi789jkl012mno345pqr678stu"
        assert _value_looks_sensitive(val) is True

    def test_aws_key_masked(self) -> None:
        assert _value_looks_sensitive("AKIAIOSFODNN7EXAMPLE") is True

    def test_openai_key_masked(self) -> None:
        val = "sk-proj1234567890abcdefghijklmnop"
        assert _value_looks_sensitive(val) is True

    def test_normal_value_not_masked(self) -> None:
        assert _value_looks_sensitive("team") is False

    def test_ordinary_string_not_masked(self) -> None:
        assert _value_looks_sensitive("modulo-production") is False


class TestGetOrgConfigMasking(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.system_config.list_config")
    async def test_nested_sensitive_key_filtered_out(
        self,
        mock_list_config: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_list_config.return_value = [
            _make_config(key="remy_config:org123:api_key", value="secret-value"),
            _make_config(key="remy_config:org123:enabled", value="true"),
        ]
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_org_config(section="remy")

        assert result["count"] == 1
        assert "remy_config:org123:api_key" not in result["results"]
        assert "remy_config:org123:enabled" in result["results"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.system_config.list_config")
    async def test_secret_pattern_value_masked_in_table(
        self,
        mock_list_config: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_list_config.return_value = [
            _make_config(
                key="webhook_url",
                value="https://hooks.example.com/ghp_abc123def456ghi789jkl012mno345pqr678stu",
            ),
            _make_config(key="app_name", value="modulo"),
        ]
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_org_config()

        assert result["count"] == 2
        # The PAT in the webhook URL should be masked
        assert "ghp_" not in result["results"]
        # The normal value should still be visible
        assert "| app_name | modulo |" in result["results"]

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    @patch("modulo.db.crud.system_config.list_config")
    async def test_non_sensitive_key_not_masked(
        self,
        mock_list_config: AsyncMock,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_list_config.return_value = [
            _make_config(key="APP_NAME", value="modulo"),
            _make_config(key="LOG_LEVEL", value="info"),
        ]
        mock_session.return_value = make_session_context(AsyncMock())

        result = await get_org_config()

        assert result["count"] == 2
        assert "| APP_NAME | modulo |" in result["results"]
        assert "| LOG_LEVEL | info |" in result["results"]


# ---------------------------------------------------------------------------
# #128 — admin_feature_flags does not embed flag_name in 500 messages
# ---------------------------------------------------------------------------


class TestFeatureFlagNoNameLeak:
    def _make_settings(self) -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_admin_password="test",
            redis_url="redis://localhost:6379/0",
        )

    def test_get_flag_500_does_not_contain_flag_name(self) -> None:
        from fastapi.testclient import TestClient

        from modulo.api.main import app
        from modulo.auth.dependencies import get_current_user
        from modulo.auth.jwt import AuthenticatedPrincipal

        app.dependency_overrides.clear()
        app.dependency_overrides[get_settings] = self._make_settings
        principal = AuthenticatedPrincipal(
            username="test",
            organisation_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            org_role="admin",
            is_system_admin=True,
        )
        app.dependency_overrides[get_current_user] = lambda: principal

        # Simulate an unexpected exception during flag resolution
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=RuntimeError("boom"),
        ):
            client = TestClient(app)
            resp = client.get("/api/v1/admin/feature-flags/test-flag")

        # The flag_name "test-flag" must NOT appear in the response body
        body = resp.json()
        assert resp.status_code == 500
        assert "test-flag" not in json.dumps(body)
        assert body["error"]["message"] == "Failed to get feature flag."
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# #130 — health check details do not expose raw exception strings
# ---------------------------------------------------------------------------


def _make_health_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="test",
        redis_url="redis://localhost:6379/0",
    )


class TestHealthCheckNoExceptionLeak:
    @pytest.mark.asyncio
    async def test_database_check_detail_hides_exception(self) -> None:
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_health_settings()),
            patch("modulo.api.routes.health.get_or_create_engine") as mock_engine,
        ):
            conn_cm = AsyncMock()
            conn_cm.execute.side_effect = ConnectionError("host=10.0.0.54 port=5432 dbname=modulo")
            engine = AsyncMock()
            engine.connect.return_value.__aenter__.return_value = conn_cm
            engine.connect.return_value.__aexit__.return_value = None
            mock_engine.return_value = engine

            result = await _check_database()

        assert result.status == "unavailable"
        assert "10.0.0.54" not in result.detail
        assert "5432" not in result.detail
        assert "modulo" not in result.detail
        assert result.detail == "database unreachable"

    @pytest.mark.asyncio
    async def test_redis_check_detail_hides_exception(self) -> None:
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_health_settings()),
            patch("modulo.api.routes.health.aioredis.Redis") as mock_redis_cls,
        ):
            redis_inst = AsyncMock()
            redis_inst.ping.side_effect = ConnectionError("redis://default:password@10.0.0.99:6379")
            redis_inst.aclose = AsyncMock()
            mock_redis_cls.return_value = redis_inst

            result = await _check_redis()

        assert result.status == "degraded"
        assert "10.0.0.99" not in result.detail
        assert "password" not in result.detail
        assert result.detail == "redis unreachable"

    @pytest.mark.asyncio
    async def test_checkpointer_check_detail_hides_exception(self) -> None:
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_health_settings()),
            patch("modulo.api.routes.health.asyncpg.connect") as mock_connect,
        ):
            conn = AsyncMock()
            conn.fetchrow.side_effect = ConnectionError("host=pg.internal.example.com")
            conn.close = AsyncMock()
            mock_connect.return_value = conn

            result = await _check_checkpointer()

        assert result.status == "degraded"
        assert "pg.internal.example.com" not in result.detail
        assert result.detail == "checkpoint_migrations table not accessible"

    @pytest.mark.asyncio
    async def test_migrations_check_detail_hides_exception(self) -> None:
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_health_settings()),
            patch("modulo.api.routes.health._resolve_alembic_ini") as mock_ini,
        ):
            mock_ini.side_effect = ConnectionError("Cannot connect to db.prod.internal:5432")

            result = await _check_migrations()

        assert result.status == "degraded"
        assert "db.prod.internal" not in result.detail
        assert "5432" not in result.detail
        assert result.detail == "migration check failed"

    @pytest.mark.asyncio
    async def test_database_check_preserves_unavailable_semantics(self) -> None:
        """Database failures must stay 'unavailable' (gates bluegreen)."""
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_health_settings()),
            patch("modulo.api.routes.health.get_or_create_engine") as mock_engine,
        ):
            conn_cm = AsyncMock()
            conn_cm.execute.side_effect = OSError("Connection refused")
            engine = AsyncMock()
            engine.connect.return_value.__aenter__.return_value = conn_cm
            engine.connect.return_value.__aexit__.return_value = None
            mock_engine.return_value = engine

            result = await _check_database()

        assert result.status == "unavailable"

    @pytest.mark.asyncio
    async def test_redis_check_preserves_degraded_semantics(self) -> None:
        """Redis failures must stay 'degraded' (non-gating)."""
        with (
            patch("modulo.api.routes.health.get_settings", return_value=_make_health_settings()),
            patch("modulo.api.routes.health.aioredis.Redis") as mock_redis_cls,
        ):
            redis_inst = AsyncMock()
            redis_inst.ping.side_effect = OSError("Connection refused")
            redis_inst.aclose = AsyncMock()
            mock_redis_cls.return_value = redis_inst

            result = await _check_redis()

        assert result.status == "degraded"


# ---------------------------------------------------------------------------
# #131 — admin_email test-send raises 500 on decrypt failure
# ---------------------------------------------------------------------------


class TestAdminEmailDecryptFailure:
    def _make_settings(self) -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key=base64.urlsafe_b64encode(b"a" * 32).decode(),
            modulo_admin_password="test",
            redis_url="redis://localhost:6379/0",
        )

    @pytest.mark.asyncio
    async def test_decrypt_failure_returns_500_not_ciphertext(self) -> None:
        from modulo.api.routes import admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=uuid.uuid4(),
            name="Test",
            slug="test",
            settings_json={
                "email": {
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 587,
                    "smtp_password": "ciphertext-goes-here",
                }
            },
        )
        original_get = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        try:
            from fastapi.testclient import TestClient

            from modulo.api.dependencies import get_plan_context
            from modulo.api.main import app
            from modulo.auth.dependencies import get_current_user
            from modulo.auth.jwt import AuthenticatedPrincipal

            principal = AuthenticatedPrincipal(
                username="admin@test",
                organisation_id=org.id,
                account_id=uuid.uuid4(),
                org_role="admin",
                is_system_admin=True,
            )
            mock_plan = MagicMock()
            mock_plan.feature_enabled.return_value = True

            app.dependency_overrides.clear()
            app.dependency_overrides[get_plan_context] = lambda: mock_plan
            app.dependency_overrides[get_current_user] = lambda: principal
            app.dependency_overrides[get_settings] = self._make_settings

            # Mock decode_stored_secret_scoped to raise (simulating decrypt failure)
            original_decode = admin_email.decode_stored_secret_scoped
            admin_email.decode_stored_secret_scoped = AsyncMock(side_effect=RuntimeError("Fernet: invalid token"))
            try:
                client = TestClient(app)
                resp = client.post(
                    f"/api/v1/admin/org/{org.id}/email-settings/test",
                    json={"to": "admin@example.com"},
                )
                assert resp.status_code == 500
                body = resp.json()
                # Must NOT contain the ciphertext
                assert "ciphertext-goes-here" not in json.dumps(body)
                # Must contain the generic message
                assert "SMTP password decryption failed" in body["detail"]
            finally:
                admin_email.decode_stored_secret_scoped = original_decode
        finally:
            admin_email.get_organisation = original_get
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_decrypt_failure_does_not_attempt_send(self) -> None:
        from modulo.api.routes import admin_email
        from modulo.db.models.organisation import Organisation

        org = Organisation(
            id=uuid.uuid4(),
            name="Test",
            slug="test",
            settings_json={
                "email": {
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 587,
                    "smtp_password": "ciphertext-goes-here",
                }
            },
        )
        original_get = admin_email.get_organisation
        admin_email.get_organisation = AsyncMock(return_value=org)
        try:
            from fastapi.testclient import TestClient

            from modulo.api.dependencies import get_plan_context
            from modulo.api.main import app
            from modulo.auth.dependencies import get_current_user
            from modulo.auth.jwt import AuthenticatedPrincipal

            principal = AuthenticatedPrincipal(
                username="admin@test",
                organisation_id=org.id,
                account_id=uuid.uuid4(),
                org_role="admin",
                is_system_admin=True,
            )
            mock_plan = MagicMock()
            mock_plan.feature_enabled.return_value = True

            app.dependency_overrides.clear()
            app.dependency_overrides[get_plan_context] = lambda: mock_plan
            app.dependency_overrides[get_current_user] = lambda: principal
            app.dependency_overrides[get_settings] = self._make_settings

            original_decode = admin_email.decode_stored_secret_scoped
            original_send = admin_email.send_email
            admin_email.decode_stored_secret_scoped = AsyncMock(side_effect=RuntimeError("decrypt failed"))
            admin_email.send_email = MagicMock(return_value=True)
            try:
                client = TestClient(app)
                resp = client.post(
                    f"/api/v1/admin/org/{org.id}/email-settings/test",
                    json={"to": "admin@example.com"},
                )
                # Should 500 BEFORE send_email is called
                assert resp.status_code == 500
                admin_email.send_email.assert_not_called()
            finally:
                admin_email.decode_stored_secret_scoped = original_decode
                admin_email.send_email = original_send
        finally:
            admin_email.get_organisation = original_get
            app.dependency_overrides.clear()
