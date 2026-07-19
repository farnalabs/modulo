"""Unit tests for db/crud/node_composite.py — composite node operations."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.node_composite import get_child_nodes, set_parent_node
from modulo.db.models.node import Node

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture()
def session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _make_node(*, id: uuid.UUID, parent_node_id: uuid.UUID | None = None) -> Node:
    node = MagicMock(spec=Node)
    node.id = id
    node.organisation_id = _ORG_ID
    node.parent_node_id = parent_node_id
    node.name = "test-node"
    node.timeout_seconds = None
    node.retry_count = None
    node.retry_delay_seconds = None
    return node


class TestGetChildNodes:
    async def test_returns_direct_children(self, session: AsyncMock) -> None:
        parent_id = uuid.uuid4()
        child1 = _make_node(id=uuid.uuid4(), parent_node_id=parent_id)
        child2 = _make_node(id=uuid.uuid4(), parent_node_id=parent_id)

        scalars_result = MagicMock()
        scalars_result.all.return_value = [child1, child2]
        items_result = MagicMock()
        items_result.scalars.return_value = scalars_result
        session.execute = AsyncMock(return_value=items_result)

        children = await get_child_nodes(session, parent_id)

        assert children == [child1, child2]
        session.execute.assert_awaited_once()

    async def test_returns_empty_list_when_no_children(self, session: AsyncMock) -> None:
        parent_id = uuid.uuid4()

        scalars_result = MagicMock()
        scalars_result.all.return_value = []
        items_result = MagicMock()
        items_result.scalars.return_value = scalars_result
        session.execute = AsyncMock(return_value=items_result)

        children = await get_child_nodes(session, parent_id)

        assert children == []

    async def test_filters_by_parent_node_id(self, session: AsyncMock) -> None:
        parent_id = uuid.uuid4()

        scalars_result = MagicMock()
        scalars_result.all.return_value = []
        items_result = MagicMock()
        items_result.scalars.return_value = scalars_result
        session.execute = AsyncMock(return_value=items_result)

        await get_child_nodes(session, parent_id)

        call_stmt = session.execute.call_args[0][0]
        assert isinstance(call_stmt, Select)


class TestSetParentNode:
    def _execute_result(self, scalar_return: object) -> MagicMock:
        """Build a mock that mirrors session.execute -> scalar_one_or_none()."""
        items_result = MagicMock()
        items_result.scalar_one_or_none.return_value = scalar_return
        return items_result

    async def test_sets_parent_node_id(self, session: AsyncMock) -> None:
        node_id = uuid.uuid4()
        parent_id = uuid.uuid4()
        node = _make_node(id=node_id)
        parent = _make_node(id=parent_id)

        session.execute = AsyncMock(
            side_effect=[
                self._execute_result(node),
                self._execute_result(parent),
            ]
        )

        result = await set_parent_node(session, node_id, parent_id)

        assert result is node
        assert node.parent_node_id == parent_id
        session.flush.assert_awaited_once()

    async def test_clears_parent_when_none(self, session: AsyncMock) -> None:
        node_id = uuid.uuid4()
        node = _make_node(id=node_id, parent_node_id=uuid.uuid4())

        session.execute = AsyncMock(return_value=self._execute_result(node))

        result = await set_parent_node(session, node_id, None)

        assert result is node
        assert node.parent_node_id is None
        session.flush.assert_awaited_once()

    async def test_returns_none_when_node_not_found(self, session: AsyncMock) -> None:
        session.execute = AsyncMock(return_value=self._execute_result(None))

        result = await set_parent_node(session, uuid.uuid4(), uuid.uuid4())

        assert result is None
        session.flush.assert_not_called()

    async def test_returns_none_when_parent_not_found(self, session: AsyncMock) -> None:
        node_id = uuid.uuid4()
        node = _make_node(id=node_id)

        session.execute = AsyncMock(
            side_effect=[
                self._execute_result(node),
                self._execute_result(None),
            ]
        )

        result = await set_parent_node(session, node_id, uuid.uuid4())

        assert result is None
        session.flush.assert_not_called()
