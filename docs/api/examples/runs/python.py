#!/usr/bin/env python3
"""
Modulo API Example: Run Lifecycle

Demonstrates:
  - Trigger a pipeline run
  - Poll run status
  - Get run detail with IO
  - Cancel a running run
  - List pending HITL gates for a run
  - Get a WebSocket token for real-time event streaming

Usage:
  export MODULO_URL=http://localhost:8000
  export MODULO_EMAIL=admin@example.com
  export MODULO_PASSWORD=changeme
  python runs/python.py
"""

import os
import sys
import time

import httpx

BASE_URL = os.getenv("MODULO_URL", "http://localhost:8000").rstrip("/")
EMAIL = os.getenv("MODULO_EMAIL")
PASSWORD = os.getenv("MODULO_PASSWORD")


def bail(msg: str):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def login(client: httpx.Client) -> str:
    resp = client.post("/api/v1/auth/login", json={
        "email": EMAIL, "password": PASSWORD,
    })
    if resp.status_code != 200:
        bail(f"login failed: {resp.status_code} {resp.text}")
    return resp.json()["access_token"]


def main():
    if not EMAIL or not PASSWORD:
        bail("MODULO_EMAIL and MODULO_PASSWORD must be set")

    client = httpx.Client(base_url=BASE_URL, timeout=30)
    token = login(client)
    headers = {"Authorization": f"Bearer {token}"}

    # First, find a pipeline to trigger
    resp = client.get("/api/v1/pipelines", params={"page": 1, "page_size": 5}, headers=headers)
    if resp.status_code != 200:
        bail(f"list pipelines failed: {resp.status_code} {resp.text}")
    pipelines = resp.json()["items"]
    if not pipelines:
        bail("No pipelines found. Create one first (see pipelines/ example).")
    pipeline_id = pipelines[0]["id"]
    print(f"Using pipeline: {pipelines[0]['name']} ({pipeline_id})")

    # Step 1: Trigger a run
    print("\nTriggering run ...")
    resp = client.post("/api/v1/runs", json={
        "pipeline_id": pipeline_id,
        "input_payload": {"pr_url": "https://github.com/example/org/pull/42"},
    }, headers=headers)
    if resp.status_code != 202:
        bail(f"trigger run failed: {resp.status_code} {resp.text}")
    run = resp.json()
    run_id = run["run_id"]
    print(f"  Run triggered: {run_id}")
    print(f"  Status:        {run['status']}")
    print(f"  Thread ID:     {run.get('langgraph_thread_id', 'N/A')}")

    # Step 2: Poll run status until terminal or timeout
    print("\nPolling run status ...")
    terminal_states = {"completed", "failed", "cancelled"}
    for attempt in range(15):
        time.sleep(2)
        resp = client.get(f"/api/v1/runs/{run_id}", headers=headers)
        if resp.status_code != 200:
            bail(f"get run failed: {resp.status_code} {resp.text}")
        status = resp.json()["status"]
        print(f"  [{attempt + 1}] status = {status}")
        if status in terminal_states:
            break
    else:
        print("  Run did not reach terminal state — cancelling ...")
        resp = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
        if resp.status_code != 202:
            bail(f"cancel failed: {resp.status_code} {resp.text}")
        cancellations_resp = resp.json()
        print(f"  Cancel accepted: {cancellations_resp}")

    # Step 3: Get run detail with IO
    print(f"\nFetching run IO for {run_id} ...")
    resp = client.get(f"/api/v1/runs/{run_id}/io", headers=headers)
    if resp.status_code == 200:
        io = resp.json()
        print(f"  Run status: {io['status']}")
        print(f"  Input:      {io.get('input_payload', 'N/A')}")
        print(f"  Output:     {io.get('outputs_json', 'N/A')}")
        print(f"  Fixtures:   {len(io.get('fixture_map', {}))} fixture(s)")
    else:
        print(f"  IO not available ({resp.status_code})")

    # Step 4: Get a WebSocket token (for real-time streaming)
    print("\nRequesting WebSocket token ...")
    resp = client.post("/api/v1/auth/ws-token", json={}, headers=headers)
    if resp.status_code == 200:
        ws = resp.json()
        print(f"  WS token:    {ws['ws_token'][:20]}...")
        print(f"  Expires in:  {ws['expires_in_minutes']} min")
        ws_url = f"{BASE_URL}/api/v1/runs/{run_id}/ws?token={ws['ws_token']}"
        print(f"  Connect via: websocat {ws_url}")
    else:
        print(f"  WS token not available ({resp.status_code})")

    # Step 5: Check for pending HITL gates for this run
    print(f"\nChecking pending HITL gates for run {run_id} ...")
    resp = client.get(f"/api/v1/runs/{run_id}/hitl/pending", headers=headers)
    if resp.status_code == 200:
        gates = resp.json().get("gates", [])
        print(f"  {len(gates)} pending gate(s)")
        for g in gates:
            print(f"    - {g['gate_id']} (node: {g.get('node_id', 'N/A')})")
    else:
        print(f"  Could not fetch gates ({resp.status_code})")

    print("\nDone.")


if __name__ == "__main__":
    main()
