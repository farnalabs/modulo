"""Create and manage isolated orgs for test isolation.

Each test run gets its own org so test data is fully isolated.
Used when testing against real deployments (staging.modulo.run)
rather than mock sessions.
"""

from __future__ import annotations

import secrets

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
    admin_email: str = "admin@modulo.run",
    admin_password: str = "admin123",
) -> IsolatedOrgContext:
    """Create an isolated org + user via the admin API.

    Returns a context with the org ID and user credentials.
    """
    slug = _random_slug()
    org_name = f"E2E Test {slug}"

    # Authenticate as admin
    admin_token = await get_admin_token(
        base_url=base_url, email=admin_email, password=admin_password
    )

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
    """Best-effort teardown: delete the test org."""
    auth_headers = {"Authorization": f"Bearer {ctx.admin_token}"}
    async with httpx.AsyncClient(
        base_url=ctx.base_url, verify=False, headers=auth_headers
    ) as client:
        await client.post("/api/v1/admin/org/deletion-request")


async def get_admin_token(
    *, base_url: str = BASE_URL, email: str, password: str
) -> str:
    """Authenticate and return a Bearer token."""
    async with httpx.AsyncClient(base_url=base_url, verify=False) as client:
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
