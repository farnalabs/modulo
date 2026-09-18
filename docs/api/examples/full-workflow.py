"""
Modulo API Example: Complete Workflow

Demonstrates a full end-to-end workflow:
  1. Login with email/password
  2. Create a pipeline
  3. Create an agent
  4. Add the agent as a node to the pipeline graph
  5. Configure a connector binding
  6. Create a schema, assign to the agent
  7. Trigger a run
  8. Monitor via WebSocket
  9. Handle any HITL gates that arise
  10. View run results

Fully runnable with environment variable config.

Usage:
  export MODULO_URL=http://localhost:8000
  export MODULO_EMAIL=admin@example.com
  export MODULO_PASSWORD=changeme
  python full-workflow.py

Requires: httpx (pip install httpx)
"""

import json
import os
import sys
import time
import uuid
from urllib.parse import urlparse

import httpx

BASE_URL = os.getenv("MODULO_URL", "http://localhost:8000").rstrip("/")
EMAIL = os.getenv("MODULO_EMAIL")
PASSWORD = os.getenv("MODULO_PASSWORD")
POLL_INTERVAL = float(os.getenv("MODULO_POLL_INTERVAL", "2"))
MAX_POLLS = int(os.getenv("MODULO_MAX_POLLS", "30"))


def bail(msg: str):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def login(client: httpx.Client) -> tuple[str, str, str]:
    """Authenticate and return (access_token, refresh_token, user_id)."""
    resp = client.post(
        "/api/v1/auth/login",
        json={
            "email": EMAIL,
            "password": PASSWORD,
        },
    )
    if resp.status_code != 200:
        bail(f"Login failed: {resp.status_code} {resp.text}")
    data = resp.json()
    print(f"✓ Logged in as {EMAIL}")

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"}).json()
    return data["access_token"], data["refresh_token"], me["id"]


def step(label: str):
    print(f"\n─── {label} {'─' * max(0, 60 - len(label))}")


