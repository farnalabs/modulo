"""Unit tests for RemyConfigService — config CRUD and access control."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.remy.config_service import RemyConfig, RemyConfigService


class TestRemyConfigDefaults:
    """Tests for RemyConfig Pydantic model defaults."""

    def test_default_values(self) -> None:
        config = RemyConfig()
        assert config.system_prompt == ""
        assert config.additional_guidance == ""
        assert config.access_list == {"user_ids": [], "team_ids": [], "org_roles": ["admin"]}
        assert config.default_provider == "anthropic"
        assert config.default_model == "claude-sonnet-4-20250514"
        assert config.default_context_window == 200000
        assert config.allowed_providers == ["anthropic", "openai", "google-gemini", "deepseek", "groq"]
        assert config.allowed_models == []


class TestRemyConfigServiceGetConfig:
    """Tests for RemyConfigService.get_config."""

    @pytest.fixture
    def service(self, mock_session: AsyncMock) -> RemyConfigService:
        return RemyConfigService(mock_session)

    async def test_returns_defaults_when_no_config_stored(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)

        config = await service.get_config(org_id)
        assert isinstance(config, RemyConfig)
        assert config.system_prompt == ""
        assert config.default_provider == "anthropic"

    async def test_returns_stored_config(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        stored_value = {
            "system_prompt": "You are helpful.",
            "default_provider": "openai",
            "default_model": "gpt-4o",
            "default_context_window": 100000,
        }
        entry = MagicMock()
        entry.value = stored_value
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=entry)
        mock_session.execute = AsyncMock(return_value=mock_result)

        config = await service.get_config(org_id)
        assert config.system_prompt == "You are helpful."
        assert config.default_provider == "openai"
        assert config.default_model == "gpt-4o"
        assert config.default_context_window == 100000

    async def test_returns_defaults_when_stored_value_is_not_dict(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        entry = MagicMock()
        entry.value = "not a dict"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=entry)
        mock_session.execute = AsyncMock(return_value=mock_result)

        config = await service.get_config(org_id)
        assert isinstance(config, RemyConfig)
        assert config.system_prompt == ""

    async def test_returns_partial_config_with_defaults(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        stored_value = {
            "system_prompt": "Be concise.",
        }
        entry = MagicMock()
        entry.value = stored_value
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=entry)
        mock_session.execute = AsyncMock(return_value=mock_result)

        config = await service.get_config(org_id)
        assert config.system_prompt == "Be concise."
        assert config.default_provider == "anthropic"  # default preserved
        assert config.default_context_window == 200000  # default preserved


class TestRemyConfigServiceUpdateConfig:
    """Tests for RemyConfigService.update_config."""

    @pytest.fixture
    def service(self, mock_session: AsyncMock) -> RemyConfigService:
        return RemyConfigService(mock_session)

    async def test_update_config_persists(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        with patch("modulo.core.remy.config_service.set_config", new_callable=AsyncMock) as mock_set:
            config = RemyConfig(
                system_prompt="New system prompt",
                default_provider="deepseek",
            )
            await service.update_config(org_id, config)
            mock_set.assert_awaited_once()
            _args, kwargs = mock_set.call_args
            assert kwargs["key"] == f"remy_config:{org_id}"
            assert kwargs["value"]["system_prompt"] == "New system prompt"
            assert kwargs["value"]["default_provider"] == "deepseek"


class TestRemyConfigServiceCheckAccess:
    """Tests for RemyConfigService.check_access."""

    @pytest.fixture
    def service(self, mock_session: AsyncMock) -> RemyConfigService:
        return RemyConfigService(mock_session)

    async def test_check_access_matches_user_id(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        stored_value = {
            "access_list": {
                "user_ids": [str(user_id)],
                "team_ids": [],
                "org_roles": [],
            },
        }
        entry = MagicMock()
        entry.value = stored_value
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=entry)
        mock_session.execute = AsyncMock(return_value=mock_result)

        granted = await service.check_access(org_id, user_id, "viewer", [])
        assert granted is True

    async def test_check_access_matches_org_role(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        stored_value = {
            "access_list": {
                "user_ids": [],
                "team_ids": [],
                "org_roles": ["admin"],
            },
        }
        entry = MagicMock()
        entry.value = stored_value
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=entry)
        mock_session.execute = AsyncMock(return_value=mock_result)

        granted = await service.check_access(org_id, user_id, "admin", [])
        assert granted is True

    async def test_check_access_matches_team_id(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        team_id = uuid.uuid4()
        stored_value = {
            "access_list": {
                "user_ids": [],
                "team_ids": [str(team_id)],
                "org_roles": [],
            },
        }
        entry = MagicMock()
        entry.value = stored_value
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=entry)
        mock_session.execute = AsyncMock(return_value=mock_result)

        granted = await service.check_access(org_id, user_id, "viewer", [team_id])
        assert granted is True

    async def test_check_access_returns_false_when_no_match(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        stored_value = {
            "access_list": {
                "user_ids": [],
                "team_ids": [],
                "org_roles": [],
            },
        }
        entry = MagicMock()
        entry.value = stored_value
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=entry)
        mock_session.execute = AsyncMock(return_value=mock_result)

        granted = await service.check_access(org_id, user_id, "viewer", [])
        assert granted is False

    async def test_check_access_with_team_ids_as_uuid_objects(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        team_id = uuid.uuid4()
        stored_value = {
            "access_list": {
                "user_ids": [],
                "team_ids": [team_id],  # stored as UUID, not string
                "org_roles": [],
            },
        }
        entry = MagicMock()
        entry.value = stored_value
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=entry)
        mock_session.execute = AsyncMock(return_value=mock_result)

        granted = await service.check_access(org_id, user_id, "viewer", [team_id])
        assert granted is True

    async def test_check_access_with_user_id_as_uuid_object(
        self, service: RemyConfigService, mock_session: AsyncMock, org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        stored_value = {
            "access_list": {
                "user_ids": [user_id],  # UUID, not string
                "team_ids": [],
                "org_roles": [],
            },
        }
        entry = MagicMock()
        entry.value = stored_value
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=entry)
        mock_session.execute = AsyncMock(return_value=mock_result)

        granted = await service.check_access(org_id, user_id, "viewer", [])
        assert granted is True
