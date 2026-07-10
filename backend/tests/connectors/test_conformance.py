"""Connector contract conformance suite.

Every ConnectorType implementation must pass these scenarios.  When a new
connector is added, register it in its test module via
``register_conformance_connector()`` and the tests below are parametrised
automatically.

Run with::

    pytest tests/connectors/ -v
"""

import pytest

from modulo.connectors.base import ConnectorBase, ConnectorPayload, ConnectorQuery, ConnectorType
from tests.connectors._conformance import assert_health_shape

pytestmark = pytest.mark.connector_conformance


class TestConnectorInitialisation:
    async def test_connector_type_returns_valid_enum(
        self, connector_type: str, conformance_connector: ConnectorBase
    ) -> None:
        t = conformance_connector.connector_type
        assert isinstance(t, ConnectorType), f"Expected ConnectorType, got {type(t).__name__}"
        assert t in ConnectorType, f"{t!r} is not a known ConnectorType member"


class TestConnectorHealthCheck:
    async def test_health_check_returns_health_result(
        self, connector_type: str, conformance_connector: ConnectorBase
    ) -> None:
        result = await conformance_connector.health_check()
        assert_health_shape(result)


class TestConnectorQuery:
    async def test_empty_resource_raises(self, connector_type: str, conformance_connector: ConnectorBase) -> None:
        with pytest.raises((ValueError, KeyError, AttributeError)):
            await conformance_connector.query(ConnectorQuery(resource=""))

    async def test_unknown_resource_raises(self, connector_type: str, conformance_connector: ConnectorBase) -> None:
        with pytest.raises((ValueError, KeyError)):
            await conformance_connector.query(ConnectorQuery(resource="__nonexistent_resource_xyz__"))


class TestConnectorWrite:
    async def test_empty_payload_raises(self, connector_type: str, conformance_connector: ConnectorBase) -> None:
        with pytest.raises((ValueError, KeyError, AttributeError)):
            await conformance_connector.write(ConnectorPayload(resource="", data={}))

    async def test_unknown_write_resource_raises(
        self, connector_type: str, conformance_connector: ConnectorBase
    ) -> None:
        with pytest.raises((ValueError, KeyError)):
            await conformance_connector.write(ConnectorPayload(resource="__nonexistent_write_resource__", data={}))
