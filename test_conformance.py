"""Connector contract conformance suite."""

import pytest
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorType,
)

from tests.connectors._conformance import assert_health_shape

pytestmark = [pytest.mark.connector_conformance, pytest.mark.asyncio(loop_scope="module")]


class TestConnectorInitialisation:
    async def test_connector_type_returns_valid_enum(self, conformance_connector: ConnectorBase) -> None:
        t = conformance_connector.connector_type
        assert isinstance(t, ConnectorType)
        assert t in ConnectorType


class TestConnectorHealthCheck:
    async def test_health_check_returns_health_result(self, conformance_connector: ConnectorBase) -> None:
        result = await conformance_connector.health_check()
        assert_health_shape(result)


class TestConnectorQuery:
    @pytest.mark.parametrize(
        ("query", "expected_exc"),
        [
            pytest.param(ConnectorQuery(resource=""), (ValueError, KeyError, AttributeError), id="empty_resource"),
            pytest.param(
                ConnectorQuery(resource="__nonexistent_resource_xyz__"), (ValueError, KeyError), id="unknown_resource"
            ),
        ],
    )
    async def test_invalid_query_raises(
        self, conformance_connector: ConnectorBase, query: ConnectorQuery, expected_exc
    ) -> None:
        with pytest.raises(expected_exc):
            await conformance_connector.query(query)


class TestConnectorWrite:
    @pytest.mark.parametrize(
        ("payload", "expected_exc"),
        [
            pytest.param(
                ConnectorPayload(resource="", data={}), (ValueError, KeyError, AttributeError), id="empty_payload"
            ),
            pytest.param(
                ConnectorPayload(resource="__nonexistent_write_resource__", data={}),
                (ValueError, KeyError),
                id="unknown_write_resource",
            ),
        ],
    )
    async def test_invalid_payload_raises(
        self, conformance_connector: ConnectorBase, payload: ConnectorPayload, expected_exc
    ) -> None:
        with pytest.raises(expected_exc):
            await conformance_connector.write(payload)
