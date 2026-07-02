"""Clean up stale E2E test orgs (slug prefix: e2e-).

Usage:
    python -m tests.staging_e2e.cleanup [--max-age-hours 2] [--dry-run] [--base-url https://staging.modulo.run]
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from .tenant_setup import ADMIN_EMAIL, ADMIN_PASSWORD, SLUG_PREFIX, STAGING_URL, get_admin_token


async def find_stale_orgs(
    base_url: str,
    admin_email: str,
    admin_password: str,
) -> list[dict]:
    """Find orgs whose slug starts with e2e-."""
    token = await get_admin_token(base_url, admin_email, admin_password)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=base_url, verify=False, headers=headers) as client:
        # Hitting the admin org list endpoint
        resp = await client.get("/api/v1/admin/orgs")
        if resp.status_code != 200:
            print(f"Failed to list orgs: {resp.status_code} {resp.text}", file=sys.stderr)
            return []

        orgs = resp.json()
        stale = []
        for org in orgs:
            slug = org.get("slug", "")
            if slug.startswith(SLUG_PREFIX):
                stale.append(org)

        return stale


async def cleanup(
    base_url: str = STAGING_URL,
    admin_email: str = ADMIN_EMAIL,
    admin_password: str = ADMIN_PASSWORD,
    dry_run: bool = False,
) -> int:
    """Delete all E2E test orgs. Returns count deleted."""
    token = await get_admin_token(base_url, admin_email, admin_password)
    headers = {"Authorization": f"Bearer {token}"}

    stale = await find_stale_orgs(base_url, admin_email, admin_password)
    if not stale:
        print("No stale E2E test orgs found.")
        return 0

    print(f"Found {len(stale)} stale E2E test org(s):")
    for org in stale:
        print(f"  {org.get('slug')} (id={org.get('id')})")

    if dry_run:
        print("Dry-run mode. Nothing deleted.")
        return 0

    async with httpx.AsyncClient(base_url=base_url, verify=False, headers=headers) as client:
        for org in stale:
            org_id = org.get("id")
            if not org_id:
                continue
            try:
                resp = await client.delete(f"/api/v1/admin/orgs/{org_id}")
                print(f"  Deleted {org.get('slug')}: {resp.status_code}")
            except Exception as e:
                print(f"  Failed to delete {org.get('slug')}: {e}", file=sys.stderr)

    return len(stale)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up stale E2E test orgs")
    parser.add_argument("--base-url", default=STAGING_URL)
    parser.add_argument("--admin-email", default=ADMIN_EMAIL)
    parser.add_argument("--admin-password", default=ADMIN_PASSWORD)
    parser.add_argument("--dry-run", action="store_true", help="List orgs without deleting")
    args = parser.parse_args()

    count = asyncio.run(cleanup(
        base_url=args.base_url,
        admin_email=args.admin_email,
        admin_password=args.admin_password,
        dry_run=args.dry_run,
    ))
    print(f"Cleanup complete. {count} org(s) removed.")


if __name__ == "__main__":
    main()
