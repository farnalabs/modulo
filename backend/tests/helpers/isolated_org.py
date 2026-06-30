"""Create and manage isolated orgs for test isolation.

Each test run gets its own org so test data is fully isolated.
Used when testing against real deployments (staging.modulo.run)
rather than mock sessions.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Self

import httpx

BASE_URL: str = "https://staging-modulo.fly.dev"


class IsolatedOrgContext:
    """Context for a test org that was created via the admin API."""

    def __init__(
        self,
        *,
        org_id: str,
        slug: str,
        user_email: str,
        user_password: str,
        base_url: str,
        admin_token: str,
    ) -> None:
        self.org_id = org_id
        self.slug = slug
        self.user_email = user_email
        self.user_password = user_password
        self.base_url = base_url
        self.admin_token = admin_token


def _random_slug() -> str:
    """Generate a unique slug for a test org."""
    suffix = secrets.token_hex(8)
    return f"e2e-{suffix}"


async def create_isolated_org(
    *,
    base_url: str = BASE_URL,
    admin_email: str = "admin@staging.modulo",
    admin_password: str = "admin123",
) -> IsolatedOrgContext:
    """Create an isolated org + user via the admin API.

    Returns a context with the org ID and user credentials.
    The caller should call destroy_isolated_org() to clean up.

    Uses httpx for async HTTP calls.
    """
    slug = _random_slug()
    org_name = f"E2E Test {slug}"

    # Step 1: Get CSRF token and auth session
    async with httpx.AsyncClient(base_url=base_url, verify=False) as client:
        # Get CSRF token (required for auth endpoints)
        # Use API key auth for the admin API instead to avoid CSRF
        # First, log in via basic auth to get a session
        login_resp = await client.post(
            "/api/auth/login",
            json={"username": admin_email, "password": admin_password},
        )
        if login_resp.status_code == 200:
            token_data = login_resp.json()
            admin_token = token_data.get("access_token", "")
        else:
            # CSRF-protected — use direct API key
            # Try the API auth endpoint instead
            login_resp = await client.post(
                "/api/v1/auth/login",
                json={"username": admin_email, "password": admin_password},
            )
            if login_resp.status_code != 200:
                raise RuntimeError(
                    f"Login failed ({login_resp.status_code}): {login_resp.text}"
                )
            data = login_resp.json()
            admin_token = data.get("access_token", data.get("token", ""))

    # Step 2: Create org using admin token
    auth_headers = {"Authorization": f"Bearer {admin_token}"}
    async with httpx.AsyncClient(
        base_url=base_url, verify=False, headers=auth_headers
    ) as client:
        org_resp = await client.post(
            "/api/v1/admin/orgs",
            json={"name": org_name, "slug": slug},
        )
        if org_resp.status_code != 201:
            raise RuntimeError(
                f"Org creation failed ({org_resp.status_code}): {org_resp.text}"
            )
        org_data = org_resp.json()
        org_id = org_data["id"]

        # Step 3: Create a user in the org
        user_email = f"runner-{slug}@e2e.modulo"
        user_password = secrets.token_urlsafe(16)
        user_resp = await client.post(
            f"/api/v1/admin/orgs/{org_id}/users",
            json={
                "email": user_email,
                "display_name": f"E2E Runner {slug}",
                "password": user_password,
                "org_role": "admin",
            },
        )
        if user_resp.status_code != 201:
            raise RuntimeError(
                f"User creation failed ({user_resp.status_code}): {user_resp.text}"
            )

    return IsolatedOrgContext(
        org_id=org_id,
        slug=slug,
        user_email=user_email,
        user_password=user_password,
        base_url=base_url,
        admin_token=admin_token,
    )


async def destroy_isolated_org(ctx: IsolatedOrgContext) -> None:
    """Teardown: delete the org and clean up.

    Sends a deletion request to the admin API.
    """
    auth_headers = {"Authorization": f"Bearer {ctx.admin_token}"}
    async with httpx.AsyncClient(
        base_url=ctx.base_url, verify=False, headers=auth_headers
    ) as client:
        # Request org deletion
        del_resp = await client.post("/api/v1/admin/org/deletion-request")
        if del_resp.status_code not in (200, 404):
            # Best-effort cleanup — don't raise
            pass


async def get_admin_token(
    *, base_url: str = BASE_URL, email: str, password: str
) -> str:
    """Authenticate and return a Bearer token."""
    async with httpx.AsyncClient(base_url=base_url, verify=False) as client:
        # Try API auth endpoint
        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": email, "password": password},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Auth failed ({resp.status_code}): {resp.text}"
            )
        data = resp.json()
        return data.get("access_token", data.get("token", ""))
