"""FilesystemConnector-specific tests beyond the shared conformance suite.

The ``fs_connector`` fixture is defined in ``conftest.py`` and registered
for the auto-parametrised conformance tests automatically.
"""

from pathlib import Path

import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.filesystem import FilesystemConnector, PathTraversalError
from tests.connectors._conformance import assert_result_shape, assert_write_result_shape


class TestFilesystemConnector:
    async def test_health_check_ok(self, fs_connector: FilesystemConnector) -> None:
        result = await fs_connector.health_check()
        assert result.ok is True
        assert not result.detail

    async def test_health_check_fails_on_missing_base(self, tmp_path: Path) -> None:
        c = FilesystemConnector(base_path=str(tmp_path / "missing"))
        result = await c.health_check()
        assert result.ok is False
        assert "does not exist" in result.detail

    async def test_browse_root(self, fs_connector: FilesystemConnector) -> None:
        result = await fs_connector.query(ConnectorQuery(resource="directory"))
        assert_result_shape(result)
        assert not result.records
        assert result.total == 0

    async def test_read_write_file(self, fs_connector: FilesystemConnector) -> None:
        content = "world"
        write_result = await fs_connector.write(
            ConnectorPayload(resource="file", data={"path": "hello.txt", "content": content})
        )
        assert_write_result_shape(write_result)
        assert write_result.get("bytes_written") == len(content)
        assert "hello.txt" in write_result.get("path", "")

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

    async def test_path_traversal_via_symlink_raises(
        self,
        fs_connector: FilesystemConnector,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        outside_dir = tmp_path_factory.mktemp("outside")
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret", encoding="utf-8")
        (tmp_path / "link.txt").symlink_to(outside_file)
        with pytest.raises(PathTraversalError):
            await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "link.txt"}))

    async def test_path_traversal_via_symlink_in_write_raises(
        self,
        fs_connector: FilesystemConnector,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        outside_dir = tmp_path_factory.mktemp("outside")
        (tmp_path / "link_dir").symlink_to(outside_dir, target_is_directory=True)
        with pytest.raises(PathTraversalError):
            await fs_connector.write(
                ConnectorPayload(resource="file", data={"path": "link_dir/escape.txt", "content": "x"})
            )

    async def test_invalid_path_empty(self, fs_connector: FilesystemConnector) -> None:
        with pytest.raises(IsADirectoryError, match="Cannot read directory as file"):
            await fs_connector.query(ConnectorQuery(resource="file", filters={"path": ""}))

    async def test_read_directory_as_file_raises(self, fs_connector: FilesystemConnector) -> None:
        await fs_connector.write(ConnectorPayload(resource="file", data={"path": "sub/inner.txt", "content": "x"}))
        with pytest.raises(IsADirectoryError, match="Cannot read directory as file"):
            await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "sub"}))

    async def test_write_to_nested_path_creates_intermediate_dirs(self, fs_connector: FilesystemConnector) -> None:
        content = "nested"
        result = await fs_connector.write(
            ConnectorPayload(resource="file", data={"path": "a/b/c/nested.txt", "content": content})
        )
        assert_write_result_shape(result)
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

    async def test_browse_directory_without_path_filter_lists_root(self, fs_connector: FilesystemConnector) -> None:
        await fs_connector.write(ConnectorPayload(resource="file", data={"path": "root.txt", "content": "x"}))
        result = await fs_connector.query(ConnectorQuery(resource="directory"))
        paths = [r.get("name", r.get("path", "")) for r in result.records]
        assert "root.txt" in paths

    async def test_browse_directory_respects_limit(self, fs_connector: FilesystemConnector) -> None:
        for i in range(3):
            await fs_connector.write(ConnectorPayload(resource="file", data={"path": f"f{i}.txt", "content": "x"}))
        result = await fs_connector.query(ConnectorQuery(resource="directory", limit=2))
        assert len(result.records) == 2
        assert result.total == 2

    async def test_overwrite_existing_file(self, fs_connector: FilesystemConnector) -> None:
        first_write = await fs_connector.write(
            ConnectorPayload(resource="file", data={"path": "overwrite.txt", "content": "original"})
        )
        assert_write_result_shape(first_write)
        await fs_connector.write(
            ConnectorPayload(resource="file", data={"path": "overwrite.txt", "content": "updated"})
        )
        read_result = await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "overwrite.txt"}))
        assert read_result.records[0]["content"] == "updated"

    async def test_file_read_returns_absolute_path_inside_base(
        self, fs_connector: FilesystemConnector, tmp_path: Path
    ) -> None:
        await fs_connector.write(ConnectorPayload(resource="file", data={"path": "loc.txt", "content": "x"}))
        result = await fs_connector.query(ConnectorQuery(resource="file", filters={"path": "loc.txt"}))
        returned = Path(result.records[0]["path"])
        assert returned.is_absolute()
        assert returned.is_relative_to(tmp_path)

    async def test_write_returns_absolute_path_inside_base(
        self, fs_connector: FilesystemConnector, tmp_path: Path
    ) -> None:
        result = await fs_connector.write(ConnectorPayload(resource="file", data={"path": "out.txt", "content": "x"}))
        assert_write_result_shape(result)
        returned = Path(result["path"])
        assert returned.is_absolute()
        assert returned.is_relative_to(tmp_path)

    async def test_directory_listing_includes_type_field(
        self, fs_connector: FilesystemConnector, tmp_path: Path
    ) -> None:
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        result = await fs_connector.query(ConnectorQuery(resource="directory"))
        by_name = {r["name"]: r for r in result.records}
        assert by_name["f.txt"]["type"] == "file"
        assert by_name["sub"]["type"] == "dir"
        assert Path(by_name["f.txt"]["path"]).is_absolute()

    async def test_connector_type_is_filesystem(self, fs_connector: FilesystemConnector) -> None:
        assert fs_connector.connector_type == ConnectorType.FILESYSTEM
