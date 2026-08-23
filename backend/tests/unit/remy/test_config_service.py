"""Unit tests for RemyConfigService — config CRUD and access control."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from modulo.core.remy.config_service import RemyConfig, RemyConfigService


def _stored_config(mock_session: AsyncMock, value: object) -> None:
    """Point the session's next execute at a stored SystemConfig row with the given value."""
    entry = MagicMock()
    entry.value = value
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=entry)
    mock_session.execute = AsyncMock(return_value=mock_result)


class TestRemyConfigDefaults:
    """Tests for RemyConfig Pydantic model defaults."""

    def test_default_values(self) -> None:
        config = RemyConfig()
        assert not config.system_prompt
        assert not config.additional_guidance
        assert config.access_rules == {"user_ids": [], "team_ids": [], "org_roles": ["admin"]}
        assert config.default_provider == "anthropic"
        assert config.default_model == "claude-sonnet-4-20250514"
        assert config.default_context_window == 200000
        assert config.allowed_providers == ["anthropic", "openai", "gemini", "deepseek", "groq"]
        assert not config.allowed_models

    def test_schema_version_default(self) -> None:
        config = RemyConfig()
        assert config.schema_version == 3

    def test_product_primer_default(self) -> None:
        config = RemyConfig()
        assert not config.product_primer

    def test_context_sources_defaults(self) -> None:
        config = RemyConfig()
        expected = {
            "page_context": "always_on",
            "user_profile": "always_on",
            "product_primer": "always_on",
            "product_docs": "tool",
            "integration_status": "tool",
            "org_config": "tool",
            "feature_overview": "tool",
        }
        assert config.context_sources == expected
        assert config.context_sources["page_context"] == "always_on"
        assert config.context_sources["product_docs"] == "tool"

    def test_default_context_sources_immutable(self) -> None:
        config = RemyConfig()
        config2 = RemyConfig()
        config.context_sources["page_context"] = "off"
        assert config2.context_sources["page_context"] == "always_on"


