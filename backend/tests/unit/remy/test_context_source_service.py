"""Unit tests for RemyContextSourceService — merging built-in defaults, org defaults, and user overrides."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.remy.context_source_service import RemyContextSourceService
from modulo.db.models.remy_context_source import RemyContextSource


def _mock_source_row(
    source_key: str, source_mode: str, org_id: uuid.UUID, user_id: uuid.UUID | None = None
) -> MagicMock:
    row = MagicMock(spec=RemyContextSource)
    row.id = uuid.uuid4()
    row.organisation_id = org_id
    row.user_id = user_id
    row.source_key = source_key
    row.source_mode = source_mode
    return row


def _mock_scalars_result(rows: list[MagicMock]) -> MagicMock:
    """Create a mock that supports both .all() and list() (iteration)."""
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=rows)
    scalars.__iter__ = MagicMock(return_value=iter(rows))
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    return result


class TestRemyContextSourceServiceGetEffectiveConfig:
    """Tests for merging built-in → org defaults → user overrides."""

    @pytest.fixture
    def service(self, mock_session: AsyncMock) -> RemyContextSourceService:
        return RemyContextSourceService(mock_session)

    async def test_uses_builtins_when_no_org_or_user_overrides(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        config = await service.get_effective_config(org_id, user_id)
        assert config.context_sources["page_context"] == "always_on"
        assert config.context_sources["product_primer"] == "always_on"
        assert config.context_sources["product_docs"] == "tool"
        assert config.context_sources["integration_status"] == "tool"
        assert config.context_sources["org_config"] == "tool"
        assert config.context_sources["feature_overview"] == "tool"
        assert config.context_sources["user_profile"] == "always_on"

    async def test_org_default_overrides_builtin(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        org_row = _mock_source_row("product_docs", "off", org_id)
        org_result = _mock_scalars_result([org_row])
        user_result = _mock_scalars_result([])

        mock_session.execute = AsyncMock(side_effect=[org_result, user_result])

        config = await service.get_effective_config(org_id, user_id)
        assert config.context_sources["product_docs"] == "off"

    async def test_user_override_takes_precedence_over_org_default(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        org_row = _mock_source_row("product_docs", "off", org_id)
        user_row = _mock_source_row("product_docs", "always_on", org_id, user_id)

        org_result = _mock_scalars_result([org_row])
        user_result = _mock_scalars_result([user_row])

        mock_session.execute = AsyncMock(side_effect=[org_result, user_result])

        config = await service.get_effective_config(org_id, user_id)
        assert config.context_sources["product_docs"] == "always_on"

    async def test_get_effective_config_returns_remy_config(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        config = await service.get_effective_config(org_id, user_id)
        from modulo.core.remy.config_service import RemyConfig

        assert isinstance(config, RemyConfig)


class TestRemyContextSourceServiceSetUserOverride:
    """Tests for setting user-level overrides."""

    @pytest.fixture
    def service(self, mock_session: AsyncMock) -> RemyContextSourceService:
        return RemyContextSourceService(mock_session)

    async def test_set_user_override_calls_execute(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        await service.set_user_override(org_id, user_id, "product_docs", "always_on")

        mock_session.execute.assert_called_once()
        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "FOR UPDATE" in compiled
        assert f"remy_context_sources.organisation_id = '{org_id.hex}'" in compiled
        assert "remy_context_sources.source_key = 'product_docs'" in compiled
        assert f"remy_context_sources.user_id = '{user_id.hex}'" in compiled

    async def test_set_user_override_inserts_new_row_when_missing(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        await service.set_user_override(org_id, user_id, "product_docs", "always_on")

        mock_session.add.assert_called_once()
        added = mock_session.add.call_args[0][0]
        assert isinstance(added, RemyContextSource)
        assert added.organisation_id == org_id
        assert added.user_id == user_id
        assert added.source_key == "product_docs"
        assert added.source_mode == "always_on"
        mock_session.flush.assert_awaited_once()

    async def test_set_user_override_updates_existing_row(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        existing = _mock_source_row("product_docs", "off", org_id, user_id)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=existing)
        mock_session.execute = AsyncMock(return_value=mock_result)

        await service.set_user_override(org_id, user_id, "product_docs", "always_on")

        mock_session.add.assert_not_called()
        assert existing.source_mode == "always_on"
        mock_session.flush.assert_awaited_once()

    async def test_set_user_override_rejects_empty_key(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        with pytest.raises(ValueError, match="source_key must not be empty"):
            await service.set_user_override(org_id, user_id, "", "always_on")
        mock_session.execute.assert_not_called()

    async def test_set_user_override_rejects_invalid_mode(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        with pytest.raises(ValueError, match="Invalid source_mode 'sometimes'"):
            await service.set_user_override(org_id, user_id, "product_docs", "sometimes")
        mock_session.execute.assert_not_called()


class TestRemyContextSourceServiceSetOrgDefault:
    """Tests for setting org-level defaults."""

    @pytest.fixture
    def service(self, mock_session: AsyncMock) -> RemyContextSourceService:
        return RemyContextSourceService(mock_session)

    async def test_set_org_default_calls_execute(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        await service.set_org_default(org_id, "product_docs", "off")

        mock_session.execute.assert_called_once()
        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "FOR UPDATE" in compiled
        assert f"remy_context_sources.organisation_id = '{org_id.hex}'" in compiled
        assert "remy_context_sources.source_key = 'product_docs'" in compiled
        assert "remy_context_sources.user_id IS NULL" in compiled

    async def test_set_org_default_scopes_to_null_user(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        await service.set_org_default(org_id, "product_docs", "off")

        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert f"remy_context_sources.organisation_id = '{org_id.hex}'" in compiled
        assert "remy_context_sources.source_key = 'product_docs'" in compiled
        assert "remy_context_sources.user_id IS NULL" in compiled
        assert "FOR UPDATE" in compiled

    async def test_set_org_default_rejects_invalid_mode(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        with pytest.raises(ValueError, match="Invalid source_mode"):
            await service.set_org_default(org_id, "product_docs", "bogus")
        mock_session.execute.assert_not_called()


class TestRemyContextSourceServiceQueries:
    """Tests for get_org_defaults, get_user_overrides and build_effective_items."""

    @pytest.fixture
    def service(self, mock_session: AsyncMock) -> RemyContextSourceService:
        return RemyContextSourceService(mock_session)

    async def test_get_org_defaults_returns_mode_map(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        rows = [
            _mock_source_row("product_docs", "off", org_id),
            _mock_source_row("feature_overview", "always_on", org_id),
        ]
        scalars = MagicMock()
        scalars.__iter__ = MagicMock(return_value=iter(rows))
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=scalars)
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await service.get_org_defaults(org_id)
        assert result == {"product_docs": "off", "feature_overview": "always_on"}

    async def test_get_user_overrides_scopes_by_user(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        await service.get_user_overrides(org_id, user_id)

        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert f"remy_context_sources.organisation_id = '{org_id.hex}'" in compiled
        assert f"remy_context_sources.user_id = '{user_id.hex}'" in compiled

    def test_build_effective_items_marks_overrides(self) -> None:
        service = RemyContextSourceService.__new__(RemyContextSourceService)
        effective = {"page_context": "always_on", "product_docs": "off"}
        user_overrides = {"product_docs": "off"}

        items = service.build_effective_items(effective, user_overrides)
        assert len(items) == 2
        by_key = {item.key: item for item in items}
        assert by_key["page_context"].name == "Page Context"
        assert by_key["page_context"].is_overridden is False
        assert by_key["page_context"].source_mode == "always_on"
        assert by_key["product_docs"].is_overridden is True
        assert by_key["product_docs"].description

    def test_build_effective_items_handles_unknown_keys(self) -> None:
        service = RemyContextSourceService.__new__(RemyContextSourceService)
        items = service.build_effective_items({"custom_source": "tool"}, {})
        assert len(items) == 1
        assert items[0].name == "Custom Source"
        assert not items[0].description


class TestRemyContextSourceServiceResetUserOverrides:
    """Tests for resetting all user-level overrides."""

    @pytest.fixture
    def service(self, mock_session: AsyncMock) -> RemyContextSourceService:
        return RemyContextSourceService(mock_session)

    async def test_reset_user_overrides_calls_bulk_delete(
        self,
        service: RemyContextSourceService,
        mock_session: AsyncMock,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        await service.reset_user_overrides(org_id, user_id)

        mock_session.execute.assert_called_once()
        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "DELETE FROM remy_context_sources" in compiled
        assert f"remy_context_sources.organisation_id = '{org_id.hex}'" in compiled
        assert f"remy_context_sources.user_id = '{user_id.hex}'" in compiled
        mock_session.flush.assert_awaited_once()
