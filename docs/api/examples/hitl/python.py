#!/usr/bin/env python3
"""
Modulo API Example: Human-in-the-Loop (HITL) Gate Management

Demonstrates:
  - List all pending HITL gates across the org
  - List pending gates for a specific run
  - Claim a gate (with timeout)
  - Approve a gate
  - Reject a gate

Usage:
  export MODULO_URL=http://localhost:8000
  export MODULO_EMAIL=admin@example.com
  export MODULO_PASSWORD=changeme
  python hitl/python.py

Note: Requires a run that is currently waiting at a HITL gate. Trigger a
pipeline that has a human_review node (see runs/ example), then run this.
"""

import os
import sys

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

    # Step 1: List all org-pending HITL gates
    print("Checking all pending HITL gates in the org ...")
    resp = client.get("/api/v1/hitl/pending", headers=headers)
    if resp.status_code != 200:
        bail(f"list pending gates failed: {resp.status_code} {resp.text}")
    gates = resp.json().get("gates", [])
    print(f"  {len(gates)} pending gate(s)")

    if not gates:
        print("\nNo pending gates found.")
        print("Trigger a pipeline with a human_review node, then re-run.")
        return

    # Show summary of all pending gates
    for g in gates:
        print(f"  - Gate {g['gate_id']} | Run {g['run_id']} | Node: {g.get('node_id', 'N/A')}")
        print(f"    Created: {g.get('created_at', 'N/A')}")
        claimed_by = g.get("claimed_by")
        print(f"    Claimed: {claimed_by if claimed_by else 'No (available)'}")

    # Step 2: Pick the first available (unclaimed) gate
    available = [g for g in gates if not g.get("claimed_by")]
    if not available:
        print("\nAll gates are already claimed. Waiting for release ...")
        return

    gate = available[0]
    run_id = gate["run_id"]
    gate_id = gate["gate_id"]
    print(f"\nUsing gate {gate_id} on run {run_id}")

    # Step 3: Claim the gate
    print(f"\nClaiming gate {gate_id} ...")
    resp = client.post(
        f"/api/v1/runs/{run_id}/hitl/{gate_id}/claim",
        json={"expiry_minutes": 10},
        headers=headers,
    )
    if resp.status_code != 200:
        if resp.status_code == 409:
            bail("Gate already claimed by someone else (409 Conflict)")
        bail(f"claim failed: {resp.status_code} {resp.text}")

    claim = resp.json()
    claim_token = claim["claim_token"]
    print(f"  Claimed! Token: {claim_token[:20]}...")
    print(f"  Expires at: {claim['expires_at']}")

    # Step 4: Approve or reject based on input
    print("\n--- Human Review Step ---")
    print("Gate context:", gate.get("context", "N/A"))
    choice = input("Approve or reject? (a/r) [a]: ").strip().lower() or "a"

    if choice == "a":
        print(f"\nApproving gate {gate_id} ...")
        resp = client.post(
            f"/api/v1/runs/{run_id}/hitl/{gate_id}/approve",
            json={"claim_token": claim_token, "notes": "Looks good, proceed."},
            headers=headers,
        )
        if resp.status_code != 200:
            bail(f"approve failed: {resp.status_code} {resp.text}")
        print(f"  Approved! Status: {resp.json()['status']}")
    else:
        print(f"\nRejecting gate {gate_id} ...")
        resp = client.post(
            f"/api/v1/runs/{run_id}/hitl/{gate_id}/reject",
            json={"claim_token": claim_token, "reason": "Needs revision before proceeding."},
            headers=headers,
        )
        if resp.status_code != 200:
            bail(f"reject failed: {resp.status_code} {resp.text}")
        print(f"  Rejected! Status: {resp.json()['status']}")

    print("\nDone.")


if __name__ == "__main__":
    main()
