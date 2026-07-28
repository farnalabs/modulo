"""Shared conftest for the connector conformance suite."""
from pathlib import Path

import pytest
from modulo.connectors.base import ConnectorBase
from tests.connectors._conformance import (
    get_registered_fixture,
    get_registered_types,
    register_conformance_connector,
)


@pytest.fixture
def fs_connector(tmp_path: Path):
    from modulo.connectors.filesystem import FilesystemConnector
    return FilesystemConnector(base_path=str(tmp_path))


register_conformance_connector("filesystem", "fs_connector")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "connector_type" in metafunc.fixturenames or "conformance_connector" in metafunc.fixturenames:
        types = get_registered_types()
        if types:
            metafunc.parametrize("connector_type", types, ids=types)


@pytest.fixture
def conformance_connector(connector_type: str, request: pytest.FixtureRequest) -> ConnectorBase:
    fixture_name = get_registered_fixture(connector_type)
    if fixture_name is None:
        pytest.fail(f"No fixture registered for connector type {connector_type!r}")
    return request.getfixturevalue(fixture_name)
