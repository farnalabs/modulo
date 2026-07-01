"""Tests for SystemConfig model, CRUD, and get_effective_setting."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.system_config import delete_config, get_config, list_config, set_config
from modulo.db.models.system_config import SystemConfig
from modulo.db.settings_resolver import get_effective_setting


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    execute_result.scalars.return_value.all.return_value = []
    session.execute.return_value = execute_result
    return session


class TestSystemConfigModel:
    def test_table_name(self) -> None:
        assert SystemConfig.__tablename__ == "system_config"

    def test_columns_exist(self) -> None:
        from modulo.db.models import Base

        cols = Base.metadata.tables["system_config"].c
        assert "id" in cols
        assert "key" in cols
        assert "value" in cols
        assert "updated_at" in cols
        assert "updated_by" in cols

    def test_key_unique_constraint(self) -> None:
        from sqlalchemy import UniqueConstraint

        table = SystemConfig.__table__
        has_unique = any(
            isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == ["key"]
            for c in table.constraints
        )
        assert has_unique


class TestSystemConfigCRUD:
    async def test_set_and_get_config(self, mock_session: AsyncMock) -> None:
        key = "test_key"
        value = {"nested": "data", "number": 42}

        existing = None
        mock_session.execute.return_value.scalar_one_or_none.return_value = existing

        entity = await set_config(mock_session, key, value)
        assert entity.key == key
        assert entity.value == value
        assert entity.updated_by is None
        mock_session.add.assert_called_once_with(entity)
        mock_session.flush.assert_awaited()

    async def test_get_config_returns_none_for_missing(self, mock_session: AsyncMock) -> None:
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        result = await get_config(mock_session, "nonexistent")
        assert result is None

    async def test_set_config_updates_existing(self, mock_session: AsyncMock) -> None:
        key = "existing_key"
        existing = SystemConfig(key=key, value={"old": "value"})
        mock_session.execute.return_value.scalar_one_or_none.return_value = existing

        entity = await set_config(mock_session, key, {"new": "value"})
        assert entity.key == key
        assert entity.value == {"new": "value"}
        mock_session.add.assert_not_called()

    async def test_set_config_with_updated_by(self, mock_session: AsyncMock) -> None:
        account_id = uuid.uuid4()
        mock_session.execute.return_value.scalar_one_or_none.return_value = None

        entity = await set_config(mock_session, "key", "val", updated_by=account_id)
        assert entity.updated_by == account_id

    async def test_list_config(self, mock_session: AsyncMock) -> None:
        entries = [
            SystemConfig(key="a", value=1),
            SystemConfig(key="b", value=2),
        ]
        mock_session.execute.return_value.scalars.return_value.all.return_value = entries

        result = await list_config(mock_session)
        assert len(result) == 2
        assert result[0].key == "a"

    async def test_delete_config_existing(self, mock_session: AsyncMock) -> None:
        existing = SystemConfig(key="to_delete", value="val")
        mock_session.execute.return_value.scalar_one_or_none.return_value = existing

        result = await delete_config(mock_session, "to_delete")
        assert result is True
        mock_session.delete.assert_called_once_with(existing)

    async def test_delete_config_nonexistent(self, mock_session: AsyncMock) -> None:
        mock_session.execute.return_value.scalar_one_or_none.return_value = None

        result = await delete_config(mock_session, "missing")
        assert result is False
        mock_session.delete.assert_not_called()


class TestGetEffectiveSetting:
    async def test_returns_org_setting_first(self) -> None:
        session = AsyncMock()
        org = MagicMock()
        org.settings_json = {"theme": "dark", "max_items": 50}


        get_organisation_result = AsyncMock(return_value=org)
        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.get_organisation", get_organisation_result)
            result = await get_effective_setting(session, uuid.uuid4(), "theme")
            assert result == "dark"

    async def test_falls_back_to_system_config(self) -> None:
        session = AsyncMock()
        org = MagicMock()
        org.settings_json = {}

        config_entity = MagicMock()
        config_entity.value = "system_wide"


        get_org = AsyncMock(return_value=org)
        get_cfg = AsyncMock(return_value=config_entity)

        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.get_organisation", get_org)
            m.setattr("modulo.db.settings_resolver.get_config", get_cfg)

            result = await get_effective_setting(session, uuid.uuid4(), "theme")
            assert result == "system_wide"

    async def test_falls_back_to_default(self) -> None:
        session = AsyncMock()
        org = MagicMock()
        org.settings_json = {}

        get_org = AsyncMock(return_value=org)
        get_cfg = AsyncMock(return_value=None)

        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.get_organisation", get_org)
            m.setattr("modulo.db.settings_resolver.get_config", get_cfg)

            result = await get_effective_setting(session, uuid.uuid4(), "theme", default="light")
            assert result == "light"

    async def test_returns_default_when_no_org_id(self) -> None:
        session = AsyncMock()
        config_entity = MagicMock()
        config_entity.value = "from_system"

        get_cfg = AsyncMock(return_value=config_entity)

        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.get_config", get_cfg)

            result = await get_effective_setting(session, None, "theme", default="fallback")
            assert result == "from_system"

    async def test_returns_default_when_no_org_and_no_config(self) -> None:
        session = AsyncMock()
        get_cfg = AsyncMock(return_value=None)

        with pytest.MonkeyPatch().context() as m:
            m.setattr("modulo.db.settings_resolver.get_config", get_cfg)

            result = await get_effective_setting(session, None, "theme", default="fallback")
            assert result == "fallback"
