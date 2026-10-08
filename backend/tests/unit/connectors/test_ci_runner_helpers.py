"""Unit tests for the extracted dispatch helpers in GitHubActionsCIRunner."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from modulo.connectors.ci_runner.github_actions import GitHubActionsCIRunner


def _runner():
    return GitHubActionsCIRunner(token="t")


def test_split_pipeline_id_repo_only():
    r = _runner()
    assert r._split_pipeline_id("acme/widgets") == ("acme", "widgets")


def test_split_pipeline_id_with_workflow():
    r = _runner()
    assert r._split_pipeline_id("acme/widgets/ci.yml") == ("acme/widgets", "ci.yml")


def test_split_pipeline_id_empty_owner_raises():
    r = _runner()
    with pytest.raises(ValueError, match="owner/repo"):
        r._split_pipeline_id("/ci.yml")


def test_split_pipeline_id_blank_raises():
    r = _runner()
    with pytest.raises(ValueError, match="owner/repo"):
        r._split_pipeline_id("")


async def test_post_dispatch_workflow():
    r = _runner()
    client = AsyncMock()
    response = httpx.Response(204)
    client.post.return_value = response
    result = await r._post_dispatch(client, "acme/widgets", "ci.yml", "main", {"X": "1"})
    assert result is response
    args, kwargs = client.post.call_args
    assert args[0] == "/repos/acme/widgets/actions/workflows/ci.yml/dispatches"
    assert kwargs["json"]["ref"] == "main"
    assert kwargs["json"]["inputs"] == {"X": "1"}


async def test_post_dispatch_repository_dispatch():
    r = _runner()
    client = AsyncMock()
    response = httpx.Response(204)
    client.post.return_value = response
    result = await r._post_dispatch(client, "acme/widgets", "", "main", {"Y": "2"})
    assert result is response
    args, kwargs = client.post.call_args
    assert args[0] == "/repos/acme/widgets/dispatches"
    assert kwargs["json"]["event_type"] == "modulo-trigger"
    assert kwargs["json"]["client_payload"] == {"Y": "2"}


async def test_latest_dispatched_run_finds_run():
    r = _runner()
    client = AsyncMock()
    client.get.return_value = httpx.Response(
        200,
        request=httpx.Request("GET", "https://api.github.com/repos/acme/widgets/actions/runs"),
        json={"workflow_runs": [{"id": 12345, "status": "completed"}]},
    )
    run = await r._latest_dispatched_run(
        client,
        "acme/widgets",
        "ci.yml",
        "main",
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert run is not None
    assert run.id == "acme/widgets/12345"
    params = client.get.call_args.kwargs["params"]
    assert params["workflow_id"] == "ci.yml"
    assert params["branch"] == "main"
    assert params["created"] == ">=2026-01-01T00:00:00Z"


async def test_latest_dispatched_run_none_when_empty():
    r = _runner()
    client = AsyncMock()
    client.get.return_value = httpx.Response(
        200,
        request=httpx.Request("GET", "https://api.github.com/repos/acme/widgets/actions/runs"),
        json={"workflow_runs": []},
    )
    run = await r._latest_dispatched_run(
        client,
        "acme/widgets",
        "",
        "main",
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert run is None