def main():
    if not EMAIL or not PASSWORD:
        bail("Set MODULO_EMAIL and MODULO_PASSWORD")

    # ── Shared state ──────────────────────────────────────────────
    token = None
    headers: dict = {}
    pipeline_id = None
    agent_id = None
    connector_id = None
    schema_id = None
    run_id = None

    # httpx supports both sync and async; we use sync for simplicity
    # and only use asyncio for the WebSocket monitor.
    client = httpx.Client(base_url=BASE_URL, timeout=30)

    try:
        # ═══════════════════════════════════════════════════════════
        # STEP 1: Login
        # ═══════════════════════════════════════════════════════════
        step("1. Authentication")
        token, _, _ = login(client)
        headers = {"Authorization": f"Bearer {token}"}

        # ═══════════════════════════════════════════════════════════
        # STEP 2: Create a Pipeline
        # ═══════════════════════════════════════════════════════════
        step("2. Create Pipeline")
        resp = client.post(
            "/api/v1/pipelines",
            json={
                "name": f"Full Workflow Demo {uuid.uuid4().hex[:8]}",
                "description": "Created by full-workflow.py example",
                "visibility": "org",
                "max_concurrent_runs": 3,
            },
            headers=headers,
        )
        if resp.status_code != 201:
            bail(f"Create pipeline failed: {resp.status_code} {resp.text}")
        pipeline = resp.json()
        pipeline_id = pipeline["id"]
        print(f"  Pipeline: {pipeline['name']} ({pipeline_id})")

        # ═══════════════════════════════════════════════════════════
        # STEP 3: Create a Schema (input/output contract for the agent)
        # ═══════════════════════════════════════════════════════════
        step("3. Create Schema")
        resp = client.post(
            "/api/v1/schemas",
            json={
                "name": "PR Review Input",
                "description": "Input schema for code review agent",
            },
            headers=headers,
        )
        if resp.status_code == 201:
            schema = resp.json()
            schema_id = schema["id"]
            # Create a version of the schema
            client.post(
                f"/api/v1/schemas/{schema_id}/versions",
                json={
                    "version": "1.0.0",
                    "version_number": 1,
                    "definition_json": {
                        "type": "object",
                        "properties": {
                            "pr_url": {"type": "string", "description": "URL of the PR"},
                            "diff": {"type": "string", "description": "PR diff content"},
                        },
                        "required": ["pr_url"],
                    },
                    "published": True,
                },
                headers=headers,
            )
            print(f"  Schema: {schema['name']} ({schema_id})")
        else:
            print(f"  Schema creation skipped ({resp.status_code}): {resp.text}")
            # Use existing schema if available
            schemas = client.get("/api/v1/schemas", params={"page": 1, "page_size": 1}, headers=headers)
            if schemas.status_code == 200 and schemas.json()["items"]:
                schema_id = schemas.json()["items"][0]["id"]
                print(f"  Using existing schema: {schema_id}")

        # ═══════════════════════════════════════════════════════════
        # STEP 4: Create an Agent
        # ═══════════════════════════════════════════════════════════
        step("4. Create Agent")
        # Find a model backend to use
        backends = client.get("/api/v1/model-backends", params={"page": 1, "page_size": 5}, headers=headers)
        backend_id = None
        if backends.status_code == 200 and backends.json()["items"]:
            backend_id = backends.json()["items"][0]["id"]

        agent_payload = {
            "name": "Code Reviewer",
            "description": "Reviews PR diffs for code quality and security",
            "prompt_template": (
                "You are a senior code reviewer. Review the following PR diff "
                "and provide feedback on:\n"
                "1. Code quality and style\n"
                "2. Security concerns\n"
                "3. Performance implications\n\n"
                "PR URL: {{pr_url}}\n\n"
                "Diff:\n{{diff}}"
            ),
            "input_schema_id": schema_id,
        }
        if backend_id:
            agent_payload["model_backend_id"] = backend_id

        resp = client.post("/api/v1/agents", json=agent_payload, headers=headers)
        if resp.status_code != 201:
            bail(f"Create agent failed: {resp.status_code} {resp.text}")
        agent = resp.json()
        agent_id = agent["id"]
        print(f"  Agent: {agent['name']} ({agent_id})")

        # ═══════════════════════════════════════════════════════════
        # STEP 5: Configure a Connector
        # ═══════════════════════════════════════════════════════════
        step("5. Configure Connector")
        resp = client.get("/api/v1/connectors", params={"page": 1, "page_size": 5}, headers=headers)
        if resp.status_code == 200 and resp.json()["items"]:
            connector = resp.json()["items"][0]
            connector_id = connector["id"]
            print(f"  Using existing connector: {connector['name']} ({connector_id})")
        else:
            # Create a filesystem connector (no external credentials needed)
            resp = client.post(
                "/api/v1/connectors",
                json={
                    "name": "Demo Filesystem Connector",
                    "connector_type_id": "filesystem",
                    "config_json": {"base_path": "/var/lib/modulo/connector-data/modulo-demo"},
                },
                headers=headers,
            )
            if resp.status_code == 201:
                connector = resp.json()
                connector_id = connector["id"]
                print(f"  Created connector: {connector.get('name', 'N/A')} ({connector_id})")
            else:
                print(f"  Connector creation skipped ({resp.status_code}) — continuing without")

        # ═══════════════════════════════════════════════════════════
        # STEP 6: Set Pipeline Graph (add agent node)
        # ═══════════════════════════════════════════════════════════
        step("6. Set Pipeline Graph")
        node_id = f"agent-{uuid.uuid4().hex[:8]}"
        graph = {
            "nodes": [
                {
                    "id": node_id,
                    "type": "agent",
                    "data": {
                        "agent_id": agent_id,
                        "label": "Code Review Step",
                        "config": {},
                    },
                    "position": {"x": 200, "y": 200},
                },
            ],
            "edges": [],
        }
        resp = client.patch(f"/api/v1/pipelines/{pipeline_id}/graph", json=graph, headers=headers)
        if resp.status_code == 200:
            print(f"  Graph saved with 1 node (agent: {agent_id})")
        else:
            print(f"  Graph update: {resp.status_code} — {resp.text}")

        # ═══════════════════════════════════════════════════════════
        # STEP 7: Trigger a Run
        # ═══════════════════════════════════════════════════════════
        step("7. Trigger Run")
        resp = client.post(
            "/api/v1/runs",
            json={
                "pipeline_id": pipeline_id,
                "input_payload": {
                    "pr_url": "https://github.com/example/org/pull/42",
                    "diff": (
                        "diff --git a/src/main.py b/src/main.py\n"
                        "index abc..def 100644\n"
                        "--- a/src/main.py\n"
                        "+++ b/src/main.py\n"
                        "@@ -10,6 +10,8 @@\n"
                        " def process(data):\n"
                        "+    # TODO: validate input\n"
                        "     result = execute(data)\n"
                        "     return result\n"
                    ),
                },
            },
            headers=headers,
        )
        if resp.status_code != 202:
            bail(f"Trigger run failed: {resp.status_code} {resp.text}")
        run = resp.json()
        run_id = run["run_id"]
        print(f"  Run triggered: {run_id}")
        print(f"  Initial status: {run['status']}")

        # ═══════════════════════════════════════════════════════════
        # STEP 8: Poll Run Status + Monitor via WebSocket
        # ═══════════════════════════════════════════════════════════
        step("8. Monitor Run")

        # Get a WebSocket token
        ws_token = None
        resp = client.post("/api/v1/auth/ws-token", json={}, headers=headers)
        if resp.status_code == 200:
            ws_token = resp.json()["ws_token"]
            ws_scheme = "wss" if BASE_URL.startswith("https") else "ws"
            ws_url = f"{ws_scheme}://{urlparse(BASE_URL).netloc}"
            print(f"  WebSocket available: {ws_url}/api/v1/runs/{run_id}/ws?token={ws_token[:20]}...")
        else:
            print("  WebSocket token not available — falling back to polling")

        # Poll until terminal
        terminal = {"completed", "failed", "cancelled"}
        final_status = None

        for i in range(MAX_POLLS):
            time.sleep(POLL_INTERVAL)
            resp = client.get(f"/api/v1/runs/{run_id}", headers=headers)
            if resp.status_code != 200:
                print(f"  [poll {i + 1}] Error: {resp.status_code}")
                continue
            status = resp.json()["status"]
            print(f"  [{i + 1}] status = {status}")
            final_status = status
            if status in terminal:
                break
        else:
            print(f"  Run did not complete in {MAX_POLLS * POLL_INTERVAL}s — cancelling")
            client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
            final_status = "cancelled"

        # ═══════════════════════════════════════════════════════════
        # STEP 9: Handle HITL Gates (if any)
        # ═══════════════════════════════════════════════════════════
        step("9. Handle HITL Gates")
        resp = client.get(f"/api/v1/runs/{run_id}/hitl/pending", headers=headers)
        if resp.status_code == 200:
            gates = resp.json().get("gates", [])
            if gates:
                print(f"  {len(gates)} HITL gate(s) to review:")
                for g in gates:
                    print(f"    Gate {g['gate_id']} (node: {g.get('node_id', 'N/A')})")
                    # Claim the gate
                    claim = client.post(
                        f"/api/v1/runs/{run_id}/hitl/{g['gate_id']}/claim",
                        json={"expiry_minutes": 5},
                        headers=headers,
                    )
                    if claim.status_code == 200:
                        ct = claim.json()["claim_token"]
                        print(f"      Claimed! Token: {ct[:20]}...")
                        # Approve it
                        approve = client.post(
                            f"/api/v1/runs/{run_id}/hitl/{g['gate_id']}/approve",
                            json={"claim_token": ct, "notes": "Approved by full-workflow.py"},
                            headers=headers,
                        )
                        if approve.status_code == 200:
                            print(f"      Approved! ({approve.json()['status']})")
                        else:
                            print(f"      Approve failed: {approve.status_code}")
                    else:
                        print(f"      Claim failed: {claim.status_code}")
            else:
                print("  No pending HITL gates")
        else:
            print(f"  Could not check gates ({resp.status_code})")

        # ═══════════════════════════════════════════════════════════
        # STEP 10: View Run Results
        # ═══════════════════════════════════════════════════════════
        step("10. View Run Results")
        # Final status
        resp = client.get(f"/api/v1/runs/{run_id}", headers=headers)
        if resp.status_code == 200:
            final = resp.json()
            print(f"  Final status: {final['status']}")

        # Run IO (per-node input/output)
        resp = client.get(f"/api/v1/runs/{run_id}/io", headers=headers)
        if resp.status_code == 200:
            io = resp.json()
            print(f"  Run status:  {io.get('status', 'N/A')}")
            print(f"  Input:       {json.dumps(io.get('input_payload', {}), indent=2)[:200]}")
            outputs = io.get("outputs_json")
            if outputs:
                print(f"  Output:      {json.dumps(outputs, indent=2)[:300]}")
            fixtures = io.get("fixture_map", {})
            if fixtures:
                print(f"  Fixtures:    {len(fixtures)} available")
        else:
            print(f"  IO not available ({resp.status_code})")

        # ═══════════════════════════════════════════════════════════
        # Summary
        # ═══════════════════════════════════════════════════════════
        step("Complete")
        print(f"  Pipeline: {pipeline_id}")
        print(f"  Agent:    {agent_id}")
        print(f"  Run:      {run_id}")
        print(f"  Status:   {final_status}")

    finally:
        client.close()

    print("\n✓ Full workflow complete.")


if __name__ == "__main__":
    main()
