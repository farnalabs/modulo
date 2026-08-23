"""Tests for SystemConfig model, CRUD, and get_effective_setting."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.system_config import (
    delete_config,
    get_config,
    list_config,
    set_config,
    update_config,
)
from modulo.db.models.system_config import SystemConfig
from modulo.db.settings_resolver import get_effective_setting


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.begin_nested = AsyncMock()
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
            isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == ["key"] for c in table.constraints
        )
        assert has_unique


class TestSystemConfigCRUD:
    async def test_set_and_get_config(self, mock_session: AsyncMock) -> None:
        key = "test_key"
        value = {"nested": "data", "number": 42}

        # First write: SELECT FOR UPDATE finds nothing, the ON CONFLICT INSERT
        # is issued, then the stored row is SELECTed back and returned.
        stored = SystemConfig(key=key, value=value, updated_by=None)
        select_none = MagicMock()
        select_none.scalar_one_or_none.return_value = None
        select_stored = MagicMock()
        select_stored.scalar_one.return_value = stored

        calls = {"n": 0}

        def _execute(stmt):
            calls["n"] += 1
            return select_none if calls["n"] == 1 else select_stored

        mock_session.execute = AsyncMock(side_effect=_execute)

        entity = await set_config(mock_session, key, value)
        assert entity is stored
        assert entity.key == key
        assert entity.value == value
        assert entity.updated_by is None
        # The first-write path uses an INSERT … ON CONFLICT construct, not add().
        mock_session.add.assert_not_called()
        mock_session.flush.assert_awaited()

    async def test_get_config_returns_none_for_missing(self, mock_session: AsyncMock) -> None:
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        result = await get_config(mock_session, "nonexistent")
        assert result is None

    async def test_set_config_never_overwrites_existing(self, mock_session: AsyncMock) -> None:
        """``set_config`` is TOFU: a second write adopts the stored value.

        Even after a key is already committed, a subsequent ``set_config`` must
        NOT overwrite it — it hits ``ON CONFLICT DO NOTHING`` and re-SELECTs the
        first writer's stored value (first-write-wins / TOFU). This is what
        makes the concurrent-mint invariant hold deterministically for any
        interleaving, not just the racing case.
        """
        key = "existing_key"
        stored_row = SystemConfig(key=key, value={"first": "writer"})

        insert_result = MagicMock()
        select_stored = MagicMock()
        select_stored.scalar_one.return_value = stored_row

        calls = {"n": 0}

        def _execute(stmt):
            calls["n"] += 1
            # call 1: INSERT … ON CONFLICT DO NOTHING (skipped, key exists);
            # call 2: re-SELECT the stored first-writer value back.
            return insert_result if calls["n"] == 1 else select_stored

        mock_session.execute = AsyncMock(side_effect=_execute)

        entity = await set_config(mock_session, key, {"second": "writer"})
        assert entity is stored_row
        assert entity.value == {"first": "writer"}
        mock_session.add.assert_not_called()

    async def test_update_config_overwrites_existing(self, mock_session: AsyncMock) -> None:
        """``update_config`` is the deliberate-overwrite path (last-writer-wins)."""
        key = "existing_key"
        updated = SystemConfig(key=key, value={"new": "value"})

        insert_result = MagicMock()
        select_stored = MagicMock()
        select_stored.scalar_one.return_value = updated

        calls = {"n": 0}

        def _execute(stmt):
            calls["n"] += 1
            return insert_result if calls["n"] == 1 else select_stored

        mock_session.execute = AsyncMock(side_effect=_execute)

        entity = await update_config(mock_session, key, {"new": "value"})
        assert entity.key == key
        assert entity.value == {"new": "value"}
        mock_session.add.assert_not_called()

    async def test_update_config_uses_atomic_on_conflict(self, mock_session: AsyncMock) -> None:
        """The deliberate-update path applies the mutation in one atomic statement.

        No separate existence ``SELECT`` precedes the write, so a concurrent
        committed update cannot be lost in a stale-read window between two reads.
        The statement is an ``INSERT … ON CONFLICT DO UPDATE``.
        """
        key = "contended_key"

        await update_config(mock_session, key, "v2")
        # The first statement is the atomic INSERT … ON CONFLICT DO UPDATE; the
        # second is the re-SELECT of the stored row.
        insert_stmt = mock_session.execute.call_args_list[0].args[0]
        assert "ON CONFLICT" in str(insert_stmt)
        assert "DO UPDATE" in str(insert_stmt)

    async def test_set_config_with_updated_by(self, mock_session: AsyncMock) -> None:
        account_id = uuid.uuid4()
        stored = SystemConfig(key="key", value="val", updated_by=account_id)
        select_none = MagicMock()
        select_none.scalar_one_or_none.return_value = None
        select_stored = MagicMock()
        select_stored.scalar_one.return_value = stored

        calls = {"n": 0}

        def _execute(stmt):
            calls["n"] += 1
            return select_none if calls["n"] == 1 else select_stored

        mock_session.execute = AsyncMock(side_effect=_execute)

        entity = await set_config(mock_session, "key", "val", updated_by=account_id)
        assert entity.updated_by == account_id

    async def test_set_config_concurrent_first_write_converges_to_winner(self) -> None:
        """Concurrent first-write race: the losing caller adopts the winner's value.

        ``set_config`` issues ``INSERT … ON CONFLICT DO NOTHING`` so the loser's
        INSERT is skipped (no exception), then SELECTs the single stored row back.
        The stored row is the *winner's* value, which the loser must adopt
        unchanged (first-write-wins / TOFU): both concurrent callers observe the
        same value instead of flipping to whichever caller wrote last.
        """
        session = AsyncMock(spec=AsyncSession)
        session.add = MagicMock()

        winner_row = SystemConfig(key="race_key", value="concurrent-winner-value")
        select_none = MagicMock()
        select_none.scalar_one_or_none.return_value = None
        select_stored = MagicMock()
        select_stored.scalar_one.return_value = winner_row
        insert_result = MagicMock()

        execute_calls = {"n": 0}

        def _execute(stmt):
            execute_calls["n"] += 1
            # call 1: INSERT … ON CONFLICT DO NOTHING (skipped — winner won);
            # call 2: SELECT the winner's stored row back.
            if execute_calls["n"] == 1:
                return insert_result
            return select_stored

        session.execute = AsyncMock(side_effect=_execute)

        entity = await set_config(session, "race_key", "my-intended-value")

        # The race loser converges to the winner's stored value, NOT its own.
        assert winner_row.value == "concurrent-winner-value"
        assert entity is winner_row
        assert entity.value != "my-intended-value"
        # The first-write path uses an ON CONFLICT INSERT, not begin_nested.
        session.add.assert_not_called()

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
