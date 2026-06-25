#!/usr/bin/env python3
"""
Modulo API Example: Pipeline CRUD

Demonstrates:
  - List pipelines with pagination
  - Create a new pipeline
  - Get pipeline detail
  - Update pipeline metadata
  - Delete pipeline

Usage:
  export MODULO_URL=http://localhost:8000
  export MODULO_EMAIL=admin@example.com
  export MODULO_PASSWORD=changeme
  python pipelines/python.py
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
        "email": EMAIL,
        "password": PASSWORD,
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

    # Step 1: List all pipelines
    print("Listing pipelines ...")
    resp = client.get("/api/v1/pipelines", params={"page": 1, "page_size": 20}, headers=headers)
    if resp.status_code != 200:
        bail(f"list failed: {resp.status_code} {resp.text}")
    data = resp.json()
    print(f"  Found {data['total']} pipeline(s) (page {data['page']}/{data['page_size']})")
    for p in data["items"]:
        print(f"    - {p['id']}: {p['name']} (visibility={p.get('visibility', 'org')})")

    # Step 2: Create a new pipeline
    print("\nCreating pipeline 'PR Review Pipeline' ...")
    resp = client.post("/api/v1/pipelines", json={
        "name": "PR Review Pipeline",
        "description": "Automated PR review pipeline for code quality",
        "visibility": "org",
        "max_concurrent_runs": 3,
    }, headers=headers)
    if resp.status_code != 201:
        bail(f"create failed: {resp.status_code} {resp.text}")
    pipeline = resp.json()
    pipeline_id = pipeline["id"]
    print(f"  Created: {pipeline['name']} (id={pipeline_id})")

    # Step 3: Get pipeline detail
    print(f"\nFetching pipeline {pipeline_id} ...")
    resp = client.get(f"/api/v1/pipelines/{pipeline_id}", headers=headers)
    if resp.status_code != 200:
        bail(f"get failed: {resp.status_code} {resp.text}")
    detail = resp.json()
    print(f"  Name:        {detail['name']}")
    print(f"  Description: {detail.get('description', 'N/A')}")
    print(f"  Visibility:  {detail['visibility']}")
    print(f"  Created:     {detail.get('created_at', 'N/A')}")

    # Step 4: Update pipeline description
    print("\nUpdating pipeline description ...")
    resp = client.patch(f"/api/v1/pipelines/{pipeline_id}", json={
        "description": "Updated: Now handles code review + security scanning",
        "max_concurrent_runs": 5,
    }, headers=headers)
    if resp.status_code != 200:
        bail(f"update failed: {resp.status_code} {resp.text}")
    updated = resp.json()
    print(f"  New description: {updated['description']}")
    print(f"  Max concurrent:  {updated['max_concurrent_runs']}")

    # Step 5: Delete pipeline
    print(f"\nDeleting pipeline {pipeline_id} ...")
    resp = client.delete(f"/api/v1/pipelines/{pipeline_id}", headers=headers)
    if resp.status_code != 204:
        bail(f"delete failed: {resp.status_code} {resp.text}")
    print("  Deleted successfully (204 No Content)")

    print("\nDone.")


if __name__ == "__main__":
    main()
