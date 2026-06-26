"""Unit tests for db/rls.py multi-backend behavior — set_rls_org, _inject_tenant_filter."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState

from modulo.db.rls import _inject_tenant_filter, set_rls_org

_TENANT_KEY = "org_id"
_TENANT_COLUMN = "organisation_id"
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ORG_ROLE = "admin"


class EntityWithOrg:
    organisation_id = None
    id = None


class AnotherEntityWithOrg:
    organisation_id = None
    id = None


class EntityWithoutOrg:
    id = None


def _make_session(
    *, in_tx: bool = True, dialect: str = "postgresql", org_id: uuid.UUID | None = None
) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.in_transaction.return_value = in_tx
    session.execute = AsyncMock()

    bind = MagicMock()
    bind.dialect.name = dialect

    async def _get_bind() -> MagicMock:
        return bind

    session.get_bind = _get_bind
    session.info = {}
    if org_id is not None:
        session.info[_TENANT_KEY] = org_id
    return session


def _make_execute_state(
    *,
    org_id: uuid.UUID | None = _ORG_ID,
    is_select: bool = True,
    column_descriptions: list[dict] | None = None,
    all_mapper_classes: list | None = None,
) -> MagicMock:
    state = MagicMock()
    state.session.info = {_TENANT_KEY: org_id} if org_id else {}
    state.is_select = is_select
    state.is_update = False
    state.is_delete = False
    state.statement = MagicMock()
    if column_descriptions is not None:
        state.statement.column_descriptions = column_descriptions
    if all_mapper_classes is not None:
        state.all_mapper_classes = all_mapper_classes
    return state


class TestSetRlsOrgMultiBackend:
    """Verify set_rls_org dispatches correctly for postgres vs generic backends."""

    async def test_postgres_calls_set_config(self) -> None:
        session = _make_session(dialect="postgresql")
        await set_rls_org(session, _ORG_ID)
        session.execute.assert_awaited_once()
        call_text = str(session.execute.await_args[0][0].compile())
        assert "set_config" in call_text
        assert "app.organisation_id" in call_text

    async def test_generic_stores_in_session_info(self) -> None:
        session = _make_session(dialect="sqlite")
        await set_rls_org(session, _ORG_ID)
        session.execute.assert_not_called()
        assert session.info[_TENANT_KEY] == _ORG_ID

    async def test_mariadb_stores_in_session_info(self) -> None:
        session = _make_session(dialect="mysql")
        await set_rls_org(session, _ORG_ID)
        session.execute.assert_not_called()
        assert session.info[_TENANT_KEY] == _ORG_ID

    async def test_raises_without_active_transaction(self) -> None:
        session = _make_session(in_tx=False)
        with pytest.raises(RuntimeError, match="requires an active transaction"):
            await set_rls_org(session, _ORG_ID)

    async def test_normalizes_postgresql_dialect_to_postgres(self) -> None:
        session = _make_session(dialect="postgresql")
        await set_rls_org(session, _ORG_ID)
        session.execute.assert_awaited_once()


class TestInjectTenantFilter:
    """Verify _inject_tenant_filter adds WHERE clauses for all backends."""

    def test_select_single_entity_injects_where(self) -> None:
        state = _make_execute_state(
            column_descriptions=[{"entity": EntityWithOrg}],
        )
        original_where = state.statement.where

        _inject_tenant_filter(state)

        original_where.assert_called_once()
        assert state.statement is original_where.return_value

    def test_select_skips_when_no_org_id_in_session(self) -> None:
        state = _make_execute_state(org_id=None, column_descriptions=[{"entity": EntityWithOrg}])

        _inject_tenant_filter(state)

        state.statement.where.assert_not_called()

    def test_select_skips_non_select_update_delete(self) -> None:
        state = _make_execute_state(
            is_select=False,
            column_descriptions=[{"entity": EntityWithOrg}],
        )
        state.is_insert = True

        _inject_tenant_filter(state)

        state.statement.where.assert_not_called()

    def test_select_skips_entities_without_org_column(self) -> None:
        state = _make_execute_state(
            column_descriptions=[{"entity": EntityWithoutOrg}],
        )

        _inject_tenant_filter(state)

        state.statement.where.assert_not_called()

    def test_select_skips_none_and_object_entities(self) -> None:
        state = _make_execute_state(
            column_descriptions=[
                {"entity": None},
                {"entity": object},
            ],
        )

        _inject_tenant_filter(state)

        state.statement.where.assert_not_called()

    def test_join_query_injects_for_all_org_entities(self) -> None:
        state = _make_execute_state(
            column_descriptions=[
                {"entity": EntityWithOrg},
                {"entity": AnotherEntityWithOrg},
                {"entity": EntityWithoutOrg},
            ],
        )
        original_where = state.statement.where

        _inject_tenant_filter(state)

        original_where.assert_called_once()
        original_where.return_value.where.assert_called_once()

    def test_dml_uses_all_mapper_classes(self) -> None:
        mapper1 = MagicMock()
        mapper1.class_ = EntityWithOrg
        mapper2 = MagicMock()
        mapper2.class_ = EntityWithoutOrg
        state = _make_execute_state(
            is_select=False,
            all_mapper_classes=[mapper1, mapper2],
        )
        state.is_update = True
        original_where = state.statement.where

        _inject_tenant_filter(state)

        original_where.assert_called_once()

    def test_dml_skips_when_mapper_has_no_org_column(self) -> None:
        mapper = MagicMock()
        mapper.class_ = EntityWithoutOrg
        state = _make_execute_state(
            is_select=False,
            all_mapper_classes=[mapper],
        )
        state.is_delete = True

        _inject_tenant_filter(state)

        state.statement.where.assert_not_called()
