"""Create and manage isolated orgs for test isolation.

Each test run gets its own org so test data is fully isolated.
Used when testing against real deployments (staging.modulo.run)
rather than mock sessions.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from typing import Any, cast

import httpx

_log = logging.getLogger(__name__)

BASE_URL: str = os.environ.get(
    "MODULO_BASE_URL",
    "https://staging-modulo.fly.dev",
)
_HTTP_TIMEOUT: float = 30.0


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


def _make_client(
    base_url: str,
    token: str | None = None,
) -> httpx.AsyncClient:
    """Create an httpx client for the staging API."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    verify_ssl = os.environ.get("MODULO_VERIFY_SSL", "0") == "1"
    return httpx.AsyncClient(
        base_url=base_url,
        verify=verify_ssl,
        headers=headers or None,
        timeout=_HTTP_TIMEOUT,
    )


def _raise_on_bad_status(
    response: httpx.Response,
    expected: int,
    action: str,
) -> None:
    """Check HTTP response status and raise if unexpected."""
    if response.status_code != expected:
        raise RuntimeError(
            f"{action} failed ({response.status_code}): {response.text}",
        )


def _extract_token(data: dict[str, Any]) -> str:
    """Extract access token from auth response or raise."""
    token = data.get("access_token") or data.get("token")
    if not token:
        raise RuntimeError(
            "Auth response missing access_token and token keys",
        )
    return cast(str, token)


def _parse_json_response(response: httpx.Response, action: str) -> dict[str, Any]:
    """Parse JSON from an HTTP response with a descriptive error."""
    try:
        result: dict[str, Any] = response.json()
        return result
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse {action} response as JSON ({response.status_code}): {response.text}",
        ) from exc


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

    admin_token = await get_admin_token(
        base_url=base_url,
        email=admin_email,
        password=admin_password,
    )

    org_id: str | None = None
    try:
        async with _make_client(base_url, token=admin_token) as client:
            org_resp = await client.post(
                "/api/v1/admin/orgs",
                json={"name": org_name, "slug": slug},
            )
            _raise_on_bad_status(org_resp, 201, "Org creation")
            org_data = _parse_json_response(org_resp, "org creation")
            org_id = org_data.get("id")
            if not org_id:
                raise RuntimeError(
                    f"Org creation response missing 'id': {org_data}",
                )

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
            _raise_on_bad_status(user_resp, 201, "User creation")
    except Exception:
        if org_id:
            try:
                async with _make_client(
                    base_url,
                    token=admin_token,
                ) as cleanup_client:
                    await cleanup_client.post(
                        "/api/v1/admin/org/deletion-request",
                        json={"org_id": org_id},
                    )
            except httpx.RequestError:
                _log.warning(
                    "Cleanup org deletion request failed for %s",
                    org_id,
                )
        raise

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
    try:
        async with _make_client(
            ctx.base_url,
            token=ctx.admin_token,
        ) as client:
            resp = await client.post(
                "/api/v1/admin/org/deletion-request",
                json={"org_id": ctx.org_id},
            )
            if resp.status_code not in (200, 201, 202, 204):
                _log.warning(
                    "Org deletion returned %s for %s: %s",
                    resp.status_code,
                    ctx.org_id,
                    resp.text,
                )
    except httpx.RequestError:
        _log.warning("Org deletion request failed for %s", ctx.org_id)


async def get_admin_token(
    *,
    base_url: str = BASE_URL,
    email: str,
    password: str,
) -> str:
    """Authenticate and return a Bearer token."""
    async with _make_client(base_url) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        _raise_on_bad_status(resp, 200, "Auth")
        data = _parse_json_response(resp, "auth")
        return _extract_token(data)
