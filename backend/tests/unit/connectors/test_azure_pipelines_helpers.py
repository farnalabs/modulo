"""Unit tests for the extracted log/run-id helpers in AzurePipelinesConnector."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from modulo.connectors.azure_pipelines import AzurePipelinesConnector


def _connector():
    return AzurePipelinesConnector(token="t", organization="org", project="proj")


def test_split_run_id_valid():
    c = _connector()
    assert c._split_run_id("123/456") == ("123", "456")


def test_split_run_id_no_slash_raises():
    c = _connector()
    with pytest.raises(ValueError, match="Invalid run_id format"):
        c._split_run_id("onlyid")


def test_split_run_id_invalid_raises():
    c = _connector()
    with pytest.raises(ValueError, match="Invalid run_id format"):
        c._split_run_id("/")


async def test_append_log_content_ok():
    c = _connector()
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, text="line1\nline2")
    all_lines: list[str] = []
    await c._append_log_content(client, "http://log", all_lines)
    assert all_lines == ["  line1", "  line2"]


async def test_append_log_content_non_200_ignored():
    c = _connector()
    client = AsyncMock()
    client.get.return_value = httpx.Response(404, text="nope")
    all_lines: list[str] = []
    await c._append_log_content(client, "http://log", all_lines)
    assert all_lines == []


async def test_collect_log_lines_with_url():
    c = _connector()
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, text="a\nb")
    logs_data = [{"id": "1", "url": "http://log", "name": "build"}]
    result = await c._collect_log_lines(client, logs_data)
    assert result == ["--- Log: build (1) ---", "  a", "  b", ""]


async def test_collect_log_lines_without_url():
    c = _connector()
    client = AsyncMock()
    logs_data = [{"id": "2", "name": "x"}]
    result = await c._collect_log_lines(client, logs_data)
    assert result == ["--- Log: x (2) ---", ""]


async def test_collect_log_lines_skips_non_dict():
    c = _connector()
    client = AsyncMock()
    result = await c._collect_log_lines(client, ["not-a-dict", None])
    assert result == []
