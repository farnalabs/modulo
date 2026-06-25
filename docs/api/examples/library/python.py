#!/usr/bin/env python3
"""
Modulo API Example: Library Primitive Management

Demonstrates:
  - Browse library primitives (list with type/search filters)
  - Get primitive detail / preview
  - Create a library primitive
  - Copy a primitive to adapt (clone)
  - Submit a rating

Usage:
  export MODULO_URL=http://localhost:8000
  export MODULO_EMAIL=admin@example.com
  export MODULO_PASSWORD=changeme
  python library/python.py
"""

import os
import sys
import uuid

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

    # Step 1: Browse library (list all primitives)
    print("Browsing library ...")
    resp = client.get("/api/v1/libraries", params={
        "page": 1, "page_size": 20,
    }, headers=headers)
    if resp.status_code != 200:
        bail(f"browse failed: {resp.status_code} {resp.text}")
    data = resp.json()
    print(f"  Found {data['total']} primitive(s)")
    for p in data["items"]:
        print(f"    - {p['id']}: {p['name']} (type={p.get('primitive_type', 'N/A')})")

    # Step 2: Search for a specific type
    print("\nSearching for 'agent' type primitives ...")
    resp = client.get("/api/v1/libraries", params={
        "primitive_type": "agent",
        "search": "review",
        "page": 1, "page_size": 10,
    }, headers=headers)
    if resp.status_code == 200:
        results = resp.json()
        print(f"  Found {results['total']} matching primitive(s)")
        for p in results["items"]:
            print(f"    - {p['name']} ({p['id']})")
    else:
        print(f"  Search failed ({resp.status_code})")

    # Step 3: Preview a specific primitive (if any exist)
    items = data["items"]
    if items:
        prim = items[0]
        prim_id = prim["id"]
        print(f"\nPreviewing primitive '{prim['name']}' ({prim_id}) ...")
        resp = client.get(f"/api/v1/libraries/{prim_id}", headers=headers)
        if resp.status_code == 200:
            detail = resp.json()
            print(f"  Name:        {detail['name']}")
            print(f"  Type:        {detail.get('primitive_type', 'N/A')}")
            print(f"  Description: {detail.get('description', 'N/A')}")
            print(f"  Content:     {str(detail.get('content_json', {}))[:200]}...")
        else:
            print(f"  Preview failed ({resp.status_code})")

        # Step 4: Copy-to-adapt (clone a primitive)
        print(f"\nCopying primitive '{prim['name']}' to adapt ...")
        resp = client.post(f"/api/v1/libraries/{prim_id}/adapt", json={
            # Optionally specify a target team; omit for personal copy
        }, headers=headers)
        if resp.status_code == 201:
            cloned = resp.json()
            print(f"  Cloned! New primitive: {cloned['name']} ({cloned['id']})")
        elif resp.status_code == 200:
            cloned = resp.json()
            print(f"  Cloned! New primitive: {cloned['name']} ({cloned['id']})")
        else:
            print(f"  Copy-to-adapt failed ({resp.status_code}): {resp.text}")

        # Step 5: Submit a rating
        print(f"\nSubmitting rating for '{prim['name']}' ...")
        resp = client.post(f"/api/v1/libraries/{prim_id}/ratings", json={
            "thumbs_up": True,
            "comment": "Great primitive, very useful!",
        }, headers=headers)
        if resp.status_code == 201:
            rating = resp.json()
            print(f"  Rated! ID: {rating.get('id', 'N/A')}")
        else:
            print(f"  Rating failed ({resp.status_code}): {resp.text}")
    else:
        # Step 3b: Create a primitive if none exist
        print("\nNo primitives found. Creating one ...")
        resp = client.post("/api/v1/libraries", json={
            "name": "Code Review Agent",
            "primitive_type": "agent",
            "slug": f"code-review-agent-{uuid.uuid4().hex[:8]}",
            "description": "An agent that reviews pull request code changes",
            "content_json": {
                "prompt_template": "Review the following PR diff: {{diff}}",
                "input_schema": {"type": "object", "properties": {"diff": {"type": "string"}}},
                "output_schema": {"type": "object", "properties": {"comment": {"type": "string"}}},
            },
        }, headers=headers)
        if resp.status_code == 201:
            created = resp.json()
            print(f"  Created: {created['name']} ({created['id']})")
        else:
            print(f"  Create failed ({resp.status_code}): {resp.text}")

    print("\nDone.")


if __name__ == "__main__":
    main()
