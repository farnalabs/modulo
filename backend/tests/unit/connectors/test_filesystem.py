"""Unit tests for FilesystemConnector."""

import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.filesystem import FilesystemConnector, PathTraversalError


@pytest.fixture()
def connector(tmp_path):
    return FilesystemConnector(base_path=str(tmp_path))


async def test_health_check_ok(connector, tmp_path):
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
    await connector.write(
        ConnectorPayload(resource="file", data={"path": "out.txt", "content": "hello"})
    )
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
        await connector.query(
            ConnectorQuery(resource="file", filters={"path": "../../../etc/passwd"})
        )


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
