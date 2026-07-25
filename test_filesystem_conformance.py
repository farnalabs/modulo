"""FilesystemConnector-specific tests beyond the shared conformance suite."""
import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult, ConnectorType
from modulo.connectors.filesystem import FilesystemConnector, PathTraversalError


def _file_query(path: str) -> ConnectorQuery:
    return ConnectorQuery(resource="file", filters={"path": path})


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

    async def test_read_write_file_roundtrip(self, fs_connector: FilesystemConnector) -> None:
        write_result = await fs_connector.write(
            ConnectorPayload(resource="file", data={"path": "hello.txt", "content": "world"})
        )
        assert isinstance(write_result, dict)
        assert write_result.get("bytes_written") == 5

        read_result = await fs_connector.query(_file_query("hello.txt"))
        assert isinstance(read_result, ConnectorResult)
        assert len(read_result.records) == 1
        assert read_result.records[0]["content"] == "world"

    @pytest.mark.parametrize(
        ("path", "expected_exc", "expected_match"),
        [
            pytest.param("nonexistent.txt", ValueError, "File not found", id="missing"),
            pytest.param("../etc/passwd", PathTraversalError, None, id="traversal"),
            pytest.param("", (ValueError, PathTraversalError), None, id="empty"),
        ],
    )
    async def test_invalid_file_query_resolves(
        self, fs_connector: FilesystemConnector, path: str, expected_exc, expected_match
    ) -> None:
        with pytest.raises(expected_exc, match=expected_match):
            await fs_connector.query(_file_query(path))

    async def test_connector_type_is_filesystem(self, fs_connector: FilesystemConnector) -> None:
        assert fs_connector.connector_type == ConnectorType.FILESYSTEM
