"""Unit tests for the extracted helper methods in ShellConnector."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from modulo.connectors.base import ConnectorResult
from modulo.connectors.shell import _ERR_RUNTIME_NOT_CONFIGURED, ShellConnector

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _connector(provider):
    return ShellConnector(
        runtime_provider=provider,
        workspace_lease_id=None,
        allowed_commands=["echo"],
    )


async def test_ensure_runtime_provider_returns_configured():
    provider = AsyncMock()
    c = _connector(provider)
    assert await c._ensure_runtime_provider() is provider


async def test_ensure_runtime_provider_raises_when_missing():
    c = _connector(None)
    with pytest.raises(ValueError, match=_ERR_RUNTIME_NOT_CONFIGURED):
        await c._ensure_runtime_provider()


async def test_read_file_ok():
    provider = AsyncMock()
    provider.execute_command.return_value = {
        "exit_code": 0,
        "stdout": "file contents",
        "stderr": "",
    }
    c = _connector(provider)
    result = await c._read_file(provider, "ref1", "/etc/hosts")
    assert isinstance(result, ConnectorResult)
    assert result.records == [{"path": "/etc/hosts", "content": "file contents"}]
    provider.execute_command.assert_awaited_once()


async def test_read_file_nonzero_exit_raises():
    provider = AsyncMock()
    provider.execute_command.return_value = {
        "exit_code": 2,
        "stdout": "",
        "stderr": "no such file",
    }
    c = _connector(provider)
    with pytest.raises(ValueError, match="Failed to read file"):
        await c._read_file(provider, "ref1", "/missing")


async def test_list_directory_ok():
    provider = AsyncMock()
    provider.execute_command.return_value = {
        "exit_code": 0,
        "stdout": ".\n..\na\nb\n",
        "stderr": "",
    }
    c = _connector(provider)
    result = await c._list_directory(provider, "ref1", "/tmp/dir")
    assert result.total == 2
    names = {e["name"] for e in result.records}
    assert names == {"a", "b"}
    paths = {e["path"] for e in result.records}
    assert paths == {"/tmp/dir/a", "/tmp/dir/b"}


async def test_list_directory_nonzero_exit_returns_empty():
    provider = AsyncMock()
    provider.execute_command.return_value = {
        "exit_code": 1,
        "stdout": "",
        "stderr": "denied",
    }
    c = _connector(provider)
    result = await c._list_directory(provider, "ref1", "/tmp/dir")
    assert result.records == []
    assert result.total == 0
