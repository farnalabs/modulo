"""Seed test data for Locust load testing.

Creates via the API:
  - Test organisation with admin user
  - API keys for programmatic access
  - Test pipelines with various configurations
  - Test trigger configurations (manual, cron)

Prerequisites:
  - Docker Compose stack is running (postgres, redis, backend)
  - Admin credentials exist in the target environment

Usage:
  python -m tests.load.data_seed
  python -m tests.load.data_seed --base-url http://localhost:8000/api/v1
  python -m tests.load.data_seed --admin-email admin@modulo.test --admin-password test-password-123
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import requests

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DEFAULT_BASE_URL = "http://localhost:8000/api/v1"
DEFAULT_ADMIN_EMAIL = "admin@modulo.test"
DEFAULT_ADMIN_PASSWORD = "test-password-123"


class SeedClient:
    """HTTP client wrapper for data seeding."""

    _TIMEOUT = 30.0

    def __init__(self, base_url: str, email: str, password: str):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        self.token = self._login(email, password)

    def _login(self, email: str, password: str) -> str:
        resp = self._session.post(
            f"{self.base_url}/auth/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        self._session.headers.update({"Authorization": f"Bearer {token}"})
        _log.info("Authenticated as %s", email)
        return token

    def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any] | list[Any] | None:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self._TIMEOUT)
        resp = self._session.request(method, url, **kwargs)
        if resp.status_code == 204:
            return None
        try:
            data = resp.json()
        except requests.JSONDecodeError:
            _log.warning("Non-JSON response %s %s: %s", method, path, resp.text[:200])
            resp.raise_for_status()
            return None
        if not resp.ok:
            _log.error("Request failed %s %s: %s", method, path, data)
            resp.raise_for_status()
        return data

    def get(self, path: str) -> dict[str, Any] | list[Any] | None:
        return self._request("GET", path)

    def post(self, path: str, json_data: dict[str, Any] | None = None) -> dict[str, Any] | list[Any] | None:
        return self._request("POST", path, json=json_data or {})

    def delete(self, path: str) -> None:
        self._request("DELETE", path)


def seed_pipelines(client: SeedClient, count: int = 5) -> list[dict[str, Any]]:
    """Create *count* test pipelines with varying configurations."""
    pipelines = []
    configs = [
        {"name": "Load Test - Simple Agent", "description": "Single-agent pipeline for load testing",
         "max_concurrent_runs": 10},
        {"name": "Load Test - Sequential Chain", "description": "Multi-agent sequential chain",
         "max_concurrent_runs": 5},
        {"name": "Load Test - HITL Gate", "description": "Pipeline with human-in-the-loop gate",
         "max_concurrent_runs": 3, "default_autonomy_level": "manual_approval"},
        {"name": "Load Test - High Concurrency", "description": "High-concurrency pipeline",
         "max_concurrent_runs": 25},
        {"name": "Load Test - Long Running", "description": "Long timeout pipeline for stress testing",
         "node_timeout_seconds": 600, "lock_wait_timeout_seconds": 600},
    ]

    for i in range(count):
        cfg = configs[i % len(configs)]
        try:
            pipeline = client.post(
                "/pipelines",
                json_data={
                    "name": f"{cfg['name']} #{i + 1}",
                    "description": cfg.get("description", ""),
                    "visibility": "org",
                    "max_concurrent_runs": cfg.get("max_concurrent_runs", 5),
                    "node_timeout_seconds": cfg.get("node_timeout_seconds", 300),
                    "lock_wait_timeout_seconds": cfg.get("lock_wait_timeout_seconds", 300),
                    "default_autonomy_level": cfg.get("default_autonomy_level", "manual_approval"),
                },
            )
            if isinstance(pipeline, dict):
                pipelines.append(pipeline)
                _log.info("Created pipeline: %s (%s)", pipeline["name"], pipeline["id"])
        except requests.HTTPError as exc:
            _log.warning("Failed to create pipeline %d: %s", i + 1, exc)

    _log.info("Created %d/%d pipelines", len(pipelines), count)
    return pipelines


def seed_triggers(client: SeedClient, pipelines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create trigger configurations for test pipelines."""
    triggers = []
    if not pipelines:
        _log.warning("No pipelines available for trigger creation")
        return triggers

    pipeline = pipelines[0]
    pipeline_id = pipeline["id"]

    try:
        manual = client.post(
            f"/pipelines/{pipeline_id}/triggers",
            json_data={
                "trigger_type": "manual",
                "active": True,
                "max_concurrent_runs": 5,
                "config_json": {},
            },
        )
        if isinstance(manual, dict):
            triggers.append(manual)
            _log.info("Created manual trigger: %s", manual["id"])
    except requests.HTTPError as exc:
        _log.warning("Failed to create manual trigger: %s", exc)

    try:
        cron = client.post(
            f"/pipelines/{pipeline_id}/triggers",
            json_data={
                "trigger_type": "cron",
                "active": False,
                "cron_expression": "0 */6 * * *",
                "cron_timezone": "UTC",
                "max_concurrent_runs": 1,
                "config_json": {},
            },
        )
        if isinstance(cron, dict):
            triggers.append(cron)
            _log.info("Created cron trigger: %s", cron["id"])
    except requests.HTTPError as exc:
        _log.warning("Failed to create cron trigger: %s", exc)

    return triggers


def seed_api_keys(client: SeedClient, count: int = 3) -> list[dict[str, Any]]:
    """Create API keys for programmatic load test access."""
    keys = []
    names = ["load-test-operator", "load-test-runner", "load-test-ci"]

    for name in names[:count]:
        try:
            key = client.post(
                "/api-keys",
                json_data={
                    "name": name,
                    "role": "operator" if "operator" in name else "runner",
                },
            )
            if isinstance(key, dict):
                key_preview = key.get("full_key", "?")[:20] if "full_key" in key else "?"
                _log.info("Created API key: %s → %s", key.get("name", "?"), key_preview)
                keys.append(key)
        except requests.HTTPError as exc:
            _log.warning("Failed to create API key %s: %s", name, exc)

    return keys


def verify_health(client: SeedClient) -> bool:
    """Quick health check to confirm the API is reachable."""
    try:
        me = client.get("/auth/me")
        if isinstance(me, dict) and me.get("email"):
            _log.info("API reachable, authenticated as %s (role: %s)", me.get("email"), me.get("org_role"))
            return True
    except requests.RequestException as exc:
        _log.error("API unreachable: %s", exc)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed test data for Locust load testing")
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--admin-email", default=os.environ.get("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL))
    parser.add_argument("--admin-password", default=os.environ.get("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD))
    parser.add_argument("--pipelines", type=int, default=5, help="Number of test pipelines to create")
    parser.add_argument("--api-keys", type=int, default=3, help="Number of API keys to create")
    args = parser.parse_args()

    _log.info("Seeding test data for: %s", args.base_url)
    client = SeedClient(args.base_url, args.admin_email, args.admin_password)

    if not verify_health(client):
        sys.exit(1)

    pipelines = seed_pipelines(client, count=args.pipelines)
    triggers = seed_triggers(client, pipelines)
    api_keys = seed_api_keys(client, count=args.api_keys)

    summary = {
        "pipelines_created": len(pipelines),
        "triggers_created": len(triggers),
        "api_keys_created": len(api_keys),
    }
    _log.info("Seed complete: %s", json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
