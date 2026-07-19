"""Unit tests for CompositeTemplate CRUD operations."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.base import PageResult


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEMPLATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _make_template(**overrides: object) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", _TEMPLATE_ID)
    t.organisation_id = overrides.get("organisation_id", _ORG_ID)
    t.name = overrides.get("name", "Devil's Advocate")
    t.description = overrides.get("description")
    t.sub_pipeline_graph_json = overrides.get("sub_pipeline_graph_json", {"nodes": [], "edges": []})
    t.parameter_ports_json = overrides.get("parameter_ports_json", [])
    t.input_schema_id = overrides.get("input_schema_id")
    t.output_schema_id = overrides.get("output_schema_id")
    t.version = overrides.get("version", "1.0.0")
    t.account_id = overrides.get("account_id", _ACCOUNT_ID)
    return t


class TestCreateCompositeTemplate:
    async def test_creates_and_returns_template(self, mock_session: AsyncMock) -> None:
        with patch(
            "modulo.db.crud.composite_template.CompositeTemplate",
            return_value=_make_template(),
        ) as mock_model:
            from modulo.db.crud.composite_template import create_composite_template

            result = await create_composite_template(
                mock_session,
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                name="Devil's Advocate",
                sub_pipeline_graph_json={"nodes": [], "edges": []},
                parameter_ports_json=[],
            )
            mock_model.assert_called_once_with(
                organisation_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                name="Devil's Advocate",
                description=None,
                sub_pipeline_graph_json={"nodes": [], "edges": []},
                parameter_ports_json=[],
                input_schema_id=None,
                output_schema_id=None,
                parameter_schema_id=None,
                version="1.0.0",
            )
            mock_session.add.assert_called_once()
            mock_session.flush.assert_awaited_once()
            assert result is not None

    async def test_creates_with_optional_fields(self, mock_session: AsyncMock) -> None:
        with patch(
            "modulo.db.crud.composite_template.CompositeTemplate",
            return_value=_make_template(),
        ) as mock_model:
            from modulo.db.crud.composite_template import create_composite_template

            ports = [{"id": "p1", "name": "system_prompt", "type": "string"}]
            result = await create_composite_template(
                mock_session,
                org_id=_ORG_ID,
                account_id=_ACCOUNT_ID,
                name="Test",
                description="A composite",
                sub_pipeline_graph_json={"nodes": []},
                parameter_ports_json=ports,
                input_schema_id=uuid.uuid4(),
                output_schema_id=uuid.uuid4(),
                version="2.0.0",
            )
            mock_model.assert_called_once()
            assert result is not None


class TestGetCompositeTemplate:
    async def test_returns_template_when_found(self, mock_session: AsyncMock) -> None:
        template = _make_template()
        scalar = MagicMock(return_value=template)
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=scalar))

        from modulo.db.crud.composite_template import get_composite_template

        result = await get_composite_template(mock_session, _TEMPLATE_ID)
        assert result is not None
        assert result.id == _TEMPLATE_ID

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        from modulo.db.crud.composite_template import get_composite_template

        result = await get_composite_template(mock_session, uuid.uuid4())
        assert result is None


class TestListCompositeTemplates:
    async def test_returns_paginated_templates(self, mock_session: AsyncMock) -> None:
        templates = [_make_template(name="Template A"), _make_template(name="Template B")]
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=10)

        scalars = MagicMock()
        scalars.all = MagicMock(return_value=templates)

        mock_session.execute = AsyncMock(
            side_effect=[
                count_result,
                MagicMock(scalars=MagicMock(return_value=scalars)),
            ]
        )

        from modulo.db.crud.composite_template import list_composite_templates

        result = await list_composite_templates(mock_session, org_id=_ORG_ID, page=1, page_size=20)
        assert isinstance(result, PageResult)
        assert len(result.items) == 2
        assert result.total == 10
        assert result.page == 1
        assert result.page_size == 20


class TestUpdateCompositeTemplate:
    async def test_updates_and_returns_template(self, mock_session: AsyncMock) -> None:
        template = _make_template(name="Updated")
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=template)))

        from modulo.db.crud.composite_template import update_composite_template

        result = await update_composite_template(mock_session, _TEMPLATE_ID, {"name": "Updated"})
        assert result is not None
        assert result.name == "Updated"
        mock_session.flush.assert_awaited_once()

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        from modulo.db.crud.composite_template import update_composite_template

        result = await update_composite_template(mock_session, uuid.uuid4(), {"name": "x"})
        assert result is None


class TestDeleteCompositeTemplate:
    async def test_deletes_and_returns_true(self, mock_session: AsyncMock) -> None:
        template = _make_template()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=template)))

        from modulo.db.crud.composite_template import delete_composite_template

        result = await delete_composite_template(mock_session, _TEMPLATE_ID)
        assert result is True
        mock_session.delete.assert_awaited_once_with(template)
        mock_session.flush.assert_awaited_once()

    async def test_returns_false_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        from modulo.db.crud.composite_template import delete_composite_template

        result = await delete_composite_template(mock_session, uuid.uuid4())
        assert result is False
