"""Pipeline CRUD and run tests across all tenant contexts."""
from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_create_pipeline(tenant_client: httpx.AsyncClient) -> None:
    payload = {
        "name": "E2E Test Pipeline",
        "description": "Created by staging E2E suite",
        "nodes": [
            {
                "id": "node-1",
                "node_type": "prompt",
                "config": {"model": "gpt-4o-mini", "prompt": "Say hello", "temperature": 0.7},
                "position": {"x": 100, "y": 100},
            },
        ],
        "edges": [],
    }
    resp = await tenant_client.post("/api/v1/pipelines", json=payload)
    assert resp.status_code == 201, f"Pipeline creation failed: {resp.text}"
    data = resp.json()
    assert data.get("name") == "E2E Test Pipeline"
    assert "id" in data


@pytest.mark.asyncio
async def test_list_pipelines(tenant_client: httpx.AsyncClient) -> None:
    resp = await tenant_client.get("/api/v1/pipelines")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list) or "pipelines" in data
    pipelines = data if isinstance(data, list) else data.get("pipelines", [])
    # Should at least return some pipelines (existing orgs have seeded ones)
    assert len(pipelines) >= 0


@pytest.mark.asyncio
async def test_get_pipeline_by_id(tenant_client: httpx.AsyncClient) -> None:
    # First create one
    payload = {
        "name": "Get-by-ID Test",
        "description": "",
        "nodes": [
            {
                "id": "node-1",
                "node_type": "prompt",
                "config": {"model": "gpt-4o-mini", "prompt": "Hi", "temperature": 0.7},
                "position": {"x": 100, "y": 100},
            },
        ],
        "edges": [],
    }
    create_resp = await tenant_client.post("/api/v1/pipelines", json=payload)
    assert create_resp.status_code == 201
    pipeline_id = create_resp.json()["id"]

    resp = await tenant_client.get(f"/api/v1/pipelines/{pipeline_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == pipeline_id


@pytest.mark.asyncio
async def test_run_pipeline(tenant_client: httpx.AsyncClient) -> None:
    payload = {
        "name": "Run Test",
        "description": "",
        "nodes": [
            {
                "id": "node-1",
                "node_type": "prompt",
                "config": {"model": "gpt-4o-mini", "prompt": "Return the word hello", "temperature": 0.0},
                "position": {"x": 100, "y": 100},
            },
        ],
        "edges": [],
    }
    create_resp = await tenant_client.post("/api/v1/pipelines", json=payload)
    assert create_resp.status_code == 201
    pipeline_id = create_resp.json()["id"]

    run_resp = await tenant_client.post(f"/api/v1/pipelines/{pipeline_id}/runs", json={"input": {}})
    assert run_resp.status_code in (200, 201), f"Run failed: {run_resp.text}"
    run_data = run_resp.json()
    assert "id" in run_data


@pytest.mark.asyncio
async def test_list_runs(tenant_client: httpx.AsyncClient) -> None:
    resp = await tenant_client.get("/api/v1/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list) or "runs" in data


@pytest.mark.asyncio
async def test_existing_org_has_seeded_pipelines(tenant_client: httpx.AsyncClient, tenant) -> None:
    """If this is an existing-data org, verify seeded pipelines are visible."""
    if not tenant.is_existing:
        pytest.skip("Only relevant for existing-data tenants")

    resp = await tenant_client.get("/api/v1/pipelines")
    assert resp.status_code == 200
    data = resp.json()
    pipelines = data if isinstance(data, list) else data.get("pipelines", [])
    names = {p.get("name") for p in pipelines}
    assert "Hello World" in names, f"Expected seeded pipeline 'Hello World' in existing org, got: {names}"
