#!/usr/bin/env python3
"""
Modulo API Example: Authentication (Login)

Demonstrates:
  - Login with email/password via POST /api/v1/auth/login
  - Token refresh via POST /api/v1/auth/refresh
  - Token revocation (logout) via POST /api/v1/auth/logout
  - Fetching the current user profile via GET /api/v1/auth/me

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


def main():
    if not EMAIL or not PASSWORD:
        bail("MODULO_EMAIL and MODULO_PASSWORD must be set")

    client = httpx.Client(base_url=BASE_URL, timeout=30)

    # Step 1: Login — get access + refresh tokens
    print(f"Logging in as {EMAIL} ...")
    resp = client.post("/api/v1/auth/login", json={
        "email": EMAIL,
        "password": PASSWORD,
    })
    if resp.status_code != 200:
        bail(f"login failed: {resp.status_code} {resp.text}")

    data = resp.json()
    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    print(f"  access_token:  {access_token[:20]}...")
    print(f"  refresh_token: {refresh_token[:20]}...")
    print(f"  token_type:    {data['token_type']}")

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

    # Step 3: Refresh the access token
    print("\nRefreshing token ...")
    resp = client.post("/api/v1/auth/refresh", json={
        "refresh_token": refresh_token,
    })
    if resp.status_code != 200:
        bail(f"refresh failed: {resp.status_code} {resp.text}")

    refreshed = resp.json()
    new_access = refreshed["access_token"]
    new_refresh = refreshed["refresh_token"]
    print(f"  new access_token:  {new_access[:20]}...")
    print(f"  new refresh_token: {new_refresh[:20]}...")

    # Step 4: Logout (revoke the refresh token family)
    print("\nLogging out (revoking refresh token family) ...")
    resp = client.post("/api/v1/auth/logout", json={
        "refresh_token": new_refresh,
    }, headers={"Authorization": f"Bearer {new_access}"})
    if resp.status_code != 200:
        bail(f"logout failed: {resp.status_code} {resp.text}")
    print(f"  {resp.json()['detail']}")

    print("\nDone.")


if __name__ == "__main__":
    main()
