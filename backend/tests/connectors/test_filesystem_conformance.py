"""FilesystemConnector-specific tests beyond the shared conformance suite.

The ``fs_connector`` fixture is defined in ``conftest.py`` and registered
for the auto-parametrised conformance tests automatically.
"""

import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult, ConnectorType
from modulo.connectors.filesystem import FilesystemConnector, PathTraversalError


class TestFilesystemConnector:
    async def test_health_check_ok(self, fs_connector: FilesystemConnector) -> None:
        result = await fs_connector.health_check()
        assert result.ok is True

    async def test_health_check_fails_on_missing_base(self) -> None:
        c = FilesystemConnector(base_path="/tmp/__nonexistent_modulo_test_dir__")
        result = await c.health_check()
        assert result.ok is False
        assert "does not exist" in result.detail

    async def test_browse_root(self, fs_connector: FilesystemConnector) -> None:
        result = await fs_connector.query(ConnectorQuery(resource="directory"))
        assert isinstance(result, ConnectorResult)
        assert isinstance(result.records, list)

    async def test_read_write_file(self, fs_connector: FilesystemConnector) -> None:
        write_result = await fs_connector.write(
            ConnectorPayload(resource="file", data={"path": "hello.txt", "content": "world"})
        )
        assert isinstance(write_result, dict)
        assert write_result.get("bytes_written") == 5

        read_result = await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "hello.txt"}))
        assert isinstance(read_result, ConnectorResult)
        assert len(read_result.records) == 1
        assert read_result.records[0]["content"] == "world"

    async def test_read_missing_file(self, fs_connector: FilesystemConnector) -> None:
        with pytest.raises(ValueError, match="File not found"):
            await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "nonexistent.txt"}))

    async def test_path_traversal_raises(self, fs_connector: FilesystemConnector) -> None:
        with pytest.raises(PathTraversalError):
            await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "../etc/passwd"}))

    async def test_invalid_path_empty(self, fs_connector: FilesystemConnector) -> None:
        with pytest.raises((ValueError, PathTraversalError)):
            await fs_connector.query(ConnectorQuery(resource="file", filters={"path": ""}))

    async def test_connector_type_is_filesystem(self, fs_connector: FilesystemConnector) -> None:
        assert fs_connector.connector_type == ConnectorType.FILESYSTEM
