"""Seed data into "existing" tenant orgs — pipelines, teams, connectors, runs."""
from __future__ import annotations

import httpx


async def seed_existing_org(
    base_url: str,
    user_email: str,
    user_password: str,
    org_id: str,
) -> None:
    """Seed representative data into an org."""
    async with httpx.AsyncClient(base_url=base_url, verify=False) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": user_email, "password": user_password},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Seed auth failed ({resp.status_code}): {resp.text}")
        token = resp.json().get("access_token", resp.json().get("token", ""))
        headers = {"Authorization": f"Bearer {token}"}
        client.headers.update(headers)

        # Create a team
        await client.post(
            "/api/v1/teams",
            json={"name": "Engineering", "description": "Core team"},
        )

        # Create a simple pipeline
        simple_pipeline = {
            "name": "Hello World",
            "description": "Simple test pipeline",
            "nodes": [
                {
                    "id": "node-1",
                    "node_type": "prompt",
                    "config": {
                        "model": "gpt-4o-mini",
                        "prompt": "Say hello and mention the current date.",
                        "temperature": 0.7,
                    },
                    "position": {"x": 100, "y": 100},
                }
            ],
            "edges": [],
        }
        pipeline_resp = await client.post("/api/v1/pipelines", json=simple_pipeline)
        pipeline_id = pipeline_resp.json().get("id") if pipeline_resp.status_code == 201 else None

        # Create a pipeline with branching logic
        if pipeline_resp.status_code == 201:
            branch_pipeline = {
                "name": "Branching Demo",
                "description": "Pipeline with conditional branching",
                "nodes": [
                    {
                        "id": "node-1",
                        "node_type": "prompt",
                        "config": {
                            "model": "gpt-4o-mini",
                            "prompt": "Classify this input as urgent or normal.",
                            "temperature": 0.3,
                        },
                        "position": {"x": 100, "y": 100},
                    },
                    {
                        "id": "node-2",
                        "node_type": "router",
                        "config": {
                            "condition": "{{ nodes.node-1.output }} contains 'urgent'",
                            "branches": {"true": "node-3", "false": "node-4"},
                        },
                        "position": {"x": 300, "y": 100},
                    },
                    {
                        "id": "node-3",
                        "node_type": "prompt",
                        "config": {
                            "model": "gpt-4o-mini",
                            "prompt": "This is urgent! Respond immediately.",
                            "temperature": 0.5,
                        },
                        "position": {"x": 500, "y": 50},
                    },
                    {
                        "id": "node-4",
                        "node_type": "prompt",
                        "config": {
                            "model": "gpt-4o-mini",
                            "prompt": "This is normal priority. Respond within 24 hours.",
                            "temperature": 0.5,
                        },
                        "position": {"x": 500, "y": 150},
                    },
                ],
                "edges": [
                    {"source": "node-1", "target": "node-2"},
                    {"source": "node-2", "target": "node-3", "label": "urgent"},
                    {"source": "node-2", "target": "node-4", "label": "normal"},
                ],
            }
            await client.post("/api/v1/pipelines", json=branch_pipeline)

        # Run the simple pipeline
        if pipeline_id:
            run_resp = await client.post(f"/api/v1/pipelines/{pipeline_id}/runs", json={"input": {}})
            if run_resp.status_code in (200, 201):
                run_id = run_resp.json().get("id", "")
                if run_id:
                    # Poll until complete
                    import asyncio
                    for _ in range(10):
                        status_resp = await client.get(f"/api/v1/runs/{run_id}")
                        if status_resp.status_code == 200:
                            status = status_resp.json().get("status", "")
                            if status in ("completed", "failed", "cancelled"):
                                break
                        await asyncio.sleep(2)

        # Create a saved view
        await client.post(
            "/api/v1/saved-views",
            json={"name": "All Pipelines", "filter": {"status": "active"}, "is_default": True},
        )
