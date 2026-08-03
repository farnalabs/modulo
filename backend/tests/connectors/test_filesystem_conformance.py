"""FilesystemConnector-specific tests beyond the shared conformance suite.

The ``fs_connector`` fixture is defined in ``conftest.py`` and registered
for the auto-parametrised conformance tests automatically.
"""

from pathlib import Path

import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.filesystem import FilesystemConnector, PathTraversalError
from tests.connectors._conformance import assert_result_shape


class TestFilesystemConnector:
    async def test_health_check_ok(self, fs_connector: FilesystemConnector) -> None:
        result = await fs_connector.health_check()
        assert result.ok is True

    async def test_health_check_fails_on_missing_base(self, tmp_path: Path) -> None:
        c = FilesystemConnector(base_path=str(tmp_path / "missing"))
        result = await c.health_check()
        assert result.ok is False
        assert "does not exist" in result.detail

    async def test_browse_root(self, fs_connector: FilesystemConnector) -> None:
        result = await fs_connector.query(ConnectorQuery(resource="directory"))
        assert_result_shape(result)

    async def test_read_write_file(self, fs_connector: FilesystemConnector) -> None:
        content = "world"
        write_result = await fs_connector.write(
            ConnectorPayload(resource="file", data={"path": "hello.txt", "content": content})
        )
        assert isinstance(write_result, dict)
        assert write_result.get("bytes_written") == len(content)

        read_result = await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "hello.txt"}))
        assert_result_shape(read_result)
        assert len(read_result.records) == 1
        assert read_result.records[0]["content"] == content

    async def test_read_missing_file(self, fs_connector: FilesystemConnector) -> None:
        with pytest.raises(ValueError, match="File not found"):
            await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "nonexistent.txt"}))

    async def test_path_traversal_raises(self, fs_connector: FilesystemConnector) -> None:
        with pytest.raises(PathTraversalError):
            await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "../etc/passwd"}))

    async def test_invalid_path_empty(self, fs_connector: FilesystemConnector) -> None:
        with pytest.raises(IsADirectoryError):
            await fs_connector.query(ConnectorQuery(resource="file", filters={"path": ""}))

    async def test_write_to_nested_path_creates_intermediate_dirs(self, fs_connector: FilesystemConnector) -> None:
        content = "nested"
        result = await fs_connector.write(
            ConnectorPayload(resource="file", data={"path": "a/b/c/nested.txt", "content": content})
        )
        assert result.get("bytes_written") == len(content)
        read_result = await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "a/b/c/nested.txt"}))
        assert read_result.records[0]["content"] == content

    async def test_browse_directory_returns_children(self, fs_connector: FilesystemConnector) -> None:
        await fs_connector.write(ConnectorPayload(resource="file", data={"path": "f1.txt", "content": "one"}))
        await fs_connector.write(ConnectorPayload(resource="file", data={"path": "f2.txt", "content": "two"}))
        result = await fs_connector.query(ConnectorQuery(resource="directory"))
        paths = [r.get("name", r.get("path", "")) for r in result.records]
        assert "f1.txt" in paths
        assert "f2.txt" in paths

    async def test_overwrite_existing_file(self, fs_connector: FilesystemConnector) -> None:
        await fs_connector.write(
            ConnectorPayload(resource="file", data={"path": "overwrite.txt", "content": "original"})
        )
        await fs_connector.write(
            ConnectorPayload(resource="file", data={"path": "overwrite.txt", "content": "updated"})
        )
        read_result = await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "overwrite.txt"}))
        assert read_result.records[0]["content"] == "updated"

    async def test_connector_type_is_filesystem(self, fs_connector: FilesystemConnector) -> None:
        assert fs_connector.connector_type == ConnectorType.FILESYSTEM
