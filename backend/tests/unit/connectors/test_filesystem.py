"""Unit tests for FilesystemConnector."""

import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.filesystem import FilesystemConnector, PathTraversalError


@pytest.fixture
def connector(tmp_path):
    return FilesystemConnector(base_path=str(tmp_path))


async def test_health_check_ok(connector):
    result = await connector.health_check()
    assert result.ok is True


async def test_health_check_fail(tmp_path):
    missing = tmp_path / "nonexistent"
    c = FilesystemConnector(base_path=str(missing))
    result = await c.health_check()
    assert result.ok is False
    assert "nonexistent" in result.detail


async def test_read_file(connector, tmp_path):
    (tmp_path / "hello.txt").write_text("world", encoding="utf-8")
    result = await connector.query(ConnectorQuery(resource="file", filters={"path": "hello.txt"}))
    assert len(result.records) == 1
    assert result.records[0]["content"] == "world"


async def test_list_directory(connector, tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    result = await connector.query(ConnectorQuery(resource="directory", filters={"path": "."}))
    names = [r["name"] for r in result.records]
    assert "a.txt" in names
    assert "b.txt" in names
    assert result.total == 2


async def test_write_file(connector, tmp_path):
    await connector.write(ConnectorPayload(resource="file", data={"path": "out.txt", "content": "hello"}))
    assert (tmp_path / "out.txt").read_text() == "hello"


async def test_write_creates_parent_dirs(connector, tmp_path):
    await connector.write(
        ConnectorPayload(
            resource="file",
            data={"path": "sub/dir/file.txt", "content": "nested"},
        )
    )
    assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"


async def test_path_traversal_blocked(connector):
    with pytest.raises(PathTraversalError):
        await connector.query(ConnectorQuery(resource="file", filters={"path": "../../../etc/passwd"}))


async def test_path_traversal_in_write_blocked(connector):
    with pytest.raises(PathTraversalError):
        await connector.write(
            ConnectorPayload(
                resource="file",
                data={"path": "../../escape.txt", "content": "bad"},
            )
        )


async def test_unsupported_query_resource(connector):
    with pytest.raises(ValueError, match="Unsupported filesystem resource"):
        await connector.query(ConnectorQuery(resource="unknown"))


async def test_unsupported_write_resource(connector):
    with pytest.raises(ValueError, match="Unsupported filesystem write resource"):
        await connector.write(ConnectorPayload(resource="blob", data={}))


def test_connector_type(connector):
    from modulo.connectors.base import ConnectorType

    assert connector.connector_type == ConnectorType.FILESYSTEM


def test_init_rejects_empty_base_path():
    with pytest.raises(ValueError, match="base_path must be a non-empty directory path"):
        FilesystemConnector(base_path="")


async def test_query_file_requires_path_filter(connector):
    with pytest.raises(ValueError, match="requires 'path' filter"):
        await connector.query(ConnectorQuery(resource="file"))


async def test_write_requires_path_key(connector):
    with pytest.raises(ValueError, match="requires 'path' in data"):
        await connector.write(ConnectorPayload(resource="file", data={"content": "x"}))


async def test_write_requires_content(connector):
    with pytest.raises(ValueError, match="requires 'content' in data"):
        await connector.write(ConnectorPayload(resource="file", data={"path": "out.txt"}))


async def test_query_directory_respects_limit(connector, tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text(str(i), encoding="utf-8")
    result = await connector.query(ConnectorQuery(resource="directory", limit=2))
    assert len(result.records) == 2
    assert result.total == 2


async def test_list_missing_directory_raises(connector):
    with pytest.raises(FileNotFoundError):
        await connector.query(ConnectorQuery(resource="directory", filters={"path": "missing_dir"}))


async def test_read_directory_as_file_raises(connector, tmp_path):
    (tmp_path / "sub").mkdir()
    with pytest.raises(IsADirectoryError, match="Cannot read directory as file"):
        await connector.query(ConnectorQuery(resource="file", filters={"path": "sub"}))
