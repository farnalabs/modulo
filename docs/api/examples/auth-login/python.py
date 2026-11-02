"""
Modulo API Example: Authentication (Login)

Demonstrates:
  - Login with email/password via POST /api/v1/auth/login
  - Token refresh via POST /api/v1/auth/refresh
  - Token revocation (logout) via POST /api/v1/auth/logout
  - Fetching the current user profile via GET /api/v1/auth/me

Authentication model (FAR-1197):
  - The access token comes back in the login JSON body as ``access_token``.
  - The refresh token is set ONLY as the httpOnly ``modulo_refresh`` cookie; it
    never appears in a response body.
  - ``/auth/refresh`` and ``/auth/logout`` read that cookie, so they take no
    request body. They are also guarded by a double-submit CSRF check: the
    ``XSRF-TOKEN`` cookie must be echoed in the ``X-CSRF-Token`` header.
  - Every login and refresh sets fresh auth and CSRF cookies on the response;
    an HTTP client with a cookie jar (``httpx.Client``) applies them for you.

Usage:
  export MODULO_URL=http://localhost:8000
  export MODULO_EMAIL=admin@example.com
  export MODULO_PASSWORD=changeme
  python auth-login/python.py
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


def csrf_headers(client: httpx.Client) -> dict[str, str]:
    """Echo the JS-readable XSRF-TOKEN cookie in the X-CSRF-Token header."""
    token = client.cookies.get("XSRF-TOKEN")
    if not token:
        bail("XSRF-TOKEN cookie missing: login did not return a CSRF token")
    return {"X-CSRF-Token": token}


def main():
    if not EMAIL or not PASSWORD:
        bail("MODULO_EMAIL and MODULO_PASSWORD must be set")

    client = httpx.Client(base_url=BASE_URL, timeout=30)

    # Step 1: Login. The access token is in the body; auth and CSRF cookies are
    # set on the response and held in the client's cookie jar.
    print(f"Logging in as {EMAIL} ...")
    resp = client.post(
        "/api/v1/auth/login",
        json={
            "email": EMAIL,
            "password": PASSWORD,
        },
    )
    if resp.status_code != 200:
        bail(f"login failed: {resp.status_code} {resp.text}")

    data = resp.json()
    access_token = data["access_token"]
    print(f"  access_token:  {access_token[:20]}...")
    print(f"  token_type:    {data['token_type']}")
    print(f"  modulo_refresh cookie set: {'modulo_refresh' in client.cookies}")
    print(f"  must_change_password:      {data.get('must_change_password', False)}")

    # Step 2: Use the access token to call an authenticated endpoint
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = client.get("/api/v1/auth/me", headers=headers)
    if resp.status_code != 200:
        bail(f"GET /auth/me failed: {resp.status_code} {resp.text}")

    me = resp.json()
    print(f"\nAuthenticated as: {me['display_name']} <{me['email']}>")
    print(f"  role:       {me['org_role']}")
    print(f"  user_id:    {me['id']}")
    print(f"  active:     {me['active']}")

    # Step 3: Refresh the access token. No body: the server reads the
    # modulo_refresh cookie, and the double-submit header must match the
    # XSRF-TOKEN cookie sent with this request.
    print("\nRefreshing token ...")
    resp = client.post("/api/v1/auth/refresh", headers=csrf_headers(client))
    if resp.status_code != 200:
        bail(f"refresh failed: {resp.status_code} {resp.text}")

    new_access = resp.json()["access_token"]
    print(f"  new access_token: {new_access[:20]}...")
    print(f"  rotated modulo_refresh cookie: {'modulo_refresh' in client.cookies}")

    # Step 4: Logout (revoke the refresh token family). Uses the rotated
    # XSRF-TOKEN cookie, not the pre-refresh one.
    print("\nLogging out (revoking refresh token family) ...")
    resp = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {new_access}", **csrf_headers(client)},
    )
    if resp.status_code != 200:
        bail(f"logout failed: {resp.status_code} {resp.text}")
    print(f"  {resp.json()['detail']}")

    print("\nDone.")


if __name__ == "__main__":
    main()