class TestRemyConfigServiceGetConfig:
    """Tests for RemyConfigService.get_config."""

    @pytest.fixture
    def service(self, mock_session: AsyncMock) -> RemyConfigService:
        return RemyConfigService(mock_session)

    async def test_returns_defaults_when_no_config_stored(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)

        config = await service.get_config(org_id)
        assert isinstance(config, RemyConfig)
        assert not config.system_prompt
        assert config.default_provider == "anthropic"

    async def test_returns_stored_config(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        _stored_config(
            mock_session,
            {
                "system_prompt": "You are helpful.",
                "default_provider": "openai",
                "default_model": "gpt-4o",
                "default_context_window": 100000,
            },
        )

        config = await service.get_config(org_id)
        assert config.system_prompt == "You are helpful."
        assert config.default_provider == "openai"
        assert config.default_model == "gpt-4o"
        assert config.default_context_window == 100000

    async def test_returns_defaults_when_stored_value_is_not_dict(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        _stored_config(mock_session, "not a dict")

        config = await service.get_config(org_id)
        assert isinstance(config, RemyConfig)
        assert not config.system_prompt

    async def test_returns_defaults_when_db_query_raises(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        mock_session.execute = AsyncMock(side_effect=SQLAlchemyError("connection lost"))

        config = await service.get_config(org_id)
        assert isinstance(config, RemyConfig)
        assert not config.system_prompt
        assert config.default_provider == "anthropic"

    async def test_returns_defaults_when_stored_value_is_invalid(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        _stored_config(mock_session, {"default_context_window": "not-an-int"})

        config = await service.get_config(org_id)
        assert isinstance(config, RemyConfig)
        assert config.default_context_window == 200000

    async def test_returns_partial_config_with_defaults(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        _stored_config(mock_session, {"system_prompt": "Be concise."})

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
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        with patch("modulo.core.remy.config_service.update_config", new_callable=AsyncMock) as mock_set:
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
            mock_session.flush.assert_awaited_once()

    async def test_update_config_reraises_on_db_error(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        with (
            patch(
                "modulo.core.remy.config_service.update_config",
                new_callable=AsyncMock,
                side_effect=SQLAlchemyError("write failed"),
            ),
            pytest.raises(SQLAlchemyError),
        ):
            await service.update_config(org_id, RemyConfig(system_prompt="Nope"))


class TestRemyConfigServiceCheckAccess:
    """Tests for RemyConfigService.check_access."""

    @pytest.fixture
    def service(self, mock_session: AsyncMock) -> RemyConfigService:
        return RemyConfigService(mock_session)

    async def test_check_access_matches_user_id(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        _stored_config(
            mock_session,
            {
                "access_rules": {
                    "user_ids": [str(user_id)],
                    "team_ids": [],
                    "org_roles": [],
                },
            },
        )

        granted = await service.check_access(org_id, user_id, "viewer", [])
        assert granted is True

    async def test_check_access_matches_org_role(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        _stored_config(
            mock_session,
            {
                "access_rules": {
                    "user_ids": [],
                    "team_ids": [],
                    "org_roles": ["admin"],
                },
            },
        )

        granted = await service.check_access(org_id, user_id, "admin", [])
        assert granted is True

    async def test_check_access_matches_org_role_case_insensitively(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        _stored_config(
            mock_session,
            {
                "access_rules": {
                    "user_ids": [],
                    "team_ids": [],
                    "org_roles": ["Admin"],
                },
            },
        )

        granted = await service.check_access(org_id, user_id, "admin", [])
        assert granted is True

    async def test_check_access_matches_team_id(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        team_id = uuid.uuid4()
        _stored_config(
            mock_session,
            {
                "access_rules": {
                    "user_ids": [],
                    "team_ids": [str(team_id)],
                    "org_roles": [],
                },
            },
        )

        granted = await service.check_access(org_id, user_id, "viewer", [team_id])
        assert granted is True

    async def test_check_access_returns_false_when_no_match(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        _stored_config(
            mock_session,
            {
                "access_rules": {
                    "user_ids": [],
                    "team_ids": [],
                    "org_roles": [],
                },
            },
        )

        granted = await service.check_access(org_id, user_id, "viewer", [])
        assert granted is False

    async def test_check_access_with_team_ids_as_uuid_objects(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        team_id = uuid.uuid4()
        _stored_config(
            mock_session,
            {
                "access_rules": {
                    "user_ids": [],
                    "team_ids": [team_id],  # stored as UUID, not string
                    "org_roles": [],
                },
            },
        )

        granted = await service.check_access(org_id, user_id, "viewer", [team_id])
        assert granted is True

    async def test_check_access_with_user_id_as_uuid_object(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        _stored_config(
            mock_session,
            {
                "access_rules": {
                    "user_ids": [user_id],  # UUID, not string
                    "team_ids": [],
                    "org_roles": [],
                },
            },
        )

        granted = await service.check_access(org_id, user_id, "viewer", [])
        assert granted is True

    async def test_check_access_skips_invalid_user_ids(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        _stored_config(
            mock_session,
            {
                "access_rules": {
                    "user_ids": ["not-a-uuid", 123],  # both invalid → normalized away
                    "team_ids": [],
                    "org_roles": [],
                },
            },
        )

        granted = await service.check_access(org_id, user_id, "viewer", [])
        assert granted is False

    async def test_check_access_when_access_rules_missing_keys(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        _stored_config(
            mock_session,
            {"access_rules": {"user_ids": [], "team_ids": []}},  # no org_roles key
        )

        granted = await service.check_access(org_id, user_id, "admin", [])
        assert granted is False

    async def test_check_access_denies_when_config_fetch_fails(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        with patch(
            "modulo.core.remy.config_service.get_config",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            granted = await service.check_access(org_id, user_id, "admin", [])
        assert granted is False

    async def test_check_access_reraises_cancelled_error(
        self,
        service: RemyConfigService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        user_id = uuid.uuid4()
        with (
            patch(
                "modulo.core.remy.config_service.get_config",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await service.check_access(org_id, user_id, "admin", [])
